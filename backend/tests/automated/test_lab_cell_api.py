import json
from uuid import uuid4

import pytest

from app.models import AcademicTerm, ExperimentRecord, ExperimentSubmission, User
from app.models.lab import LabAssessment, LabRecord, LabRun, LabSubmission, LabRegradeBatch
from app.models.teaching import CourseOffering, OfferingEnrollment, OfferingExperiment
from app.services.lab_assessment_service import bind_new_record
from app.services.lab_grading import publish_evaluation, refresh_regrade_batch
from app.services.lab_processing import process_run
from app.services.lab_queue import claim_run
from app.services.lab_runtime import RuntimeResult
from lab_test_support import headers, make_case, record, save


@pytest.fixture()
def cell_case(db_session_factory, test_settings):
    case = make_case(db_session_factory, test_settings)
    with db_session_factory() as db:
        assessment = db.get(LabAssessment, case.assessment_id)
        assessment.config = {**assessment.config, 'check_script': '',
            'reference_cells': [], 'criteria': [{'id': 'point', 'source': 'cell', 'name': 'numeric answer',
                'description': 'calculate result', 'max_score': 100, 'levels': []}],
            'cell_checks': [{'id': 'point', 'cell_id': 'code', 'kind': 'numeric_close', 'variable': 'result',
                'expected_value': 918273, 'tolerance': 0.001}]}
        db.commit()
    return case


def test_cell_check_snapshot_idempotency_privacy_and_stale_revision(client, db_session_factory, test_settings, cell_case, monkeypatch):
    row = record(client, cell_case, test_settings)
    user = headers(cell_case.student, test_settings)
    url = f"/api/v1/labs/records/{row['id']}/cells/code/check"
    payload = {'client_request_id': str(uuid4()), 'expected_record_revision': row['record_revision']}
    response = client.post(url, headers=user, json=payload)
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    assert client.post(url, headers=user, json=payload).json()['id'] == run_id
    assert client.post(url, headers=headers(cell_case.other_student, test_settings), json=payload).status_code == 404
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.snapshot['cells'] == [{'id': 'code', 'type': 'code', 'source': 'print(1)', 'student_editable': True}]
        claimed = claim_run(db, test_settings)
    def training(**kwargs):
        assert 'expected_value' not in json.dumps(kwargs['observations'])
        output = kwargs['work_dir']
        output.mkdir(parents=True)
        (output / '.platform-cell-evidence.json').write_text(json.dumps({'code': {'completed': True,
            'stdout': 'passed=true', 'values': {'point': {'value': 918273}}}}))
        return RuntimeResult(0, '', 1, output, .5)
    monkeypatch.setattr('app.services.lab_processing.execute_training', training)
    process_run(db_session_factory, *claimed, test_settings)
    fetched = client.get(f'/api/v1/labs/runs/{run_id}', headers=user)
    assert fetched.json()['result']['cell_checks']['point']['passed'] is True
    assert '918273' not in fetched.text
    assert 'values' not in fetched.text and 'observations' not in fetched.text
    save(client, row['id'], row['record_revision'], cell_case.student, test_settings, 'result=2')
    assert client.get(f'/api/v1/labs/runs/{run_id}', headers=user).json()['stale'] is True
    with db_session_factory() as db:
        assert db.query(ExperimentSubmission).count() == 0


def _published(db, row, student, assessment_id, score, *, attempt=1):
    from app.services.lab_assessment_service import record_snapshot
    submission = ExperimentSubmission(record_id=row.id, attempt_number=attempt, cells_snapshot={})
    db.add(submission)
    db.flush()
    state = LabSubmission(submission_id=submission.id, assessment_id=assessment_id, submission_hash='x')
    db.add(state)
    run = LabRun(owner_id=student.id, kind='evaluate', record_id=row.id, submission_id=submission.id,
        assessment_id=assessment_id, status='succeeded', result={'program_elapsed_seconds': 2.0}, snapshot=record_snapshot(db, row))
    db.add(run)
    db.flush()
    state.current_run_id = run.id
    db.flush()
    publish_evaluation(db, run, {'total': score, 'items': [], 'metrics': {}, 'feedback': ''})
    return submission.id


