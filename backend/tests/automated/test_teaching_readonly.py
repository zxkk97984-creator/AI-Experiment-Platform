"""Teaching closure is atomic with student writes; collection remains a system job."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (AcademicTerm, Assignment, Exam, ExamAnswer, ExamQuestion,
                        ExamSubmission, JudgeQuestion, Submission, TeachingClass)
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.services.exam_service import scan_expired_exams
from app.services.time_utils import utc_now
from test_school_offerings import school  # noqa: F401


@pytest.fixture()
def teaching_case(school, db_session_factory):  # noqa: F811
    now = utc_now()
    with db_session_factory() as db:
        assignment = Assignment(course_id=school['course'], offering_id=school['offerings'][0],
                                title='只读作业', status='published')
        exam = Exam(course_id=school['course'], offering_id=school['offerings'][0],
                    title='只读考试', status='published', duration_minutes=30,
                    start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=1))
        db.add_all([assignment, exam]); db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title='代码题', function_name='f',
                                 hidden_tests='', public_cases=[], grading_mode='legacy')
        exam_question = ExamQuestion(exam_id=exam.id, question_type='single_choice', prompt='选择',
                                    options={'A': '正确', 'B': '错误'}, correct_answer={'correct': ['A']},
                                    points=10, grading_mode='legacy')
        db.add_all([question, exam_question]); db.commit()
        return {**school, 'assignment': assignment.id, 'question': question.id,
                'exam': exam.id, 'exam_question': exam_question.id}


def add_attempt(factory, case, *, expired=False, status='started'):
    with factory() as db:
        now = utc_now()
        row = ExamSubmission(exam_id=case['exam'], student_id=case['users']['a'].id,
                             status=status, started_at=now - timedelta(hours=1),
                             expires_at=now + timedelta(minutes=-1 if expired else 30))
        db.add(row); db.commit()
        return row.id


def transition(client, case, kind):
    headers = case['headers']['admin']
    if kind.startswith('term'):
        url = f"/api/v1/academic-terms/{case['term']}"
    elif kind.startswith('class'):
        url = f"/api/v1/teaching-classes/{case['classes'][0]}"
    else:
        url = f"/api/v1/offerings/{case['offerings'][0]}"
    if kind.endswith('delete'):
        return client.delete(url, headers=headers)
    state = 'closed' if kind.startswith('term') else 'archived'
    if kind == 'offering-pending':
        state = 'pending_assignment'
    return client.patch(url, headers=headers, json={'status': state, 'reason': '学期结束'})


@pytest.mark.parametrize('kind', ['term-delete', 'term-patch', 'class-delete', 'class-patch',
                                 'offering-archive', 'offering-pending'])
@pytest.mark.parametrize('expired', [False, True])
def test_started_attempt_blocks_every_closure_entry(client, db_session_factory, teaching_case, kind, expired):
    case = teaching_case
    sid = add_attempt(db_session_factory, case, expired=expired)
    # Even an unpublished exam / withdrawn student remains an outstanding attempt.
    with db_session_factory() as db:
        db.get(Exam, case['exam']).status = 'draft'
        member = db.scalar(select(OfferingEnrollment).where(
            OfferingEnrollment.offering_id == case['offerings'][0],
            OfferingEnrollment.student_id == case['users']['a'].id))
        member.status = 'transferred'
        member.active_scope_key = None
        db.commit()
    response = transition(client, case, kind)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'EXAMS_IN_PROGRESS'
    with db_session_factory() as db:
        assert db.get(AcademicTerm, case['term']).status == 'active'
        assert db.get(TeachingClass, case['classes'][0]).status == 'active'
        assert db.get(CourseOffering, case['offerings'][0]).status == 'active'
        assert db.get(ExamSubmission, sid).status == 'started'


@pytest.mark.parametrize('kind', ['term-delete', 'class-delete', 'offering-archive'])
def test_system_collection_allows_closure_without_waiting_for_grading(client, db_session_factory, teaching_case, kind):
    case = teaching_case
    sid = add_attempt(db_session_factory, case, expired=True)
    assert transition(client, case, kind).status_code == 409
    with db_session_factory() as db:
        result = scan_expired_exams(db, utc_now())
        assert result['expired_claimed'] == 1
        assert db.get(ExamSubmission, sid).status != 'started'
    assert transition(client, case, kind).status_code == 200


@pytest.mark.parametrize('kind', ['term-delete', 'class-delete'])
def test_closed_context_rejects_student_writes_and_get_does_not_collect(client, db_session_factory, teaching_case, kind):
    case = teaching_case
    assert transition(client, case, kind).status_code == 200
    h = case['headers']['a']
    calls = [
        ('post', '/api/v1/judge/submissions', {'question_id': case['question'], 'code': 'pass'}),
        ('post', f"/api/v1/judge/questions/{case['question']}/sample-run", {'question_id': case['question'], 'code': 'pass'}),
        ('post', f"/api/v1/exams/{case['exam']}/start", None),
    ]
    for method, path, payload in calls:
        r = getattr(client, method)(path, headers=h, **({'json': payload} if payload is not None else {}))
        assert r.status_code == 409, r.text
        assert r.json()['detail']['code'] == 'OFFERING_READ_ONLY'
    # Legacy inconsistent data must not allow a GET to perform a student write.
    sid = add_attempt(db_session_factory, case, expired=True)
    for path, payload in [
        (f"/api/v1/exams/{case['exam']}/answers/{case['exam_question']}",
         {'expected_write_generation': 1, 'selected_options': ['A']}),
        (f"/api/v1/exams/{case['exam']}/answers",
         {'expected_write_generation': 1,
          'answers': [{'question_id': case['exam_question'], 'selected_options': ['A']}]}),
    ]:
        r = client.put(path, headers=h, json=payload)
        if path.endswith('/answers'):
            assert r.status_code == 200, r.text
            assert r.json()['results'][0]['code'] == 'OFFERING_READ_ONLY'
        else:
            assert r.status_code == 409, r.text
    assert client.post(f"/api/v1/exams/{case['exam']}/submit", headers=h,
                       json={'expected_write_generation': 1}).status_code == 409
    assert client.post(f"/api/v1/exams/{case['exam']}/questions/{case['exam_question']}/sample-run",
                       headers=h, json={'code': 'pass'}).status_code == 409
    assert client.get(f"/api/v1/exams/{case['exam']}/session", headers=h).status_code == 200
    with db_session_factory() as db:
        assert db.get(ExamSubmission, sid).status == 'started'
        assert db.scalar(select(func.count()).select_from(ExamAnswer)) == 0
        assert db.scalar(select(func.count()).select_from(Submission)) == 0


@pytest.mark.parametrize('status', ['submitted', 'grading', 'graded', 'review_required'])
@pytest.mark.parametrize('kind', ['term-delete', 'class-delete', 'offering-archive', 'offering-pending'])
def test_completed_submission_retry_remains_read_only_after_closure(client, db_session_factory, teaching_case, status, kind):
    case = teaching_case
    sid = add_attempt(db_session_factory, case, status=status)
    assert transition(client, case, kind).status_code == 200
    r = client.post(f"/api/v1/exams/{case['exam']}/submit", headers=case['headers']['a'],
                    json={'expected_write_generation': 1})
    assert r.status_code == 201, r.text
    outsider = client.post(f"/api/v1/exams/{case['exam']}/submit", headers=case['headers']['b'],
                           json={'expected_write_generation': 1})
    assert outsider.status_code == 404, outsider.text
    with db_session_factory() as db:
        assert db.get(ExamSubmission, sid).status == status
        assert db.scalar(select(func.count()).select_from(ExamAnswer)) == 0


@pytest.mark.parametrize('kind', ['assignment', 'exam'])
def test_sample_execution_releases_teaching_locks(client, db_session_factory, teaching_case, monkeypatch, kind):
    from app.api import exams, judge
    from app.services.account_service import lock_policy
    case = teaching_case
    add_attempt(db_session_factory, case)
    with db_session_factory() as db:
        if kind == 'assignment':
            q = db.get(JudgeQuestion, case['question'])
        else:
            q = db.get(ExamQuestion, case['exam_question'])
            q.question_type = 'code'
            q.starter_code = 'def f(): return 1'
        q.public_cases = [{'args': [], 'expected': 1}]
        db.commit()
    executions = []

    def run_sample(*args, **kwargs):
        # A second SQLite connection must be able to acquire the policy write
        # lock while execution is in progress, without any real Docker calls.
        with db_session_factory() as db:
            lock_policy(db)
            db.commit()
        executions.append(True)
        return '1 passed', '', 0, 1

    monkeypatch.setattr(judge if kind == 'assignment' else exams, '_run_docker_pytest', run_sample)
    path = (f"/api/v1/judge/questions/{case['question']}/sample-run" if kind == 'assignment'
            else f"/api/v1/exams/{case['exam']}/questions/{case['exam_question']}/sample-run")
    payload = {'code': 'def f(): return 1'}
    if kind == 'assignment':
        payload['question_id'] = case['question']
    response = client.post(path, headers=case['headers']['a'], json=payload)
    assert response.status_code == 200, response.text
    assert executions == [True]
