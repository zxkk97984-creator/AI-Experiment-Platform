import copy
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import (ExperimentModule, ExperimentRecord, ExperimentSubmission, LabAssessment,
    LabDatasetVersion, LabFile, LabGradeRevision, LabRecord, LabRegradeBatch, LabRun, LabSubmission,
    NotebookTemplate)
from app.models.teaching import OfferingExperiment
from app.services.lab_data_service import register_dataset_upload, validate_dataset
from app.services.lab_grading import publish_evaluation, refresh_regrade_batch
from app.services.lab_processing import process_run
from app.services.lab_queue import claim_run
from app.services.lab_runtime import RuntimeResult
from lab_test_support import complete, headers, make_case, record, save


@pytest.fixture()
def lab_case(db_session_factory, test_settings):
    return make_case(db_session_factory, test_settings)


def submit(client, record_id, revision, user, settings, *, key=None, artifact_id=None):
    payload = {"client_request_id": key or str(uuid4()), "expected_record_revision": revision}
    if artifact_id: payload["artifact_id"] = artifact_id
    return client.post(f"/api/v1/experiments/records/{record_id}/submit", json=payload, headers=headers(user, settings))


def test_draft_baseline_publish_and_old_record_compatibility(client, db_session_factory, test_settings, monkeypatch):
    case = make_case(db_session_factory, test_settings, published=False)
    old_record = record(client, case, test_settings)
    endpoint = f"/api/v1/labs/entries/module/{case.module_id}/assessment"
    teacher = headers(case.teacher, test_settings)
    response = client.put(endpoint, json=case.config, headers=teacher)
    assert response.status_code == 200, response.text
    draft = response.json()
    with db_session_factory() as db:
        assert db.get(NotebookTemplate, case.template_id).current_version_id == case.version_id
    assert client.post(endpoint + "/publish", headers=teacher).status_code == 422
    baseline = client.post(endpoint + "/baseline", headers=teacher)
    assert baseline.status_code == 202, baseline.text
    def run(**kwargs):
        kwargs["work_dir"].mkdir()
        return RuntimeResult(0, "ready", 1, kwargs["work_dir"])
    def checker(**kwargs):
        out = kwargs["output_dir"]
        out.mkdir()
        (out / "checks.json").write_text('{"method":{"passed":true,"message":"ok"}}')
        return RuntimeResult(0, "", 1, out)
    monkeypatch.setattr("app.services.lab_processing.execute_training", run)
    monkeypatch.setattr("app.services.lab_processing.execute_check", checker)
    with db_session_factory() as db:
        claimed = claim_run(db, test_settings)
    process_run(db_session_factory, *claimed, test_settings)
    same = client.put(endpoint, json=case.config, headers=teacher)
    assert same.status_code == 200, same.text
    assert same.json()["id"] == draft["id"] and same.json()["validated"] is True
    published = client.post(endpoint + "/publish", headers=teacher)
    assert published.status_code == 200, published.text
    old_again = record(client, case, test_settings)
    assert old_again["id"] == old_record["id"]
    overview = client.get(f"/api/v1/labs/records/{old_record['id']}/overview", headers=headers(case.student, test_settings))
    assert overview.json() == {"enabled": False}
    # A different class can adopt the new published standard explicitly;
    # opening a record never silently follows the global newest assessment.
    released = client.post(f'/api/v1/offerings/{case.other_offering_id}/experiments',
        json={'entry_kind': 'module', 'entry_id': case.module_id, 'assessment_id': published.json()['id'], 'status': 'published'}, headers=teacher)
    assert released.status_code == 200, released.text
    new = record(client, case, test_settings, case.other_student)
    overview = client.get(f"/api/v1/labs/records/{new['id']}/overview", headers=headers(case.other_student, test_settings))
    assert overview.status_code == 200 and overview.json()["enabled"] is True
    public_config = overview.json()["assessment"]["config"]
    assert "reference_cells" not in public_config and "check_script" not in public_config
    assert "REFERENCE_" not in overview.text