def test_board_has_classmates_under_student_query_policy_but_no_other_class_or_identifiers(client, db_session_factory, test_settings, cell_case):
    own = record(client, cell_case, test_settings)
    other = record(client, cell_case, test_settings, cell_case.other_student)
    with db_session_factory() as db:
        classmate = User(username='classmate-no-leak', real_name='Classmate real name', role='student', status='active', password_hash='test')
        db.add(classmate)
        db.flush()
        offering = db.get(CourseOffering, cell_case.offering_id)
        db.add(OfferingEnrollment(offering_id=offering.id, student_id=classmate.id, status='active',
            active_scope_key=f'{offering.course_id}:{offering.academic_term_id}:{classmate.id}'))
        mate = ExperimentRecord(offering_id=offering.id, scope_key=f'offering:{offering.id}', module_id=cell_case.module_id,
            student_id=classmate.id, template_version_id=cell_case.version_id, environment_version_id=cell_case.environment_id)
        db.add(mate)
        db.flush()
        bind_new_record(db, mate)
        _published(db, db.get(ExperimentRecord, own['id']), cell_case.student, cell_case.assessment_id, 88)
        _published(db, mate, classmate, cell_case.assessment_id, 88)
        _published(db, db.get(ExperimentRecord, other['id']), cell_case.other_student, cell_case.assessment_id, 99)
        db.commit()
    url = f"/api/v1/labs/records/{own['id']}/leaderboard"
    response = client.get(url, headers=headers(cell_case.student, test_settings))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['named'] is False and len(data['items']) == 2
    assert [r['rank'] for r in data['items']] == [1, 1]
    assert all(r['score'] == 88 for r in data['items'])
    assert sum(r['is_me'] for r in data['items']) == 1
    assert 'Classmate real name' not in response.text and 'classmate-no-leak' not in response.text
    assert not {'student_id', 'username', 'submission_id', 'run_id'} & set(data['items'][0])
    assert client.get(url, headers=headers(cell_case.other_teacher, test_settings)).status_code == 404
    with db_session_factory() as db:
        db.get(CourseOffering, cell_case.offering_id).leaderboard_named = True
        db.commit()
    named = client.get(url, headers=headers(cell_case.student, test_settings)).json()
    assert named['named'] and any(r['display_name'] == 'Classmate real name' for r in named['items'])


def test_python_script_upload_is_validated_but_never_executed(client, cell_case, test_settings):
    endpoint = f'/api/v1/labs/entries/module/{cell_case.module_id}/assessment/check-script'
    teacher = headers(cell_case.teacher, test_settings)
    script = 'raise RuntimeError("executing uploads would fail")\n'
    response = client.post(endpoint, headers=teacher, files={'file': ('check.py', script, 'text/plain')})
    assert response.status_code == 200 and response.json()['script'] == script
    assert client.post(endpoint, headers=teacher, files={'file': ('check.py', 'bad python {', 'text/plain')}).status_code == 422
    assert client.post(endpoint, headers=teacher, files={'file': ('check.py', b'x' * 20001, 'text/plain')}).status_code == 422
    assert client.post(endpoint, headers=headers(cell_case.student, test_settings), files={'file': ('check.py', script, 'text/plain')}).status_code == 403


