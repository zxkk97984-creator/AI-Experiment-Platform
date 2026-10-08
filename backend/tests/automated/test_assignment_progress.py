import pytest

from app.models import Assignment, AssignmentAudienceStudent, JudgeQuestion, Submission
from test_assignment_student_access import assignment_case  # noqa: F401
from test_school_offerings import school  # noqa: F401


def test_progress_is_assignment_scoped_latest_by_id_and_success_is_historical(client, db_session_factory, assignment_case):
    case = assignment_case
    with db_session_factory() as db:
        student = case['users']['a'].id
        first = Submission(question_id=case['question'], student_id=student, code='old success', status='accepted')
        db.add(first); db.flush()
        latest = Submission(question_id=case['question'], student_id=student, code='latest failure', status='wrong_answer')
        empty = JudgeQuestion(assignment_id=case['assignment'], title='empty', function_name='f', hidden_tests='')
        other = Assignment(course_id=case['course'], offering_id=case['offerings'][0], title='other', status='published')
        db.add_all([latest, empty, other]); db.flush()
        question = JudgeQuestion(assignment_id=other.id, title='other', function_name='f', hidden_tests='')
        db.add(question); db.flush()
        db.add_all([Submission(question_id=question.id, student_id=student, code='unrelated', status='accepted') for _ in range(105)])
        db.commit()
        expected = [{'question_id': case['question'], 'latest_submission_id': latest.id, 'status': 'wrong_answer', 'completed': True},
                    {'question_id': empty.id, 'latest_submission_id': None, 'status': None, 'completed': False}]
    response = client.get(f"/api/v1/assignments/{case['assignment']}/my-progress", headers=case['headers']['a'])
    assert response.status_code == 200, response.text
    assert response.json() == {'items': expected}
    assert 'code' not in response.text


@pytest.mark.parametrize('actor,exclude,expected', [('b', False, 404), ('a', True, 403), ('owner', False, 403)])
def test_progress_obeys_student_and_audience_boundary(client, db_session_factory, assignment_case, actor, exclude, expected):
    case = assignment_case
    if exclude:
        with db_session_factory() as db:
            db.add(AssignmentAudienceStudent(assignment_id=case['assignment'], student_id=case['users']['a'].id, kind='exclude'))
            db.commit()
    response = client.get(f"/api/v1/assignments/{case['assignment']}/my-progress", headers=case['headers'][actor])
    assert response.status_code == expected, response.text