def test_template_change_invalidates_publish(client, db_session_factory, test_settings):
    case = make_case(db_session_factory, test_settings, published=False)
    endpoint = f"/api/v1/labs/entries/module/{case.module_id}/assessment"
    response = client.put(endpoint, json=case.config, headers=headers(case.teacher, test_settings))
    assert response.status_code == 200, response.text
    with db_session_factory() as db:
        draft = db.get(LabAssessment, response.json()["id"])
        draft.validated_hash = draft.config_hash
        template = db.get(NotebookTemplate, case.template_id)
        template.draft_cells = [{"id": "code", "type": "code", "source": "changed"}]
        db.commit()
    response = client.post(endpoint + "/publish", headers=headers(case.teacher, test_settings))
    assert response.status_code == 409


def test_train_idempotency_revision_one_active_and_ownership(client, lab_case, test_settings):
    row = record(client, lab_case, test_settings)
    url = f"/api/v1/labs/records/{row['id']}/train"
    payload = {"client_request_id": str(uuid4()), "expected_record_revision": row["record_revision"]}
    user = headers(lab_case.student, test_settings)
    first = client.post(url, json=payload, headers=user)
    assert first.status_code == 202, first.text
    assert client.post(url, json=payload, headers=user).json()["id"] == first.json()["id"]
    assert client.post(url, json={**payload, "client_request_id": str(uuid4())}, headers=user).status_code == 409
    assert client.post(url, json=payload, headers=headers(lab_case.other_student, test_settings)).status_code == 404
    assert client.get(f"/api/v1/labs/runs/{first.json()['id']}", headers=headers(lab_case.other_teacher, test_settings)).status_code == 404
    assert client.post(f"/api/v1/labs/runs/{first.json()['id']}/cancel", headers=user).status_code == 200
    changed = save(client, row["id"], row["record_revision"], lab_case.student, test_settings)
    assert client.post(url, json={**payload, "client_request_id": str(uuid4())}, headers=user).status_code == 409
    assert client.post(url, json={**payload, "client_request_id": str(uuid4()), "expected_record_revision": changed}, headers=user).status_code == 202


def test_submission_revisions_idempotency_quotas_and_highest_score(client, db_session_factory, test_settings, lab_case):
    row = record(client, lab_case, test_settings)
    revision = row["record_revision"]
    wrong = submit(client, row["id"], revision + 1, lab_case.student, test_settings)
    assert wrong.status_code == 409
    first_id = None
    for attempt, score in enumerate([95, 60, 70, 80, 85], 1):
        revision = save(client, row["id"], revision, lab_case.student, test_settings, f"print({attempt})")
        key = str(uuid4())
        response = submit(client, row["id"], revision, lab_case.student, test_settings, key=key)
        assert response.status_code == 201, response.text
        ident = response.json()["id"]
        first_id = first_id or ident
        repeated = submit(client, row["id"], revision, lab_case.student, test_settings, key=key)
        assert repeated.status_code == 201 and repeated.json()["id"] == ident
        complete(db_session_factory, ident, score)
        revision += 1
    sixth = submit(client, row["id"], revision, lab_case.student, test_settings)
    assert sixth.status_code == 409 and sixth.json()["detail"]["code"] == "LAB_ATTEMPTS_EXHAUSTED"
    overview = client.get(f"/api/v1/labs/records/{row['id']}/overview", headers=headers(lab_case.student, test_settings))
    assert overview.json()["attempts_used"] == 5
    assert overview.json()["score"] == 95 and overview.json()["selected_submission_id"] == first_id