def test_global_regrade_cannot_bypass_class_history_and_only_admin_can_confirm_legacy(client, db_session_factory, test_settings, cell_case):
    own = record(client, cell_case, test_settings)
    with db_session_factory() as db:
        ident = _published(db, db.get(ExperimentRecord, own['id']), cell_case.student, cell_case.assessment_id, 88)
        db.commit()
    endpoint = f'/api/v1/labs/entries/module/{cell_case.module_id}/assessment/regrade'
    payload = {'submission_ids': [ident], 'reason': 'Verify historical rubric'}
    for actor in (cell_case.teacher, cell_case.admin):
        rejected = client.post(endpoint, headers=headers(actor, test_settings), json=payload)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()['detail']['code'] == 'LAB_CLASS_REGRADING_REQUIRED'
    with db_session_factory() as db:
        assert db.query(LabRegradeBatch).count() == 0
        assert db.get(ExperimentSubmission, ident).score == 88
        historical = db.get(ExperimentRecord, own['id'])
        historical.offering_id, historical.scope_key = None, 'legacy'
        db.commit()
    # Check the service guard independently of the ORM privacy filter: even an
    # authorized content owner cannot assume a historical student's class.
    from fastapi import HTTPException
    from app.api.labs import batch_regrade
    from app.schemas.lab import LabRegradeRequest
    with db_session_factory() as db:
        with pytest.raises(HTTPException) as rejected:
            batch_regrade('module', cell_case.module_id, LabRegradeRequest(**payload), db, cell_case.teacher)
        assert rejected.value.status_code == 403
        assert rejected.value.detail['code'] == 'LEGACY_SCOPE_UNASSIGNED'
        db.rollback()
    confirmed = client.post(endpoint, headers=headers(cell_case.admin, test_settings), json=payload)
    assert confirmed.status_code == 202, confirmed.text
    with db_session_factory() as db:
        batch = db.get(LabRegradeBatch, confirmed.json()['id'])
        assert batch.offering_id is None and batch.owner_id == cell_case.admin.id
        assert batch.submission_ids == [ident]
        assert db.get(ExperimentSubmission, ident).score == 88


@pytest.mark.parametrize('ids', [[1, 1], [0], [-1]])
def test_regrade_rejects_duplicate_or_invalid_ids_before_enqueuing(client, test_settings, cell_case, ids):
    endpoint = f'/api/v1/offerings/{cell_case.offering_id}/experiments/module/{cell_case.module_id}/assessment/regrade'
    result = client.post(endpoint, headers=headers(cell_case.teacher, test_settings),
        json={'submission_ids': ids, 'reason': 'Correct rubric'})
    assert result.status_code == 422, result.text


def test_reassignment_revokes_baseline_and_batch_access_even_from_original_queue_owner(client, db_session_factory, test_settings, cell_case):
    own = record(client, cell_case, test_settings)
    with db_session_factory() as db:
        ident = _published(db, db.get(ExperimentRecord, own['id']), cell_case.student, cell_case.assessment_id, 88)
        assessment = LabAssessment(owner_id=cell_case.teacher.id,
            entry_key=f'offering:{cell_case.offering_id}:module:{cell_case.module_id}', version=1,
            status='draft', config={}, template_version_id=cell_case.version_id)
        db.add(assessment)
        db.flush()
        run = LabRun(owner_id=cell_case.teacher.id, kind='baseline', assessment_id=assessment.id,
            status='queued', logs='PRIVATE CLASS REFERENCE', snapshot={})
        batch = LabRegradeBatch(offering_id=cell_case.offering_id, owner_id=cell_case.teacher.id,
            assessment_id=assessment.id, submission_ids=[ident], reason='Check class history')
        db.add_all([run, batch])
        db.commit()
        run_id, batch_id = run.id, batch.id
    teacher = headers(cell_case.teacher, test_settings)
    assert client.get(f'/api/v1/labs/runs/{run_id}', headers=teacher).status_code == 200
    assert client.get(f'/api/v1/labs/regrade-batches/{batch_id}', headers=teacher).status_code == 200
    assert client.get(f'/api/v1/labs/runs/{run_id}', headers=headers(cell_case.student, test_settings)).status_code == 403
    with db_session_factory() as db:
        db.get(CourseOffering, cell_case.offering_id).teacher_id = cell_case.other_teacher.id
        db.commit()
    assert client.get(f'/api/v1/labs/runs/{run_id}', headers=teacher).status_code == 403
    assert client.post(f'/api/v1/labs/runs/{run_id}/cancel', headers=teacher).status_code == 403
    assert client.get(f'/api/v1/labs/regrade-batches/{batch_id}', headers=teacher).status_code == 403
    with db_session_factory() as db:
        assert db.get(LabRun, run_id).status == 'queued'
        assert db.get(LabRun, run_id).cancel_requested is False
    assert client.get(f'/api/v1/labs/runs/{run_id}', headers=headers(cell_case.admin, test_settings)).status_code == 200


