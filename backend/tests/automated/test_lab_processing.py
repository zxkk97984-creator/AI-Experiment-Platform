import copy
import csv
import io
import json
from pathlib import Path

import pytest

from app.models import User
from app.models.lab import LabAssessment, LabRun
from app.services.lab_data_service import register_dataset_upload, validate_dataset
from app.services.lab_processing import normalise_cells, process_run, read_curves
from app.services.lab_queue import claim_run
from app.services.lab_runtime import RuntimeFailure, RuntimeResult


def test_markdown_not_executed_and_sources_normalised():
    result = normalise_cells([{"id": "report", "type": "markdown", "source": "# Analysis"},
        {"id": "code", "type": "code", "source": ["print(", "1)"]}])
    assert result[0]["cell_type"] == "markdown"
    assert result[1]["source"] == "print(1)"


def test_curves_reject_nonfinite_or_unbounded_data(tmp_path):
    target = tmp_path / "metrics.json"
    target.write_text('[{"epoch":1,"train_loss":NaN}]')
    assert read_curves(tmp_path) == []
    target.write_text('[{"epoch":1,"train_loss":0.2,"secret":"ignored"}]')
    assert read_curves(tmp_path) == [{"epoch": 1, "train_loss": 0.2}]
    target.write_text('[{"epoch":true,"train_loss":0.2}]')
    assert read_curves(tmp_path) == []


@pytest.fixture()
def processing_case(db_session_factory, test_settings, tmp_path):
    settings = test_settings.model_copy(update={"judge_work_dir": str(tmp_path / "work"),
        "judge_host_work_dir": "", "lab_min_free_disk_mb": 64})
    with db_session_factory() as db:
        owner = User(username="lab-processing-teacher", real_name="Teacher", role="teacher", password_hash="test")
        db.add(owner)
        db.flush()
        rows = "sample_id,x,label,split\n" + "".join(f"s{i},{i},{'a' if i % 2 else 'b'},{split}\n"
            for i, split in enumerate(["train", "train", "validation", "validation", "test", "test"], 1))
        dataset = register_dataset_upload(db, owner.id, "test data", "tabular_classification",
            {"split_column": "split"}, "samples.csv", io.BytesIO(rows.encode()), settings)
        validate_dataset(db, dataset.id, settings)
        assert dataset.status == "ready", dataset.error
        config = {"task_type": "tabular_classification", "dataset_id": dataset.id,
            "resources": {"cpus": 1, "memory_mb": 1024, "timeout_seconds": 60, "disk_mb": 64},
            "evaluation_resources": {"cpus": 1, "memory_mb": 1024, "timeout_seconds": 60, "disk_mb": 64},
            "metric": "macro_f1", "zero_baseline": 0, "full_target": 1,
            "criteria": [{"id": "model", "source": "metric", "name": "Model accuracy", "max_score": 100}],
            "reference_cells": [{"id": "a", "type": "code", "source": "print('training')"}], "check_script": ""}
        assessment = LabAssessment(owner_id=owner.id, entry_key="module:999", config=config,
            config_hash="a" * 64, version=1, status="draft")
        db.add(assessment)
        db.flush()
        run = LabRun(owner_id=owner.id, kind="baseline", assessment_id=assessment.id,
            dataset_id=dataset.id, resources=config["resources"], snapshot={"image_ref": "sha256:" + "a" * 64,
                "assessment_hash": "a" * 64, "assessment_config": copy.deepcopy(config)})
        db.add(run)
        db.commit()
        claimed = claim_run(db, settings)
        return settings, claimed, assessment.id


def test_hidden_labels_never_reach_prediction_and_metrics_ignore_self_reports(
        db_session_factory, processing_case, monkeypatch):
    settings, (run_id, token), assessment_id = processing_case
    phases = []
    def training(**kwargs):
        course = kwargs["course_dir"]
        assert (course / "train" / "labels.csv").is_file()
        assert (course / "validation" / "labels.csv").is_file()
        assert not (course / "test").exists()
        out = kwargs["work_dir"]
        out.mkdir()
        (out / "predict.py").write_text("# predictor")
        (out / "metrics.json").write_text('{"macro_f1":1.0}')
        phases.append("train")
        return RuntimeResult(0, "accuracy=1.0 (not trusted)", 1, out)
    def predict(**kwargs):
        inputs = kwargs["input_dir"]
        assert not (inputs / "labels.csv").exists()
        assert not (inputs.parent / "scorer").exists()
        out = kwargs["output_dir"]
        out.mkdir()
        rows = list(csv.DictReader((inputs / "samples.csv").open()))
        with (out / "predictions.csv").open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["sample_id", "prediction"])
            for row in rows:
                writer.writerow([row["sample_id"], "a"])
        phases.append("predict")
        return RuntimeResult(0, "", 1, out)
    monkeypatch.setattr("app.services.lab_processing.execute_training", training)
    monkeypatch.setattr("app.services.lab_processing.execute_prediction", predict)
    process_run(db_session_factory, run_id, token, settings)
    assert phases == ["train", "predict"]
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.status == "succeeded", run.error_message
        assert run.result["metrics"]["accuracy"] == 0.5
        assert run.result["score"] < 100
        assert run.result["artifact_id"]
        assert db.get(LabAssessment, assessment_id).validated_hash == "a" * 64