def test_deadline_rejects_new_work_but_accepted_work_can_finish(client, db_session_factory, test_settings, lab_case):
    row = record(client, lab_case, test_settings)
    accepted = submit(client, row["id"], row["record_revision"], lab_case.student, test_settings)
    assert accepted.status_code == 201
    with db_session_factory() as db:
        db.get(ExperimentModule, lab_case.module_id).due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    complete(db_session_factory, accepted.json()["id"], 88)
    rejected = submit(client, row["id"], row["record_revision"] + 1, lab_case.student, test_settings)
    assert rejected.status_code == 403 and rejected.json()["detail"]["code"] == "LAB_DEADLINE_PASSED"


def test_teacher_override_wins_over_late_automatic_completion(client, db_session_factory, test_settings, lab_case):
    row = record(client, lab_case, test_settings)
    submission = submit(client, row["id"], row["record_revision"], lab_case.student, test_settings).json()
    with db_session_factory() as db:
        old_run_id = db.get(LabSubmission, submission["id"]).current_run_id
    url = f"/api/v1/labs/submissions/{submission['id']}/review"
    response = client.post(url, json={"score": 82, "feedback": "Reviewed", "reason": "Verified training evidence"},
        headers=headers(lab_case.teacher, test_settings))
    assert response.status_code == 200, response.text
    assert client.post(url, json={"score": 80, "reason": ""}, headers=headers(lab_case.teacher, test_settings)).status_code == 422
    with db_session_factory() as db:
        run = db.get(LabRun, old_run_id)
        assert publish_evaluation(db, run, {"total": 20, "items": [], "metrics": {}, "feedback": "late"}) is None
        db.commit()
        assert db.get(ExperimentSubmission, submission["id"]).score == 82
        assert db.get(LabSubmission, submission["id"]).current_run_id is None


def test_latest_selection_can_choose_lower_new_score(client, db_session_factory, test_settings, lab_case):
    with db_session_factory() as db:
        assessment = db.get(LabAssessment, lab_case.assessment_id)
        assessment.config = {**assessment.config, "grade_selection": "latest"}
        db.commit()
    row = record(client, lab_case, test_settings)
    first = submit(client, row["id"], row["record_revision"], lab_case.student, test_settings).json()["id"]
    complete(db_session_factory, first, 95)
    revision = save(client, row["id"], row["record_revision"] + 1, lab_case.student, test_settings)
    second = submit(client, row["id"], revision, lab_case.student, test_settings).json()["id"]
    complete(db_session_factory, second, 60)
    overview = client.get(f"/api/v1/labs/records/{row['id']}/overview", headers=headers(lab_case.student, test_settings)).json()
    assert overview["score"] == 60 and overview["selected_submission_id"] == second


def add_dataset(factory, settings, case):
    rows = "sample_id,x,label,split\n" + "".join(f"s{i},{i},{'a' if i%2 else 'b'},{split}\n"
        for i, split in enumerate(["train", "train", "validation", "validation", "test", "test"], 1))
    with factory() as db:
        data = register_dataset_upload(db, case.teacher.id, "Dataset", "tabular_classification",
            {"split_column": "split"}, "data.csv", io.BytesIO(rows.encode()), settings)
        validate_dataset(db, data.id, settings)
        assert data.status == "ready", data.error
        assessment = db.get(LabAssessment, case.assessment_id)
        assessment.config = {**assessment.config, "task_type": "tabular_classification", "dataset_id": data.id}
        db.commit()
        return data.id