@pytest.mark.parametrize('withdrawal', ['term', 'class', 'offering', 'teacher', 'account'])
def test_late_single_regrade_retains_automatic_evidence_without_writing_readonly_scores(client, db_session_factory, test_settings, cell_case, withdrawal):
    from app.models import TeachingClass
    own = record(client, cell_case, test_settings)
    with db_session_factory() as db:
        row = db.get(ExperimentRecord, own['id'])
        ident = _published(db, row, cell_case.student, cell_case.assessment_id, 88)
        state = db.get(LabSubmission, ident)
        previous_grade_id = state.published_grade_id
        run = LabRun(owner_id=cell_case.teacher.id, kind='evaluate', record_id=row.id,
            submission_id=ident, assessment_id=cell_case.assessment_id, status='succeeded',
            result={'total': 98, 'items': [], 'metrics': {}, 'feedback': 'new evidence'}, snapshot={})
        db.add(run)
        db.flush()
        state.current_run_id = run.id
        offering = db.get(CourseOffering, row.offering_id)
        if withdrawal == 'term': db.get(AcademicTerm, offering.academic_term_id).status = 'closed'
        if withdrawal == 'class': db.get(TeachingClass, offering.teaching_class_id).status = 'archived'
        if withdrawal == 'offering': offering.status = 'archived'
        if withdrawal == 'teacher': offering.teacher_id = cell_case.other_teacher.id
        if withdrawal == 'account': db.get(User, cell_case.teacher.id).status = 'disabled'
        db.commit()
        run_id = run.id
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        evidence = publish_evaluation(db, run, run.result)
        db.commit()
        assert evidence.total == 98 and evidence.published_at is None
        assert run.status == 'needs_review' and run.error_code == 'LAB_PUBLICATION_SCOPE_CHANGED'
        assert db.get(ExperimentSubmission, ident).score == 88
        assert db.get(LabSubmission, ident).published_grade_id == previous_grade_id
        assert db.get(LabSubmission, ident).review_requested is True


def test_worker_finishing_after_term_close_keeps_result_for_review_and_releases_resources(client, db_session_factory, test_settings, cell_case, monkeypatch):
    from app.models.lab import LabGradeRevision, LabResourceLease
    own = record(client, cell_case, test_settings)
    created = client.post(f"/api/v1/experiments/records/{own['id']}/submit",
        headers=headers(cell_case.student, test_settings),
        json={'client_request_id': str(uuid4()), 'expected_record_revision': own['record_revision']})
    assert created.status_code == 201, created.text
    ident = created.json()['id']
    with db_session_factory() as db:
        claimed = claim_run(db, test_settings)
    def training(**kwargs):
        with db_session_factory() as db:
            offering = db.get(CourseOffering, cell_case.offering_id)
            db.get(AcademicTerm, offering.academic_term_id).status = 'closed'
            db.commit()
        output = kwargs['work_dir']
        output.mkdir(parents=True)
        (output / '.platform-cell-evidence.json').write_text(json.dumps({'code': {'completed': True,
            'stdout': '', 'values': {'point': {'value': 918273}}}}))
        return RuntimeResult(0, '', 1, output, .5)
    monkeypatch.setattr('app.services.lab_processing.execute_training', training)
    process_run(db_session_factory, *claimed, test_settings)
    with db_session_factory() as db:
        state = db.get(LabSubmission, ident)
        run = db.get(LabRun, state.current_run_id)
        assert run.status == 'needs_review' and run.result['score'] == 100
        assert state.published_grade_id is None and state.review_requested
        assert db.get(ExperimentSubmission, ident).score is None
        evidence = db.query(LabGradeRevision).filter_by(run_id=run.id).one()
        assert evidence.total == 100 and evidence.published_at is None
        assert db.get(LabResourceLease, f'run:{run.id}') is None


