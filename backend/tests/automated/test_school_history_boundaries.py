"""Historical confirmation is bounded, refuses moving work, and preserves facts."""
import pytest
from sqlalchemy import select

from app.models import (Assignment, AuditEvent, ExperimentRecord, ExperimentSubmission,
    JudgeQuestion, LabAssessment, LabRecord, LabRun, Lesson, Submission)
from app.models.teaching import CourseOffering, OfferingExperiment
from conftest import make_template_version
from test_school_offerings import school  # noqa: F401


@pytest.fixture()
def transferable(client, school, db_session_factory):  # noqa: F811
    version = make_template_version(db_session_factory)
    source, target = school['offerings']
    with db_session_factory() as db:
        record = ExperimentRecord(lesson_id=school['lessons'][0], template_version_id=version,
            student_id=school['users']['a'].id, status='completed', offering_id=source,
            scope_key=f'offering:{source}')
        db.add(record); db.flush()
        score = ExperimentSubmission(record_id=record.id, score=82.25, feedback='保留原始反馈')
        assessment = LabAssessment(entry_key=f"lesson:{record.lesson_id}", version=1,
            owner_id=school['users']['owner'].id, status='published', config={})
        db.add_all([score, assessment]); db.flush()
        db.add(LabRecord(record_id=record.id, assessment_id=assessment.id))
        deliveries = [OfferingExperiment(offering_id=oid, entry_kind='lesson',
            entry_id=record.lesson_id, template_version_id=version, assessment_id=assessment.id,
            status='published') for oid in (source, target)]
        db.add_all(deliveries); db.commit()
        result = {'record': record.id, 'submission': score.id, 'assessment': assessment.id,
                  'deliveries': [row.id for row in deliveries], 'source': source, 'target': target}
    moved = client.post(f"/api/v1/offerings/{source}/students/{school['users']['a'].id}/transfer",
        json={'destination_offering_id': target, 'reason': '先核对名单转班'}, headers=school['headers']['admin'])
    assert moved.status_code == 200, moved.text
    return result


def _assert_original(db_session_factory, setup):
    with db_session_factory() as db:
        record = db.get(ExperimentRecord, setup['record'])
        score = db.get(ExperimentSubmission, setup['submission'])
        assert (record.offering_id, record.scope_key) == (setup['source'], f"offering:{setup['source']}")
        assert (score.score, score.feedback) == (82.25, '保留原始反馈')
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'record.grade.transfer'))


@pytest.mark.parametrize('status', ['queued', 'running', 'recovering'])
def test_grade_transfer_rejects_all_nonterminal_runs_without_changing_score(
        client, school, db_session_factory, transferable, status):  # noqa: F811
    with db_session_factory() as db:
        db.add(LabRun(record_id=transferable['record'], owner_id=school['users']['a'].id,
                      kind='notebook_submit', assessment_id=transferable['assessment'], status=status))
        db.commit()
    response = client.post(f"/api/v1/offerings/{transferable['target']}/records/{transferable['record']}/transfer",
        json={'source_offering_id': transferable['source'], 'reason': '不得移动运行中成绩'}, headers=school['headers']['admin'])
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'GRADE_TRANSFER_BUSY'
    _assert_original(db_session_factory, transferable)


@pytest.mark.parametrize('delivery_index', [0, 1])
def test_grade_transfer_rejects_pending_regrade_in_either_class(
        client, school, db_session_factory, transferable, delivery_index):  # noqa: F811
    with db_session_factory() as db:
        db.get(OfferingExperiment, transferable['deliveries'][delivery_index]).pending_assessment_id = transferable['assessment']
        db.commit()
    response = client.post(f"/api/v1/offerings/{transferable['target']}/records/{transferable['record']}/transfer",
        json={'source_offering_id': transferable['source'], 'reason': '整班重评中保留原归属'}, headers=school['headers']['admin'])
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'GRADE_TRANSFER_BUSY'
    _assert_original(db_session_factory, transferable)


@pytest.mark.parametrize('invalid', ['template', 'assessment', 'archived_source', 'archived_target'])
def test_grade_transfer_requires_matching_versions_and_writable_classes(
        client, school, db_session_factory, transferable, invalid):  # noqa: F811
    replacement = make_template_version(db_session_factory) if invalid == 'template' else None
    with db_session_factory() as db:
        if invalid == 'template':
            db.get(OfferingExperiment, transferable['deliveries'][1]).template_version_id = replacement
        elif invalid == 'assessment':
            db.get(OfferingExperiment, transferable['deliveries'][1]).assessment_id = None
        else:
            ident = transferable['source' if invalid == 'archived_source' else 'target']
            db.get(CourseOffering, ident).status = 'archived'
        db.commit()
    response = client.post(f"/api/v1/offerings/{transferable['target']}/records/{transferable['record']}/transfer",
        json={'source_offering_id': transferable['source'], 'reason': '不能覆盖历史事实'}, headers=school['headers']['admin'])
    assert response.status_code == 409, response.text
    _assert_original(db_session_factory, transferable)