def test_dataset_and_hidden_labels_remain_authorized(client, db_session_factory, test_settings, lab_case):
    data_id = add_dataset(db_session_factory, test_settings, lab_case)
    row = record(client, lab_case, test_settings)
    with db_session_factory() as db:
        files = {f.role: f.id for f in db.scalars(select(LabFile).where(LabFile.dataset_version_id == data_id))}
    student = headers(lab_case.student, test_settings)
    assert client.get(f"/api/v1/labs/files/{files['train_labels']}", headers=student).status_code == 200
    assert client.get(f"/api/v1/labs/files/{files['validation_labels']}", headers=student).status_code == 200
    for role in ("test_input", "test_labels"):
        assert client.get(f"/api/v1/labs/files/{files[role]}", headers=student).status_code == 403
    assert client.get(f"/api/v1/labs/files/{files['train_labels']}", headers=headers(lab_case.other_student, test_settings)).status_code == 403
    assert client.get(f"/api/v1/labs/datasets/{data_id}", headers=headers(lab_case.other_teacher, test_settings)).status_code == 404
    overview = client.get(f"/api/v1/labs/records/{row['id']}/overview", headers=student).json()
    assert all(f["role"] == "public_bundle" or f["role"].startswith(("train_", "validation_")) for f in overview["dataset"]["files"])
    assert len(overview["dataset"]["files"]) <= 6
    assert client.get(f"/api/v1/labs/files/{files['public_bundle']}", headers=student).status_code == 200


def test_external_artifact_freezes_real_code_not_later_notebook(client, db_session_factory, test_settings, lab_case):
    add_dataset(db_session_factory, test_settings, lab_case)
    row = record(client, lab_case, test_settings)
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("train.py", "ACTUAL_TRAINING_CODE = 123")
        archive.writestr("predict.py", "# actual prediction implementation")
        archive.writestr("model.json", '{"weight":2}')
    response = client.post(f"/api/v1/labs/records/{row['id']}/artifacts",
        data={"expected_record_revision": str(row["record_revision"])},
        files={"file": ("artifact.zip", zipped.getvalue(), "application/zip")},
        headers=headers(lab_case.student, test_settings))
    assert response.status_code == 202, response.text
    artifact_id = response.json()["id"]
    with db_session_factory() as db:
        claimed = claim_run(db, test_settings)
    process_run(db_session_factory, *claimed, test_settings)
    file_url = f"/api/v1/labs/artifacts/{artifact_id}/files"
    page = client.get(file_url + '?page_size=1', headers=headers(lab_case.student, test_settings))
    assert page.status_code == 200 and len(page.json()['items']) == 1
    next_page = client.get(file_url + '?page_size=1&page=2', headers=headers(lab_case.student, test_settings))
    assert next_page.json()['items'][0]['id'] != page.json()['items'][0]['id']
    assert client.get(file_url, headers=headers(lab_case.other_student, test_settings)).status_code == 403
    assert client.get(file_url, headers=headers(lab_case.teacher, test_settings)).status_code == 200
    revision = save(client, row["id"], row["record_revision"], lab_case.student, test_settings, "LATER_UNTRAINED_CODE = 456")
    submitted = submit(client, row["id"], revision, lab_case.student, test_settings, artifact_id=artifact_id)
    assert submitted.status_code == 201, submitted.text
    with db_session_factory() as db:
        state = db.get(LabSubmission, submitted.json()["id"])
        run = db.get(LabRun, state.current_run_id)
        assert run.snapshot["source_files"]["train.py"] == "ACTUAL_TRAINING_CODE = 123"
        assert "LATER_UNTRAINED_CODE" not in json.dumps(run.snapshot)
        assert run.snapshot["source"] == "external"


def test_runtime_visible_to_admin_while_disabled(app, client, db_session_factory, test_settings, lab_case):
    from app.config import get_settings
    app.dependency_overrides[get_settings] = lambda: test_settings.model_copy(update={"lab_enabled": False})
    response = client.get("/api/v1/labs/runtime", headers=headers(lab_case.admin, test_settings))
    assert response.status_code == 200 and response.json()["enabled"] is False
    assert client.get("/api/v1/labs/datasets", headers=headers(lab_case.teacher, test_settings)).status_code == 503