@pytest.mark.parametrize('withdrawal', [None, 'teacher', 'account', 'term', 'offering', 'pending', 'history', 'run_version', 'grade_version'])
def test_class_regrade_rechecks_authority_and_atomically_switches_only_current_pending(client, db_session_factory, test_settings, cell_case, withdrawal):
    own = record(client, cell_case, test_settings)
    with db_session_factory() as db:
        row = db.get(ExperimentRecord, own['id'])
        _published(db, row, cell_case.student, cell_case.assessment_id, 88)
        binding = db.get(LabRecord, row.id)
        old = db.get(LabAssessment, cell_case.assessment_id)
        target = LabAssessment(owner_id=cell_case.teacher.id,
            entry_key=f'offering:{cell_case.offering_id}:module:{cell_case.module_id}', version=1,
            status='published', config=old.config, template_version_id=old.template_version_id)
        db.add(target)
        db.flush()
        delivery = db.query(OfferingExperiment).filter_by(offering_id=cell_case.offering_id).one()
        delivery.pending_assessment_id = target.id
        submission = db.get(ExperimentSubmission, binding.selected_submission_id)
        batch = LabRegradeBatch(offering_id=cell_case.offering_id, assessment_id=target.id,
            owner_id=cell_case.teacher.id, reason='correcting rubric', submission_ids=[submission.id], status='running')
        db.add(batch)
        db.flush()
        run = LabRun(owner_id=cell_case.teacher.id, kind='evaluate', record_id=row.id,
            submission_id=submission.id, assessment_id=target.id, status='succeeded', snapshot={'batch_id': batch.id})
        db.add(run)
        db.flush()
        state = db.get(LabSubmission, submission.id)
        state.current_run_id = run.id
        db.flush()
        staged = publish_evaluation(db, run, {'total': 98, 'items': [], 'metrics': {}, 'feedback': 'new'})
        assert staged.published_at is None and submission.score == 88
        offering = db.get(CourseOffering, cell_case.offering_id)
        if withdrawal == 'teacher': offering.teacher_id = cell_case.other_teacher.id
        if withdrawal == 'account': db.get(User, cell_case.teacher.id).status = 'disabled'
        if withdrawal == 'term': db.get(AcademicTerm, offering.academic_term_id).status = 'closed'
        if withdrawal == 'offering': offering.status = 'archived'
        if withdrawal == 'pending': delivery.pending_assessment_id = None
        if withdrawal == 'run_version': run.assessment_id = cell_case.assessment_id
        if withdrawal == 'grade_version': staged.assessment_id = cell_case.assessment_id
        if withdrawal == 'history':
            # Simulate an administrative import after the batch's history IDs
            # were frozen. It must retain the old publication for the class.
            _published(db, row, cell_case.student, cell_case.assessment_id, 91, attempt=2)
        db.commit()
        batch_id, target_id, submission_id = batch.id, target.id, submission.id
    with db_session_factory() as db:
        refresh_regrade_batch(db, batch_id)
        db.commit()
        batch, submission = db.get(LabRegradeBatch, batch_id), db.get(ExperimentSubmission, submission_id)
        delivery = db.query(OfferingExperiment).filter_by(offering_id=cell_case.offering_id).one()
        if withdrawal:
            assert batch.status == 'needs_review'
            assert submission.score == 88 and delivery.assessment_id == cell_case.assessment_id
        else:
            assert batch.status == 'succeeded'
            assert submission.score == 98 and delivery.assessment_id == target_id
            assert delivery.pending_assessment_id is None


def _class_config(client, case, settings):
    endpoint = f'/api/v1/offerings/{case.offering_id}/experiments/module/{case.module_id}/assessment'
    teacher = headers(case.teacher, settings)
    current = client.get(endpoint, headers=teacher)
    assert current.status_code == 200, current.text
    config = current.json()['published']['config']
    config['requirements'] = 'This class uses a distinct result target'
    config['cell_checks'][0]['expected_value'] = 7
    config['reference_cells'] = [{'id': 'code', 'type': 'code', 'source': 'result=7', 'student_editable': True}]
    return endpoint, teacher, config


