"""Administrative history confirmation keeps original scores and identities."""
from uuid import uuid4

from sqlalchemy import select

from app.models import (Assignment, ExperimentRecord, ExperimentSubmission, JudgeQuestion,
                        Submission)
from app.models.teaching import OfferingEnrollment, OfferingExperiment
from conftest import make_template_version
from test_school_offerings import school  # noqa: F401


def test_history_review_is_admin_only_and_confirmation_preserves_score(client, school, db_session_factory):  # noqa: F811
    version = make_template_version(db_session_factory)
    with db_session_factory() as db:
        record = ExperimentRecord(lesson_id=school['lessons'][0], template_version_id=version,
                                  student_id=school['users']['a'].id, status='completed')
        db.add(record); db.flush()
        score = ExperimentSubmission(record_id=record.id, score=71.5, feedback='原评语')
        db.add(score); db.commit(); rid, sid = record.id, score.id
    offering = school['offerings'][0]
    route = f'/api/v1/offerings/{offering}'
    assert client.get(route + '/legacy-review', headers=school['headers']['owner']).status_code == 403
    reviewed = client.get(route + '/legacy-review', headers=school['headers']['admin'])
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()['items'][0]['can_confirm'] is True
    wrong = client.post(f"/api/v1/offerings/{school['offerings'][1]}/legacy-records/{rid}/assign",
                        json={'reason': '未核对名单'}, headers=school['headers']['admin'])
    assert wrong.status_code == 422
    assert client.post(route + f'/legacy-records/{rid}/assign', json={'reason': '核对原名册'}, headers=school['headers']['admin']).status_code == 200
    with db_session_factory() as db:
        assert db.get(ExperimentRecord, rid).offering_id == offering
        assert (db.get(ExperimentSubmission, sid).score, db.get(ExperimentSubmission, sid).feedback) == (71.5, '原评语')


def test_history_task_requires_all_participants_in_one_confirmed_class(client, school, db_session_factory):  # noqa: F811
    with db_session_factory() as db:
        task = Assignment(course_id=school['course'], title='历史混班作业', status='published')
        db.add(task); db.flush()
        question = JudgeQuestion(assignment_id=task.id, title='题目', function_name='f', hidden_tests='')
        db.add(question); db.flush()
        scores = [Submission(question_id=question.id, student_id=school['users'][who].id,
                   code='pass', status='completed', grading_status='completed', score=score)
                  for who, score in [('a', 70), ('b', 90)]]
        db.add_all(scores); db.commit(); tid, ids = task.id, [row.id for row in scores]
    route = f"/api/v1/offerings/{school['offerings'][0]}"
    reviewed = client.get(route + '/legacy-review', headers=school['headers']['admin'])
    assert reviewed.json()['tasks'][0]['unmatched'] == 1
    rejected = client.post(route + f'/legacy-tasks/assignment/{tid}/assign', json={'reason': '不能猜测归属'}, headers=school['headers']['admin'])
    assert rejected.status_code == 409
    with db_session_factory() as db:
        assert db.get(Assignment, tid).offering_id is None
        assert [db.get(Submission, sid).score for sid in ids] == [70, 90]
        # A separate unambiguous source can be confirmed; mixed-class facts remain intact.
        same_class = Assignment(course_id=school['course'], title='同班历史作业', status='published')
        db.add(same_class); db.flush()
        own_question = JudgeQuestion(assignment_id=same_class.id, title='题目', function_name='f', hidden_tests='')
        db.add(own_question); db.flush()
        own_score = Submission(question_id=own_question.id, student_id=school['users']['a'].id,
            code='pass', status='completed', grading_status='completed', score=75)
        db.add(own_score); db.commit(); own_tid, own_sid = same_class.id, own_score.id
    accepted = client.post(route + f'/legacy-tasks/assignment/{own_tid}/assign', json={'reason': '核对同班历史来源'}, headers=school['headers']['admin'])
    assert accepted.status_code == 200, accepted.text
    assert client.post(route + f'/legacy-tasks/assignment/{own_tid}/assign', json={'reason': '不能覆盖'}, headers=school['headers']['admin']).status_code == 409
    with db_session_factory() as db:
        assert db.get(Submission, own_sid).score == 75
        assert [db.get(Submission, sid).score for sid in ids] == [70, 90]
        assert db.get(Assignment, tid).offering_id is None


def test_transfer_does_not_move_score_without_separate_admin_confirmation(client, school, db_session_factory):  # noqa: F811
    version = make_template_version(db_session_factory)
    source, target = school['offerings']
    with db_session_factory() as db:
        record = ExperimentRecord(lesson_id=school['lessons'][0], template_version_id=version,
            student_id=school['users']['a'].id, status='completed', offering_id=source, scope_key=f'offering:{source}')
        db.add(record); db.flush()
        score = ExperimentSubmission(record_id=record.id, score=85)
        db.add(score)
        db.add(OfferingExperiment(offering_id=target, entry_kind='lesson', entry_id=record.lesson_id,
            template_version_id=version, status='published'))
        db.commit(); rid, sid = record.id, score.id
    admin = school['headers']['admin']
    transfer = client.post(f"/api/v1/offerings/{source}/students/{school['users']['a'].id}/transfer",
        json={'destination_offering_id': target, 'reason': '转入二班'}, headers=admin)
    assert transfer.status_code == 200
    with db_session_factory() as db:
        assert db.get(ExperimentRecord, rid).offering_id == source
    route = f'/api/v1/offerings/{target}/records/{rid}/transfer'
    payload = {'source_offering_id': source, 'reason': '核对同模板同评分版本，正式转入成绩'}
    assert client.post(route, json=payload, headers=school['headers']['other']).status_code == 403
    accepted = client.post(route, json=payload, headers=admin)
    assert accepted.status_code == 200, accepted.text
    with db_session_factory() as db:
        assert db.get(ExperimentRecord, rid).offering_id == target
        assert db.get(ExperimentSubmission, sid).score == 85
    assert client.post(route, json=payload, headers=admin).status_code == 409


def test_removed_student_cannot_create_or_retry_formal_submission(client, school, db_session_factory):  # noqa: F811
    from teaching_test_support import grant_experiment
    from conftest import auth_header, login
    from test_experiment_submission_idempotency import _setup_experiment
    setup = _setup_experiment(client, db_session_factory)
    with db_session_factory() as db:
        record = db.get(ExperimentRecord, setup['rid'])
        member = db.scalar(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == record.offering_id,
            OfferingEnrollment.student_id == record.student_id))
        member.status, member.active_scope_key = 'removed', None
        db.commit()
    response = client.post(f"/api/v1/experiments/records/{setup['rid']}/submit", json={'client_request_id': str(uuid4())},
                           headers=auth_header(login(client, 'esub_s')[0]))
    assert response.status_code == 403
    with db_session_factory() as db:
        assert not db.scalar(select(ExperimentSubmission.id).where(ExperimentSubmission.record_id == setup['rid']))