def test_overview_is_bounded_and_details_load_logs_separately(client, db_session_factory, test_settings, lab_case):
    row = record(client, lab_case, test_settings)
    with db_session_factory() as db:
        for _ in range(25):
            run = LabRun(owner_id=lab_case.student.id, record_id=row["id"], assessment_id=lab_case.assessment_id,
                kind="train", status="succeeded", logs="x" * 8192, result={"curves": [{"epoch": 1, "train_loss": .5}], "artifact_id": 123})
            db.add(run)
        db.commit()
        last_id = run.id
    user = headers(lab_case.student, test_settings)
    response = client.get(f"/api/v1/labs/records/{row['id']}/overview", headers=user)
    overview = response.json()
    assert len(overview["runs"]) == 20
    assert all(item["logs"] == "" and "curves" not in item["result"] for item in overview["runs"])
    detail = client.get(f"/api/v1/labs/runs/{last_id}", headers=user).json()
    assert len(detail["logs"]) == 8192 and detail["result"]["curves"]


def test_reference_upload_returns_pollable_validation_run(client, db_session_factory, test_settings, lab_case):
    endpoint = f"/api/v1/labs/entries/module/{lab_case.module_id}"
    teacher = headers(lab_case.teacher, test_settings)
    response = client.put(endpoint + "/assessment", json=lab_case.config, headers=teacher)
    assert response.status_code == 200, response.text
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("train.py", "print('reference')")
    response = client.post(endpoint + "/reference-artifacts", files={"file": ("reference.zip", zipped.getvalue(), "application/zip")}, headers=teacher)
    assert response.status_code == 202, response.text
    run_id = response.json()["validation_run_id"]
    assert client.get(f"/api/v1/labs/runs/{run_id}", headers=teacher).status_code == 200
    assert client.get(f"/api/v1/labs/runs/{run_id}", headers=headers(lab_case.student, test_settings)).status_code == 403


def test_batch_regrade_keeps_old_standard_until_whole_history_complete(client, db_session_factory, test_settings, lab_case, monkeypatch):
    row = record(client, lab_case, test_settings)
    first = submit(client, row["id"], row["record_revision"], lab_case.student, test_settings).json()["id"]
    complete(db_session_factory, first, 95)
    revision = save(client, row["id"], row["record_revision"] + 1, lab_case.student, test_settings)
    second = submit(client, row["id"], revision, lab_case.student, test_settings).json()["id"]
    complete(db_session_factory, second, 80)
    teacher = headers(lab_case.teacher, test_settings)
    assessment_url = f"/api/v1/offerings/{lab_case.offering_id}/experiments/module/{lab_case.module_id}/assessment"
    revised = copy.deepcopy(lab_case.config)
    revised['criteria'][0]['description'] = 'Corrected public method requirement'
    draft = client.put(assessment_url, json=revised, headers=teacher)
    assert draft.status_code == 200, draft.text
    assert draft.json()['template_version_id'] == lab_case.version_id
    def train(**kwargs):
        kwargs['work_dir'].mkdir()
        return RuntimeResult(0, '', 1, kwargs['work_dir'])
    def check(**kwargs):
        kwargs['output_dir'].mkdir()
        (kwargs['output_dir'] / 'checks.json').write_text('{"method":{"passed":true}}')
        return RuntimeResult(0, '', 1, kwargs['output_dir'])
    monkeypatch.setattr('app.services.lab_processing.execute_training', train)
    monkeypatch.setattr('app.services.lab_processing.execute_check', check)
    assert client.post(assessment_url + '/baseline', headers=teacher).status_code == 202
    with db_session_factory() as db:
        claim = claim_run(db, test_settings)
    process_run(db_session_factory, *claim, test_settings)
    published = client.post(assessment_url + '/publish', headers=teacher)
    assert published.status_code == 200, published.text
    target_id = published.json()['id']
    url = assessment_url + '/regrade'
    incomplete = client.post(url, json={"submission_ids": [first], "reason": "Corrected threshold"}, headers=teacher)
    assert incomplete.status_code == 422
    response = client.post(url, json={"submission_ids": [first, second], "reason": "Corrected threshold"}, headers=teacher)
    assert response.status_code == 202, response.text
    batch_id = response.json()["id"]
    batch_url = f"/api/v1/labs/regrade-batches/{batch_id}"
    status = client.get(batch_url, headers=teacher)
    assert status.status_code == 200 and status.json()['total'] == 2
    assert client.get(batch_url, headers=headers(lab_case.other_teacher, test_settings)).status_code == 404
    assert client.get(batch_url, headers=headers(lab_case.student, test_settings)).status_code == 403
    for index, (submission_id, score) in enumerate([(first, 60), (second, 85)]):
        complete(db_session_factory, submission_id, score)
        with db_session_factory() as db:
            refresh_regrade_batch(db, batch_id)
            db.commit()
            binding = db.get(LabRecord, row["id"])
            if index == 0:
                assert binding.assessment_id == lab_case.assessment_id
                assert db.get(ExperimentSubmission, first).score == 95
            else:
                assert binding.assessment_id == target_id
                assert binding.selected_submission_id == second
                assert db.get(LabRegradeBatch, batch_id).status == "succeeded"