def _complete_class_baseline(client, factory, settings, endpoint, teacher, monkeypatch):
    first = client.post(endpoint + '/baseline', headers=teacher)
    assert first.status_code == 202, first.text
    assert client.post(endpoint + '/baseline', headers=teacher).json()['id'] == first.json()['id']
    def training(**kwargs):
        output = kwargs['work_dir']
        output.mkdir(parents=True)
        (output / '.platform-cell-evidence.json').write_text(json.dumps({'code': {'completed': True,
            'stdout': '', 'values': {'point': {'value': 7}}}}))
        return RuntimeResult(0, '', 1, output, .5)
    monkeypatch.setattr('app.services.lab_processing.execute_training', training)
    with factory() as db:
        claimed = claim_run(db, settings)
    process_run(factory, *claimed, settings)
    assert client.get(endpoint, headers=teacher).json()['draft']['validated'] is True


def test_class_assessment_is_private_versioned_and_requires_unchanged_baseline(client, db_session_factory, test_settings, cell_case, monkeypatch):
    endpoint, teacher, config = _class_config(client, cell_case, test_settings)
    stranger = headers(cell_case.other_teacher, test_settings)
    assert client.get(endpoint, headers=stranger).status_code == 403
    assert client.put(endpoint, headers=stranger, json=config).status_code == 403
    saved = client.put(endpoint, headers=teacher, json=config)
    assert saved.status_code == 200, saved.text
    target_id = saved.json()['id']
    assert target_id != cell_case.assessment_id
    assert client.post(endpoint + '/publish', headers=teacher).status_code == 422
    _complete_class_baseline(client, db_session_factory, test_settings, endpoint, teacher, monkeypatch)
    config['requirements'] = 'Changed after the validated baseline'
    changed = client.put(endpoint, headers=teacher, json=config)
    assert changed.status_code == 200 and changed.json()['validated'] is False
    assert client.post(endpoint + '/publish', headers=teacher).status_code == 422
    _complete_class_baseline(client, db_session_factory, test_settings, endpoint, teacher, monkeypatch)
    published = client.post(endpoint + '/publish', headers=teacher)
    assert published.status_code == 200, published.text
    own = record(client, cell_case, test_settings)
    other = record(client, cell_case, test_settings, cell_case.other_student)
    with db_session_factory() as db:
        assert db.get(LabRecord, own['id']).assessment_id == target_id
        assert db.get(LabRecord, other['id']).assessment_id == cell_case.assessment_id
    public = client.get(f"/api/v1/labs/records/{own['id']}/overview", headers=headers(cell_case.student, test_settings))
    assert 'expected_value' not in public.text and 'reference_cells' not in public.text