def test_history_pages_are_bounded_for_records_and_tasks(client, school, db_session_factory):  # noqa: F811
    version = make_template_version(db_session_factory)
    with db_session_factory() as db:
        for index in range(21):
            lesson = Lesson(chapter_id=school['chapter'], title=f'历史课时{index:02d}', status='published')
            db.add(lesson); db.flush()
            db.add(ExperimentRecord(lesson_id=lesson.id, template_version_id=version,
                student_id=school['users']['a'].id, status='completed'))
            task = Assignment(course_id=school['course'], title=f'历史作业{index:02d}', status='published')
            db.add(task); db.flush()
            question = JudgeQuestion(assignment_id=task.id, title='题', function_name='f', hidden_tests='')
            db.add(question); db.flush()
            db.add(Submission(question_id=question.id, student_id=school['users']['a'].id, code='pass',
                              status='completed', grading_status='completed', score=70))
        db.commit()
    route = f"/api/v1/offerings/{school['offerings'][0]}/legacy-review"
    first = client.get(route, params={'page': 1, 'page_size': 20}, headers=school['headers']['admin']).json()
    second = client.get(route, params={'page': 2, 'page_size': 20}, headers=school['headers']['admin']).json()
    assert (first['total'], first['task_totals']['assignment']) == (21, 21)
    assert (len(first['items']), len(first['tasks']), len(second['items']), len(second['tasks'])) == (20, 20, 1, 1)
    assert {r['id'] for r in first['items']}.isdisjoint(r['id'] for r in second['items'])
    assert {r['id'] for r in first['tasks']}.isdisjoint(r['id'] for r in second['tasks'])
    assert client.get(route, params={'page': 0}, headers=school['headers']['admin']).status_code == 422


@pytest.mark.parametrize('busy', ['queued', 'running', 'recovering', 'target_pending'])
def test_legacy_assignment_waits_for_running_work_and_target_regrade(
        client, school, db_session_factory, busy):  # noqa: F811
    version = make_template_version(db_session_factory)
    target = school['offerings'][0]
    with db_session_factory() as db:
        record = ExperimentRecord(lesson_id=school['lessons'][0], template_version_id=version,
            student_id=school['users']['a'].id, status='completed')
        db.add(record); db.flush()
        score = ExperimentSubmission(record_id=record.id, score=71.25, feedback='原历史分')
        db.add(score)
        if busy == 'target_pending':
            assessment = LabAssessment(entry_key=f"lesson:{record.lesson_id}", version=1,
                owner_id=school['users']['owner'].id, status='published', config={})
            db.add(assessment); db.flush()
            work = OfferingExperiment(offering_id=target, entry_kind='lesson', entry_id=record.lesson_id,
                template_version_id=version, assessment_id=assessment.id,
                pending_assessment_id=assessment.id, status='published')
        else:
            work = LabRun(record_id=record.id, owner_id=record.student_id, kind='notebook_submit', status=busy)
        db.add(work); db.commit()
        rid, sid, work_id = record.id, score.id, work.id
    route = f'/api/v1/offerings/{target}/legacy-records/{rid}/assign'
    payload = {'reason': '先完成运行和整班重评，再核对历史'}
    rejected = client.post(route, json=payload, headers=school['headers']['admin'])
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()['detail']['code'] == 'HISTORY_SCOPE_BUSY'
    with db_session_factory() as db:
        assert (db.get(ExperimentRecord, rid).offering_id, db.get(ExperimentRecord, rid).scope_key) == (None, 'legacy')
        assert (db.get(ExperimentSubmission, sid).score, db.get(ExperimentSubmission, sid).feedback) == (71.25, '原历史分')
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'record.scope.assign'))
        if busy == 'target_pending':
            db.get(OfferingExperiment, work_id).pending_assessment_id = None
        else:
            db.get(LabRun, work_id).status = 'succeeded'
        db.commit()
    accepted = client.post(route, json=payload, headers=school['headers']['admin'])
    assert accepted.status_code == 200, accepted.text
    with db_session_factory() as db:
        assert db.get(ExperimentRecord, rid).offering_id == target
        assert (db.get(ExperimentSubmission, sid).score, db.get(ExperimentSubmission, sid).feedback) == (71.25, '原历史分')