def test_batch_refresh_never_reuses_stale_state_over_manual_grade(client, db_session_factory, test_settings, lab_case):
    from app.schemas.lab import LabReviewRequest
    from app.services.lab_grading import manual_review
    row = record(client, lab_case, test_settings)
    ident = submit(client, row['id'], row['record_revision'], lab_case.student, test_settings).json()['id']
    complete(db_session_factory, ident, 50)
    with db_session_factory() as db:
        delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == lab_case.offering_id))
        delivery.pending_assessment_id = lab_case.assessment_id
        batch = LabRegradeBatch(offering_id=lab_case.offering_id, assessment_id=lab_case.assessment_id, owner_id=lab_case.teacher.id,
            status='running', reason='Recheck', submission_ids=[ident])
        db.add(batch); db.flush()
        run = LabRun(owner_id=lab_case.teacher.id, record_id=row['id'], assessment_id=lab_case.assessment_id,
            submission_id=ident, kind='evaluate', status='succeeded', snapshot={'batch_id': batch.id})
        db.add(run); db.flush()
        db.get(LabSubmission, ident).current_run_id = run.id
        grade = LabGradeRevision(submission_id=ident, assessment_id=lab_case.assessment_id, run_id=run.id,
            source='automatic', total=10, items=[], metrics={}, feedback='Staged')
        db.add(grade); db.commit()
        batch_id = batch.id
    with db_session_factory() as stale:
        stale.get(LabRegradeBatch, batch_id)
        old_state = stale.get(LabSubmission, ident)
        assert old_state.current_run_id is not None
        with db_session_factory() as fresh:
            manual_review(fresh, fresh.get(ExperimentSubmission, ident), lab_case.teacher,
                LabReviewRequest(score=92, reason='Human verified', feedback='Final'))
            fresh.commit()
        refresh_regrade_batch(stale, batch_id)
        stale.commit()
    with db_session_factory() as db:
        assert db.get(ExperimentSubmission, ident).score == 92
        assert db.get(LabSubmission, ident).current_run_id is None
        assert db.get(LabRegradeBatch, batch_id).status == 'needs_review'


def test_parallel_training_retry_is_idempotent(db_session_factory, test_settings, lab_case, client):
    from concurrent.futures import ThreadPoolExecutor
    from app.schemas.lab import LabTrainRequest
    from app.services.lab_submission_service import enqueue_training
    row=record(client,lab_case,test_settings)
    payload=LabTrainRequest(client_request_id=uuid4(),expected_record_revision=row['record_revision'])
    def request():
        with db_session_factory() as db:
            run=enqueue_training(db,db.get(ExperimentRecord,row['id']),lab_case.student,payload,test_settings)
            db.commit()
            return run.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids=list(pool.map(lambda _:request(),range(2)))
    assert ids[0]==ids[1]