def test_class_pending_standard_keeps_grades_until_entire_history_regrade_is_authorized(client, db_session_factory, test_settings, cell_case, monkeypatch):
    own = record(client, cell_case, test_settings)
    with db_session_factory() as db:
        row = db.get(ExperimentRecord, own['id'])
        first = _published(db, row, cell_case.student, cell_case.assessment_id, 88)
        second = _published(db, row, cell_case.student, cell_case.assessment_id, 92, attempt=2)
        db.commit()
    endpoint, teacher, config = _class_config(client, cell_case, test_settings)
    saved = client.put(endpoint, headers=teacher, json=config)
    assert saved.status_code == 200, saved.text
    _complete_class_baseline(client, db_session_factory, test_settings, endpoint, teacher, monkeypatch)
    assert client.post(endpoint + '/publish', headers=teacher).status_code == 200
    with db_session_factory() as db:
        delivery = db.query(OfferingExperiment).filter_by(offering_id=cell_case.offering_id).one()
        assert delivery.assessment_id == cell_case.assessment_id
        assert delivery.pending_assessment_id == saved.json()['id']
        assert db.get(ExperimentSubmission, second).score == 92

    learner = headers(cell_case.student, test_settings)
    formal = client.post(f"/api/v1/experiments/records/{own['id']}/submit", headers=learner,
        json={'client_request_id': str(uuid4()), 'expected_record_revision': own['record_revision']})
    assert formal.status_code == 409, formal.text
    assert formal.json()['detail']['code'] == 'ASSESSMENT_SWITCH_PENDING'
    # The service boundary enforces the same rule when called independently of
    # the route; rejected attempts consume neither history entries nor quota.
    from fastapi import HTTPException
    from app.schemas import ExperimentSubmitRequest
    from app.services.lab_submission_service import submit_lab
    with db_session_factory() as db:
        with pytest.raises(HTTPException) as rejected:
            submit_lab(db, db.get(ExperimentRecord, own['id']), cell_case.student,
                ExperimentSubmitRequest(client_request_id=uuid4(), expected_record_revision=own['record_revision']), test_settings)
        assert rejected.value.detail['code'] == 'ASSESSMENT_SWITCH_PENDING'
        db.rollback()
        assert db.query(ExperimentSubmission).count() == 2
        assert db.get(ExperimentSubmission, second).score == 92
    practice = client.post(f"/api/v1/labs/records/{own['id']}/cells/code/check", headers=learner,
        json={'client_request_id': str(uuid4()), 'expected_record_revision': own['record_revision']})
    assert practice.status_code == 202, practice.text

    partial = client.post(endpoint + '/regrade', headers=teacher, json={'submission_ids': [second], 'reason': 'new class standard'})
    assert partial.status_code == 422 and partial.json()['detail']['code'] == 'LAB_FULL_HISTORY_REQUIRED'
    whole = client.post(endpoint + '/regrade', headers=teacher, json={'submission_ids': [first, second], 'reason': 'new class standard'})
    assert whole.status_code == 202, whole.text
    with db_session_factory() as db:
        assert db.get(LabRegradeBatch, whole.json()['id']).offering_id == cell_case.offering_id
        assert db.get(ExperimentSubmission, second).score == 92

def test_first_automatic_class_standard_preserves_existing_manual_grade_without_stuck_pending(client, db_session_factory, test_settings, cell_case, monkeypatch):
    endpoint, teacher, config = _class_config(client, cell_case, test_settings)
    with db_session_factory() as db:
        delivery = db.query(OfferingExperiment).filter_by(offering_id=cell_case.offering_id).one()
        delivery.assessment_id = None
        db.commit()
    old = record(client, cell_case, test_settings)
    with db_session_factory() as db:
        manual = ExperimentSubmission(record_id=old['id'], attempt_number=1, score=91, cells_snapshot={'code': 'result=7'})
        db.add(manual)
        db.commit()
        manual_id = manual.id
    saved = client.put(endpoint, headers=teacher, json=config)
    assert saved.status_code == 200, saved.text
    _complete_class_baseline(client, db_session_factory, test_settings, endpoint, teacher, monkeypatch)
    assert client.post(endpoint + '/publish', headers=teacher).status_code == 200
    with db_session_factory() as db:
        delivery = db.query(OfferingExperiment).filter_by(offering_id=cell_case.offering_id).one()
        assert delivery.assessment_id == saved.json()['id'] and delivery.pending_assessment_id is None
        assert db.get(LabRecord, old['id']) is None
        assert db.get(ExperimentSubmission, manual_id).score == 91
        newcomer = User(username='new-class-student', real_name='New student', role='student', status='active', password_hash='test')
        db.add(newcomer)
        db.flush()
        offering = db.get(CourseOffering, cell_case.offering_id)
        db.add(OfferingEnrollment(offering_id=offering.id, student_id=newcomer.id, status='active', active_scope_key=f'{offering.course_id}:{offering.academic_term_id}:{newcomer.id}'))
        db.commit()
    new_record = record(client, cell_case, test_settings, newcomer)
    with db_session_factory() as db:
        assert db.get(LabRecord, new_record['id']).assessment_id == saved.json()['id']