def test_infrastructure_retries_without_zero_score(db_session_factory, processing_case, monkeypatch):
    settings, (run_id, token), _ = processing_case
    def unavailable(**kwargs):
        raise RuntimeFailure("sandbox unavailable", "SANDBOX_UNAVAILABLE", infrastructure=True)
    monkeypatch.setattr("app.services.lab_processing.execute_training", unavailable)
    process_run(db_session_factory, run_id, token, settings)
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.status == "queued"
        assert run.lease_token is None
        assert "score" not in run.result
        next_claim = claim_run(db, settings)
        assert next_claim[0] == run_id and next_claim[1] != token


def test_model_notebook_cells_and_independent_metrics_are_scored_fifty_fifty(db_session_factory, processing_case, monkeypatch):
    settings, (run_id, token), assessment_id = processing_case
    with db_session_factory() as db:
        assessment, run = db.get(LabAssessment, assessment_id), db.get(LabRun, run_id)
        config = {**assessment.config, 'require_platform_training': True, 'allow_external': False,
            'criteria': [{'id': 'model', 'source': 'metric', 'name': 'model', 'max_score': 50},
                {'id': 'cell', 'source': 'cell', 'name': 'Notebook result', 'max_score': 50}],
            'cell_checks': [{'id': 'cell', 'cell_id': 'a', 'kind': 'stdout', 'expected_output': 'correct'}]}
        assessment.config = config
        run.snapshot = {**run.snapshot, 'assessment_config': copy.deepcopy(config)}
        db.commit()
    phases = []
    def training(**kwargs):
        assert kwargs['cells'][0]['id'] == 'a'
        output = kwargs['work_dir']
        output.mkdir(parents=True)
        (output / 'predict.py').write_text('# model entry')
        (output / '.platform-cell-evidence.json').write_text(json.dumps({'a': {'completed': True, 'stdout': 'correct', 'values': {}}}))
        phases.append('train')
        return RuntimeResult(0, '', 1, output, .5)
    def predict(**kwargs):
        output = kwargs['output_dir']
        output.mkdir(parents=True)
        rows = list(csv.DictReader((kwargs['input_dir'] / 'samples.csv').open()))
        with (output / 'predictions.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['sample_id', 'prediction'])
            for row in rows: writer.writerow([row['sample_id'], 'a'])
        phases.append('predict')
        return RuntimeResult(0, '', 1, output, .25)
    monkeypatch.setattr('app.services.lab_processing.execute_training', training)
    monkeypatch.setattr('app.services.lab_processing.execute_prediction', predict)
    process_run(db_session_factory, run_id, token, settings)
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.status == 'succeeded', run.error_message
        assert phases == ['train', 'predict', 'train']
        assert run.result['score'] == pytest.approx(66.67)
        assert run.result['cell_checks']['cell']['passed'] is True


def test_student_execution_failure_requires_review_not_zero(db_session_factory, processing_case, monkeypatch):
    settings, (run_id, token), _ = processing_case
    monkeypatch.setattr("app.services.lab_processing.execute_training",
        lambda **kwargs: RuntimeResult(1, "ValueError", 1, kwargs["work_dir"]))
    process_run(db_session_factory, run_id, token, settings)
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.status == "needs_review"
        assert "score" not in run.result


def test_cleanup_failure_keeps_resources_for_recovery(db_session_factory, processing_case, monkeypatch):
    from app.models.lab import LabResourceLease
    settings, (run_id, token), _ = processing_case
    def unsafe_cleanup(**kwargs):
        raise RuntimeFailure("cleanup uncertain", "SANDBOX_CLEANUP_FAILED", infrastructure=True)
    monkeypatch.setattr("app.services.lab_processing.execute_training", unsafe_cleanup)
    with pytest.raises(RuntimeFailure, match="cleanup uncertain"):
        process_run(db_session_factory, run_id, token, settings)
    with db_session_factory() as db:
        assert db.get(LabResourceLease, f"run:{run_id}") is not None
        assert db.get(LabRun, run_id).status == "running"
