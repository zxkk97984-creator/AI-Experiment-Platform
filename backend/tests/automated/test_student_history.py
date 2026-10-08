"""Student history is an ownership-scoped immutable projection, not course access."""
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models import (CourseEnrollment, Exam, ExamAnswer, ExamQuestion, ExamSubmission,
                        ExperimentRecord, ExperimentSubmission, Submission)
from app.services.time_utils import utc_now
from app.models.teaching import OfferingEnrollment
from conftest import (auth_header, create_user, login, make_course, make_submission,
                      make_experiment_module, make_template_version)

API = '/api/v1/users/me/history'


@pytest.fixture()
def history_owner(client, db_session_factory):
    owner = create_user(db_session_factory, 'history-owner', 'student')
    stranger = create_user(db_session_factory, 'history-stranger', 'student')
    return owner, auth_header(login(client, owner.username)[0]), auth_header(login(client, stranger.username)[0])


def seed_exam(factory, owner, *, visible=False, status='graded'):
    course_id = make_course(factory)
    with factory() as db:
        exam = Exam(course_id=course_id, title='Historical exam', status='published',
                    duration_minutes=60, show_score_after_grading=visible)
        db.add(exam)
        db.flush()
        question = ExamQuestion(exam_id=exam.id, question_type='code', prompt='PRIVATE PROMPT',
                                correct_answer={'private': 'PRIVATE ANSWER'}, hidden_tests='PRIVATE TEST', points=100)
        db.add(question)
        db.flush()
        submission = ExamSubmission(exam_id=exam.id, student_id=owner.id, status=status, score=87,
                                    submitted_at=utc_now() if status != 'started' else None,
                                    expires_at=utc_now() - timedelta(days=1))
        db.add(submission)
        db.flush()
        answer = ExamAnswer(submission_id=submission.id, question_id=question.id,
                            code_answer='print("my answer")', score=87, grading_status='completed')
        db.add(answer)
        db.commit()
        return submission.id, course_id


@pytest.mark.parametrize('visible,status,score', [
    (False, 'graded', None), (True, 'grading', None), (True, 'graded', 87),
])
def test_exam_total_and_answer_scores_share_visibility_gate(client, db_session_factory, history_owner, visible, status, score):
    owner, headers, stranger = history_owner
    submission_id, _ = seed_exam(db_session_factory, owner, visible=visible, status=status)
    listing = client.get(API, headers=headers, params={'kind': 'exam'})
    assert listing.status_code == 200, listing.text
    assert listing.json()['items'][0]['score'] == score
    detail = client.get(f'{API}/exam/{submission_id}', headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body['score'] == score
    assert body['answers'][0]['score'] == score
    assert body['answers'][0]['code'] == 'print("my answer")'
    assert 'PRIVATE' not in detail.text
    assert 'ai_grading' not in detail.text
    assert client.get(f'{API}/exam/{submission_id}', headers=stranger).status_code == 404
    stranger_list = client.get(API, headers=stranger).json()
    assert stranger_list['total'] == 0
    assert stranger_list['courses'] == []


def test_expired_started_exam_is_excluded_without_auto_submission_or_any_write(client, db_session_factory, history_owner):
    owner, headers, _ = history_owner
    submission_id, _ = seed_exam(db_session_factory, owner, visible=True, status='started')
    writes = []
    engine = db_session_factory.kw['bind']
    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split(' ', 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert client.get(API, headers=headers).json()['total'] == 0
        assert client.get(f'{API}/exam/{submission_id}', headers=headers).status_code == 404
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert writes == []
    with db_session_factory() as db:
        saved = db.get(ExamSubmission, submission_id)
        assert saved.status == 'started'
        assert saved.submitted_at is None


def test_assignment_history_survives_course_removal_without_judge_secrets(client, db_session_factory, history_owner):
    owner, headers, stranger = history_owner
    submission_id = make_submission(db_session_factory, student_username=owner.username,
                                    code='my submitted code', grading_status='completed', score=90)
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        course_id = submission.question.assignment.course_id
        # Shared factories can already create the course enrollment projection.
        enrollment = db.scalar(select(CourseEnrollment).where(CourseEnrollment.course_id == course_id,
                                                              CourseEnrollment.student_id == owner.id))
        if enrollment is None:
            enrollment = CourseEnrollment(course_id=course_id, student_id=owner.id)
            db.add(enrollment)
        enrollment.status, enrollment.origin = 'dropped', 'manual'
        membership = db.scalar(select(OfferingEnrollment).where(
            OfferingEnrollment.offering_id == submission.question.assignment.offering_id,
            OfferingEnrollment.student_id == owner.id))
        assert membership is not None
        membership.status, membership.active_scope_key = 'removed', None
        db.commit()
    response = client.get(f'{API}/assignment/{submission_id}', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['code'] == 'my submitted code'
    assert response.json()['score'] == 90
    assert not {'stdout', 'stderr', 'hidden_tests', 'test_results'} & response.json().keys()
    assert client.get(f'{API}/assignment/{submission_id}', headers=stranger).status_code == 404
    with db_session_factory() as db:
        row = db.scalar(select(CourseEnrollment).where(CourseEnrollment.student_id == owner.id))
        assert row.status == 'dropped'
        assert db.scalar(select(OfferingEnrollment).where(OfferingEnrollment.student_id == owner.id)).status == 'removed'


def test_experiment_history_uses_snapshot_and_removes_hidden_cells_and_outputs(client, db_session_factory, history_owner):
    owner, headers, stranger = history_owner
    module_id = make_experiment_module(db_session_factory)
    version_id = make_template_version(db_session_factory, cells=[
        {'id': 'visible', 'type': 'code', 'order': 1, 'source': 'template source'},
        {'id': 'hidden', 'type': 'code', 'order': 2, 'source_hidden': True, 'source': 'secret'},
    ])
    with db_session_factory() as db:
        record = ExperimentRecord(module_id=module_id, template_version_id=version_id, student_id=owner.id,
                                  cells_sources={'visible': 'later draft'}, cells_outputs={'visible': 'later output'})
        db.add(record)
        db.flush()
        submission = ExperimentSubmission(record_id=record.id, cells_snapshot={
            'visible': 'submitted source', 'hidden': 'PRIVATE SOURCE'},
            outputs_snapshot={'visible': {'outputs': []}, 'hidden': {'private': 'PRIVATE OUTPUT'}},
            score=99, feedback='PRIVATE UNREVIEWED FEEDBACK')
        db.add(submission)
        db.commit()
        submission_id = submission.id
    response = client.get(f'{API}/experiment/{submission_id}', headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['cells'] == [{'id': 'visible', 'type': 'code', 'source': 'submitted source', 'output': {'outputs': []}}]
    assert body['score'] is None
    assert body['feedback'] is None
    assert 'PRIVATE' not in response.text
    assert client.get(f'{API}/experiment/{submission_id}', headers=stranger).status_code == 404
    with db_session_factory() as db:
        db.get(ExperimentSubmission, submission_id).reviewed_at = utc_now()
        db.get(ExperimentSubmission, submission_id).feedback = 'Published feedback'
        db.commit()
    body = client.get(f'{API}/experiment/{submission_id}', headers=headers).json()
    assert body['score'] == 99
    assert body['feedback'] == 'Published feedback'
