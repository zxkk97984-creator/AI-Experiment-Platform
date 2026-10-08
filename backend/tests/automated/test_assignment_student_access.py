"""One student audience boundary protects assignment metadata and execution."""
import pytest
from sqlalchemy import func, select

from app.api import judge
from app.models import (Assignment, AssignmentAudienceClass, AssignmentAudienceStudent,
                        Course, JudgeQuestion, Submission)
from app.services import judge_queue
from test_school_offerings import school  # noqa: F401


@pytest.fixture()
def assignment_case(school, db_session_factory, monkeypatch, redis_client):  # noqa: F811
    calls = []

    def run(*args, **kwargs):
        calls.append('sample')
        return '1 passed', '', 0, 1

    monkeypatch.setattr(judge, '_run_docker_pytest', run)
    monkeypatch.setattr(judge_queue, '_get_redis', lambda: redis_client)
    with db_session_factory() as db:
        task = Assignment(course_id=school['course'], offering_id=school['offerings'][0],
                          title='受众作业', status='published')
        db.add(task); db.flush()
        question = JudgeQuestion(assignment_id=task.id, title='仅受众可见题面', function_name='f',
                                 hidden_tests='private-check', public_cases=[{'args': [], 'expected': 1}],
                                 grading_mode='legacy')
        db.add(question); db.commit()
        return {**school, 'assignment': task.id, 'question': question.id, 'executions': calls}


def request_all(client, case, *, actor='a'):
    h = case['headers'][actor]
    aid, qid = case['assignment'], case['question']
    return [
        client.get(f'/api/v1/assignments/{aid}', headers=h),
        client.get(f'/api/v1/assignments/{aid}/questions', headers=h),
        client.post(f'/api/v1/judge/questions/{qid}/sample-run', headers=h,
                    json={'question_id': qid, 'code': 'def f(): return 1'}),
        client.post('/api/v1/judge/submissions', headers=h,
                    json={'question_id': qid, 'code': 'def f(): return 1'}),
    ]


@pytest.mark.parametrize('restriction', ['excluded', 'whitelist', 'other_class', 'draft_task', 'draft_course'])
def test_all_assignment_endpoints_deny_outside_audience_without_execution(client, db_session_factory, assignment_case, restriction):
    case = assignment_case
    with db_session_factory() as db:
        task = db.get(Assignment, case['assignment'])
        if restriction == 'excluded':
            db.add(AssignmentAudienceStudent(assignment_id=task.id, student_id=case['users']['a'].id, kind='exclude'))
        elif restriction == 'whitelist':
            task.audience_mode = 'whitelist_only'
        elif restriction == 'draft_task':
            task.status = 'draft'
        elif restriction == 'draft_course':
            db.get(Course, case['course']).status = 'draft'
        db.commit()
    responses = request_all(client, case, actor='b' if restriction == 'other_class' else 'a')
    expected = 404 if restriction == 'other_class' else 403
    expected_codes = [expected] * 4
    if restriction == 'draft_task':
        expected_codes[-1] = 400  # Existing submission API compatibility.
    assert [r.status_code for r in responses] == expected_codes, [r.text for r in responses]
    if restriction in ('excluded', 'whitelist'):
        assert {r.json()['detail']['code'] for r in responses} == {'NOT_IN_ASSIGNMENT_AUDIENCE'}
    assert case['executions'] == []
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Submission)) == 0


@pytest.mark.parametrize('audience', ['all_enrolled', 'whitelist_only', 'selected_classes'])
def test_assignment_audience_members_can_read_test_and_submit(client, db_session_factory, assignment_case, audience):
    case = assignment_case
    with db_session_factory() as db:
        task = db.get(Assignment, case['assignment'])
        task.audience_mode = audience
        if audience == 'whitelist_only':
            db.add(AssignmentAudienceStudent(assignment_id=task.id, student_id=case['users']['a'].id, kind='include'))
        elif audience == 'selected_classes':
            db.add(AssignmentAudienceClass(assignment_id=task.id, teaching_class_id=case['classes'][0]))
        db.commit()
    responses = request_all(client, case)
    assert [r.status_code for r in responses] == [200, 200, 200, 201], [r.text for r in responses]
    assert all('private-check' not in r.text for r in responses)
    assert case['executions'] == ['sample']
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Submission)) == 1
