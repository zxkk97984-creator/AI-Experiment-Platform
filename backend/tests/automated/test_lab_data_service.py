"""Real CSV/ZIP ingestion, isolation, integrity and lifecycle checks."""
from __future__ import annotations

import csv
from io import BytesIO
from pathlib import Path
import stat
import struct
import zipfile
import zlib

import boto3
from moto import mock_aws
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.models.lab  # Register shared control tables before create_all.
from app.models import StorageObject
from app.models.lab_data import LabArtifact, LabDatasetVersion, LabFile
from app.services.lab_archive import LabDataError
from app.services.lab_data_service import (
    archive_artifact, artifact_projection, dataset_projection, materialize_artifact,
    materialize_dataset, register_artifact_upload, register_dataset_upload,
    validate_artifact, validate_dataset,
)
from app.services.storage_reconcile import business_references
from app.storage import S3CompatibleStorage, StorageArea, StorageLimitExceeded, StorageService
from conftest import create_user


def _zip(files):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    stream.seek(0)
    return stream


def _csv(n=20):
    return ("sample_id,value,label\n" + "".join(f"row-{i},{i},{i % 2}\n" for i in range(n))).encode()


def _png(value):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, value, 0, 0]))) + chunk(b"IEND", b""))


@pytest.fixture
def owner(db_session_factory):
    return create_user(db_session_factory, "lab-data-owner", "teacher")


@pytest.fixture(params=["local", "s3"])
def lab_storage(request, test_settings, monkeypatch):
    if request.param == "local":
        yield test_settings
    else:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="lab-data-tests")
            backend = S3CompatibleStorage(client, bucket="lab-data-tests")
            storage = StorageService({StorageArea.DATASETS: backend, StorageArea.ARTIFACTS: backend}, backend_name="s3")
            monkeypatch.setattr(StorageService, "from_settings", classmethod(lambda cls, settings: storage))
            yield test_settings


def _dataset(db, owner, settings, *, data=None, config=None, filename="data.csv", task="tabular_classification"):
    stream = BytesIO(_csv()) if data is None else data
    row = register_dataset_upload(db, owner.id, "演示数据", task, config or {}, filename, stream, settings)
    db.commit()
    validate_dataset(db, row.id, settings)
    db.commit()
    return row


def test_dataset_roundtrip_both_backends_and_test_label_isolation(db_session_factory, owner, lab_storage, tmp_path):
    with db_session_factory() as db:
        dataset = _dataset(db, owner, lab_storage)
        assert dataset.status == "ready", dataset.error
        assert dataset.summary["splits"] == {"train": 14, "validation": 3, "test": 3}
        assert dataset.summary["class_labels"] == ["0", "1"]
        train_manifest = materialize_dataset(db, dataset.id, "train", tmp_path / "train", lab_storage, include_labels=True)
        assert (train_manifest.parent / "labels.csv").is_file()
        test_manifest = materialize_dataset(db, dataset.id, "test", tmp_path / "test", lab_storage)
        assert {p.name for p in test_manifest.parent.iterdir()} == {"samples.csv"}
        with test_manifest.open() as stream:
            rows = list(csv.DictReader(stream))
        assert set(rows[0]) == {"sample_id", "value"}
        assert all(row["sample_id"].startswith("s-") for row in rows)
        projection = dataset_projection(dataset)
        assert not {"raw_object_id", "sha256", "config", "error"} & projection.keys()
        referenced = business_references(db)
        assert dataset.raw_object_id in referenced
        for file in db.scalars(select(LabFile).where(LabFile.dataset_version_id == dataset.id)):
            assert "lab_files.object_id" in referenced[file.object_id]
        original_files = db.scalars(select(LabFile).where(LabFile.dataset_version_id == dataset.id)).all()
        validate_dataset(db, dataset.id, lab_storage)
        assert len(db.scalars(select(LabFile).where(LabFile.dataset_version_id == dataset.id)).all()) == len(original_files)


def test_fixed_seed_reproducible_split_and_explicit_csv_partitions(db_session_factory, owner, test_settings, tmp_path):
    with db_session_factory() as db:
        first = _dataset(db, owner, test_settings)
        second = _dataset(db, owner, test_settings)
        a = materialize_dataset(db, first.id, "test", tmp_path / "a", test_settings)
        b = materialize_dataset(db, second.id, "test", tmp_path / "b", test_settings)
        with a.open() as stream_a, b.open() as stream_b:
            rows_a, rows_b = list(csv.DictReader(stream_a)), list(csv.DictReader(stream_b))
        assert [row["value"] for row in rows_a] == [row["value"] for row in rows_b]
        assert {row["sample_id"] for row in rows_a}.isdisjoint(row["sample_id"] for row in rows_b)
        data = _zip({f"{split}.csv": f"sample_id,value,label\n{split},{i},{i % 2}\n" for i, split in enumerate(("train", "validation", "test"))})
        explicit = _dataset(db, owner, test_settings, data=data, filename="parts.zip")
        assert explicit.status == "ready", explicit.error
        assert explicit.summary["splits"] == {"train": 1, "validation": 1, "test": 1}


@pytest.mark.parametrize("body,config", [
    (b"sample_id,value,label\na,1,0\na,2,1\nb,3,0\n", {}),
    (b"sample_id,value,label,split\na,1,0,train\nb,1,1,test\nc,3,0,validation\n", {"split_column": "split"}),
    (b"sample_id,value,label,split\na,1,0,train\nb,2,1,\nc,3,0,test\n", {"split_column": "split"}),
    (b"sample_id,value,label\na,1,0\nb,2,\nc,3,0\n", {}),
    (b"sample_id,value,label\na,1,0\nb,2,1,extra\nc,3,0\n", {}),
])
def test_reject_invalid_ids_labels_rows_and_cross_split_overlap(db_session_factory, owner, test_settings, body, config):
    with db_session_factory() as db:
        dataset = _dataset(db, owner, test_settings, data=BytesIO(body), config=config)
        assert dataset.status == "failed"
        assert dataset.error
        assert not db.scalars(select(LabFile).where(LabFile.dataset_version_id == dataset.id)).all()


def test_regression_requires_finite_labels(db_session_factory, owner, test_settings):
    with db_session_factory() as db:
        row = _dataset(db, owner, test_settings, data=BytesIO(b"value,label\n1,2.4\n2,nan\n3,7\n"), task="tabular_regression")
        assert row.status == "failed"
        assert "有限" in row.error


@pytest.mark.parametrize("manifest", [False, True])
def test_image_zip_anonymizes_class_paths_and_separates_labels(db_session_factory, owner, test_settings, tmp_path, manifest):
    files = {f"{'cat' if i % 2 else 'dog'}/name-{i}.png": _png(i) for i in range(20)}
    if manifest:
        files["labels.csv"] = "image_path,label\n" + "".join(f"{path},{path.split('/')[0]}\n" for path in files)
    with db_session_factory() as db:
        row = _dataset(db, owner, test_settings, data=_zip(files), filename="images.zip", task="image_classification")
        assert row.status == "ready", row.error
        samples = materialize_dataset(db, row.id, "test", tmp_path / "images", test_settings)
        assert "dog" not in samples.read_text() and "cat" not in samples.read_text()
        with samples.open() as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 3
        for sample in rows:
            assert (samples.parent / sample["image_path"]).is_file()
        assert not (samples.parent / "labels.csv").exists()


@pytest.mark.parametrize("path", ["../escape.csv", "/etc/data.csv", "a\\b.csv", "a/../../x.csv", "C:/x.csv"])
def test_reject_zip_path_traversal(db_session_factory, owner, test_settings, path):
    with db_session_factory() as db:
        dataset = _dataset(db, owner, test_settings, data=_zip({path: _csv()}), filename="unsafe.zip")
        assert dataset.status == "failed"


def test_reject_symlink_zip_and_expansion_bomb(db_session_factory, owner, test_settings):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        link = zipfile.ZipInfo("link.csv")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    stream.seek(0)
    with db_session_factory() as db:
        linked = _dataset(db, owner, test_settings, data=stream, filename="link.zip")
        assert linked.status == "failed"
        limited = test_settings.model_copy(update={"lab_expanded_max_bytes": 64})
        bomb = _dataset(db, owner, limited, data=_zip({"data.csv": b"x" * 4096}), filename="bomb.zip")
        assert bomb.status == "failed"


def test_stream_upload_enforces_limit_without_unbounded_reads(db_session_factory, owner, test_settings):
    class Bounded(BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024
            return super().read(size)
    settings = test_settings.model_copy(update={"lab_upload_max_bytes": 10})
    with db_session_factory() as db:
        with pytest.raises(StorageLimitExceeded):
            register_dataset_upload(db, owner.id, "size", "tabular_classification", {}, "data.csv", Bounded(_csv()), settings)
        assert not db.scalars(select(LabDatasetVersion)).all()


def test_external_artifact_roundtrip_manifest_and_storage_fk_protection(db_session_factory, owner, lab_storage, tmp_path):
    files = {"predict.py": "print('never executed during ingest')", "train.py": "pass", "model/weights.bin": b"unsafe-pickle-is-just-bytes", "model.meta": "metadata"}
    with db_session_factory() as db:
        artifact = register_artifact_upload(db, owner.id, "model.zip", _zip(files), {}, lab_storage)
        db.commit()
        validate_artifact(db, artifact.id, lab_storage)
        db.commit()
        assert artifact.status == "ready", artifact.error
        dest = materialize_artifact(db, artifact.id, tmp_path / "model", lab_storage)
        assert (dest / "model/weights.bin").read_bytes() == files["model/weights.bin"]
        assert len(artifact.manifest_hash) == 64
        projection = artifact_projection(db, artifact)
        assert projection["log_trust"] == "student_provided"
        assert all("object_id" not in file and "object_key" not in file for file in projection["files"])
        obj = db.get(StorageObject, artifact.raw_object_id)
        with pytest.raises(IntegrityError):
            db.delete(obj)
            db.commit()
        db.rollback()


def test_artifact_requires_prediction_and_training_source(db_session_factory, owner, test_settings):
    with db_session_factory() as db:
        for files in ({"train.py": "pass"}, {"predict.py": "pass"}):
            artifact = register_artifact_upload(db, owner.id, "model.zip", _zip(files), {}, test_settings)
            db.commit()
            validate_artifact(db, artifact.id, test_settings)
            assert artifact.status == "failed"
            db.commit()


def test_completed_training_archive_and_modified_storage_detection(db_session_factory, owner, test_settings, tmp_path):
    source = tmp_path / "training"
    source.mkdir()
    (source / "predict.py").write_text("pass")
    (source / "model.bin").write_bytes(b"weights")
    with db_session_factory() as db:
        artifact = archive_artifact(db, owner.id, source, {"cell-1": "train()"}, test_settings)
        db.commit()
        assert artifact.status == "ready"
        file = db.scalar(select(LabFile).where(LabFile.artifact_id == artifact.id, LabFile.path == "model.bin"))
        obj = db.get(StorageObject, file.object_id)
        StorageService.from_settings(test_settings).put(StorageArea.ARTIFACTS, obj.object_key, BytesIO(b"forged!"))
        with pytest.raises(LabDataError, match="完整性"):
            materialize_artifact(db, artifact.id, tmp_path / "restore", test_settings)


def test_artifact_rejects_local_symlinks(db_session_factory, owner, test_settings, tmp_path):
    source = tmp_path / "training"
    source.mkdir()
    (source / "predict.py").symlink_to("/etc/passwd")
    with db_session_factory() as db:
        with pytest.raises(LabDataError, match="普通文件"):
            archive_artifact(db, owner.id, source, {"cell": "x"}, test_settings)


@pytest.mark.parametrize("name,body", [
    ("train.py", b"\xff\xfe\x00"),
    ("train.ipynb", b"not-json"),
    ("train.ipynb", b"[]"),
    ("train.ipynb", b'{"cells":[4]}'),
    ("train.ipynb", b'{"cells":[{"cell_type":"code","source":[4]}]}'),
    ("train.py", b"x" * (2 * 1024 * 1024 + 1)),
])
def test_source_validation_fails_before_artifact_becomes_ready(db_session_factory, owner, test_settings, name, body):
    with db_session_factory() as db:
        artifact = register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", name: body}), {}, test_settings)
        db.commit()
        validate_artifact(db, artifact.id, test_settings)
        db.commit()
        assert artifact.status == "failed"
        assert not db.scalars(select(LabFile).where(LabFile.artifact_id == artifact.id)).all()


def test_image_header_pixel_bomb_is_rejected(db_session_factory, owner, test_settings):
    body = bytearray(_png(9))
    body[16:24] = struct.pack(">II", 50000, 50000)
    body[29:33] = struct.pack(">I", zlib.crc32(body[12:29]))
    with db_session_factory() as db:
        dataset = _dataset(db, owner, test_settings, data=_zip({"a/one.png": bytes(body), "a/two.png": _png(2), "b/three.png": _png(3)}),
                           filename="images.zip", task="image_classification")
        assert dataset.status == "failed" and "像素" in dataset.error


def test_dataset_sample_limit_rejects_before_materialization(db_session_factory, owner, test_settings, monkeypatch):
    import app.services.lab_data_service as service
    assert service.MAX_DATASET_SAMPLES == 200000
    monkeypatch.setattr(service, "MAX_DATASET_SAMPLES", 3)
    with db_session_factory() as db:
        dataset = _dataset(db, owner, test_settings)
        assert dataset.status == "failed" and "200000" in dataset.error
        assert not db.scalars(select(LabFile).where(LabFile.dataset_version_id == dataset.id)).all()


def _artifact_record(db_session_factory, owner):
    from app.models import ExperimentModule, ExperimentRecord, LabAssessment, LabRecord, NotebookTemplateVersion
    from conftest import make_template_version
    version_id = make_template_version(db_session_factory)
    with db_session_factory() as db:
        version = db.get(NotebookTemplateVersion, version_id)
        module = ExperimentModule(name="quota", owner_id=owner.id, status="published", template_id=version.template_id)
        db.add(module)
        db.flush()
        record = ExperimentRecord(module_id=module.id, student_id=owner.id, template_version_id=version.id,
            environment_version_id=version.environment_version_id)
        db.add(record)
        db.flush()
        assessment = LabAssessment(entry_key=f"module:{module.id}", owner_id=owner.id, version=1, status="published", config={})
        db.add(assessment)
        db.flush()
        db.add(LabRecord(record_id=record.id, assessment_id=assessment.id, generation=7))
        db.commit()
        return record.id


def test_record_artifact_admission_serializes_pending_without_changing_grade_generation(db_session_factory, owner, test_settings):
    from app.models import LabRecord
    record_id = _artifact_record(db_session_factory, owner)
    with db_session_factory() as db:
        first = register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", "train.py": "pass"}), {}, test_settings, record_id=record_id)
        db.commit()
        with pytest.raises(LabDataError, match="等待校验"):
            register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", "train.py": "pass"}), {}, test_settings, record_id=record_id)
        db.rollback()
        assert db.get(LabRecord, record_id).generation == 7
        validate_artifact(db, first.id, test_settings)
        db.commit()
        assert first.status == "ready"


def test_record_quota_counts_raw_package_and_expanded_artifact(db_session_factory, owner, test_settings):
    record_id = _artifact_record(db_session_factory, owner)
    files = {"predict.py": "pass", "train.py": "pass", "model.bin": b"x" * 1024}
    raw = _zip(files)
    raw_size = len(raw.getvalue())
    settings = test_settings.model_copy(update={"lab_artifact_quota_bytes": raw_size + 1031})
    with db_session_factory() as db:
        artifact = register_artifact_upload(db, owner.id, "model.zip", raw, {}, settings, record_id=record_id)
        db.commit()
        validate_artifact(db, artifact.id, settings)
        db.commit()
        assert artifact.status == "failed" and "剩余存储额度" in artifact.error
        assert not db.scalars(select(LabFile).where(LabFile.artifact_id == artifact.id)).all()
        assert db.get(StorageObject, artifact.raw_object_id).size_bytes == raw_size
        exhausted = settings.model_copy(update={"lab_artifact_quota_bytes": raw_size})
        with pytest.raises(LabDataError, match="已用尽"):
            register_artifact_upload(db, owner.id, "model.zip", _zip(files), {}, exhausted, record_id=record_id)


def test_record_artifact_count_limit(db_session_factory, owner, test_settings):
    record_id = _artifact_record(db_session_factory, owner)
    with db_session_factory() as db:
        for _ in range(20):
            db.add(LabArtifact(owner_id=owner.id, record_id=record_id, source="platform", status="ready", code_snapshot={}))
        db.commit()
        with pytest.raises(LabDataError, match="20"):
            register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", "train.py": "pass"}), {}, test_settings, record_id=record_id)


def test_platform_training_result_can_finish_while_external_upload_waits(db_session_factory, owner, test_settings, tmp_path):
    record_id = _artifact_record(db_session_factory, owner)
    output = tmp_path / "completed"
    output.mkdir()
    (output / "predict.py").write_text("pass")
    with db_session_factory() as db:
        pending = register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", "train.py": "pass"}), {}, test_settings, record_id=record_id)
        db.commit()
        completed = archive_artifact(db, owner.id, output, {"cells": []}, test_settings, record_id=record_id)
        db.commit()
        assert completed.status == "ready" and pending.status == "queued"
        assert len(db.scalars(select(LabArtifact).where(LabArtifact.record_id == record_id, LabArtifact.status == "queued")).all()) == 1


def _ready_artifact(db, owner, settings, *, record_id=None):
    artifact = register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", "train.py": "pass", "model.bin": b"model"}),
        {}, settings, record_id=record_id)
    db.commit()
    validate_artifact(db, artifact.id, settings)
    db.commit()
    assert artifact.status == "ready", artifact.error
    return artifact


def test_artifact_delete_is_owner_or_admin_only_and_idempotent(client, db_session_factory, owner, test_settings):
    from app.models import AuditEvent
    from conftest import auth_header, login
    intruder = create_user(db_session_factory, "delete-intruder", "student")
    admin = create_user(db_session_factory, "delete-admin", "admin")
    with db_session_factory() as db:
        artifact = _ready_artifact(db, owner, test_settings)
        ident, digest = artifact.id, artifact.manifest_hash
    assert client.delete(f"/api/v1/labs/artifacts/{ident}").status_code == 401
    intruder_token, _ = login(client, intruder.username)
    denied = client.delete(f"/api/v1/labs/artifacts/{ident}", headers=auth_header(intruder_token))
    assert denied.status_code == 403
    admin_token, _ = login(client, admin.username)
    for _ in range(2):
        response = client.delete(f"/api/v1/labs/artifacts/{ident}", headers=auth_header(admin_token))
        assert response.status_code == 200, response.text
        assert response.json() == {"id": ident, "status": "deleted", "storage_cleanup": "pending_gc"}
    with db_session_factory() as db:
        artifact = db.get(LabArtifact, ident)
        assert artifact.manifest_hash == digest and artifact.raw_object_id is None
        assert not db.scalars(select(LabFile).where(LabFile.artifact_id == ident)).all()
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "lab.artifact.delete")).all()) == 1


@pytest.mark.parametrize("status", ["queued", "running"])
def test_artifact_delete_rejects_active_run_reference(db_session_factory, owner, test_settings, status):
    from fastapi import HTTPException
    from app.models import LabRun
    from app.services.lab_data_service import delete_unsubmitted_artifact
    with db_session_factory() as db:
        artifact = _ready_artifact(db, owner, test_settings)
        db.add(LabRun(owner_id=owner.id, kind="public_validate", status=status, artifact_id=artifact.id))
        db.commit()
        with pytest.raises(HTTPException) as failure:
            delete_unsubmitted_artifact(db, artifact.id, owner, test_settings)
        assert failure.value.status_code == 409
        db.rollback()
        assert artifact.status == "ready" and artifact.raw_object_id is not None


@pytest.mark.parametrize("reference", ["config", "baseline"])
def test_artifact_delete_protects_published_reference_and_baseline(db_session_factory, owner, test_settings, reference):
    from fastapi import HTTPException
    from app.models import LabAssessment, LabRun
    from app.services.lab_data_service import delete_unsubmitted_artifact
    with db_session_factory() as db:
        artifact = _ready_artifact(db, owner, test_settings)
        assessment = LabAssessment(owner_id=owner.id, entry_key="module:published-test", version=1, status="published", config={})
        if reference == "config":
            assessment.config = {"reference_artifact_id": artifact.id}
        else:
            baseline = LabRun(owner_id=owner.id, kind="baseline", status="succeeded", result={"artifact_id": artifact.id})
            db.add(baseline)
            db.flush()
            assessment.validation_run_id = baseline.id
        db.add(assessment)
        db.commit()
        with pytest.raises(HTTPException) as failure:
            delete_unsubmitted_artifact(db, artifact.id, owner, test_settings)
        assert failure.value.status_code == 409


def test_artifact_delete_protects_any_formal_submission(db_session_factory, owner, test_settings):
    from fastapi import HTTPException
    from app.models import ExperimentSubmission, LabRecord, LabSubmission
    from app.services.lab_data_service import delete_unsubmitted_artifact
    record_id = _artifact_record(db_session_factory, owner)
    with db_session_factory() as db:
        artifact = _ready_artifact(db, owner, test_settings, record_id=record_id)
        submission = ExperimentSubmission(record_id=record_id, attempt_number=1, client_request_id="deletion-protection", cells_snapshot={}, outputs_snapshot={})
        db.add(submission)
        db.flush()
        db.add(LabSubmission(submission_id=submission.id, assessment_id=db.get(LabRecord, record_id).assessment_id,
            artifact_id=artifact.id, submission_hash="a" * 64))
        db.commit()
        with pytest.raises(HTTPException) as failure:
            delete_unsubmitted_artifact(db, artifact.id, owner, test_settings)
        assert failure.value.status_code == 409


def test_deleted_artifacts_release_record_quota(db_session_factory, owner, test_settings):
    from app.services.lab_data_service import delete_unsubmitted_artifact, _artifact_budget
    record_id = _artifact_record(db_session_factory, owner)
    with db_session_factory() as db:
        artifact = _ready_artifact(db, owner, test_settings, record_id=record_id)
        assert _artifact_budget(db, record_id, test_settings) < test_settings.lab_artifact_quota_bytes
        delete_unsubmitted_artifact(db, artifact.id, owner, test_settings)
        db.commit()
        assert _artifact_budget(db, record_id, test_settings) == test_settings.lab_artifact_quota_bytes
        for _ in range(20):
            db.add(LabArtifact(owner_id=owner.id, record_id=record_id, source="platform", status="deleted", code_snapshot={}))
        db.commit()
        replacement = register_artifact_upload(db, owner.id, "model.zip", _zip({"predict.py": "pass", "train.py": "pass"}), {}, test_settings, record_id=record_id)
        assert replacement.status == "queued"


def test_deleted_artifact_storage_gc_keeps_shared_objects(db_session_factory, owner, test_settings):
    from datetime import datetime, timedelta, timezone
    from app.services.lab_data_service import delete_unsubmitted_artifact
    from app.services.storage_gc import StorageGarbageCollector
    from app.services.storage_reconcile import StorageReconcileService
    with db_session_factory() as db:
        artifact = _ready_artifact(db, owner, test_settings)
        raw = db.get(StorageObject, artifact.raw_object_id)
        file = db.scalar(select(LabFile).where(LabFile.artifact_id == artifact.id, LabFile.path == "model.bin"))
        shared_object = db.get(StorageObject, file.object_id)
        other = LabArtifact(owner_id=owner.id, source="platform", status="ready", code_snapshot={})
        db.add(other)
        db.flush()
        db.add(LabFile(artifact_id=other.id, object_id=shared_object.id, path="model.bin", role="output"))
        db.commit()
        delete_unsubmitted_artifact(db, artifact.id, owner, test_settings)
        db.commit()
        storage = StorageService.from_settings(test_settings)
        assert storage.exists(StorageArea.ARTIFACTS, raw.object_key)
        now = datetime.now(timezone.utc)
        StorageReconcileService(db, storage, quarantine_retention=timedelta(seconds=1)).scan(now=now, dry_run=False)
        db.commit()
        StorageGarbageCollector(db, storage).collect(now=now + timedelta(seconds=2), dry_run=False)
        db.refresh(raw)
        db.refresh(shared_object)
        assert raw.status == "deleted" and not storage.exists(StorageArea.ARTIFACTS, raw.object_key)
        assert shared_object.status == "active" and storage.exists(StorageArea.ARTIFACTS, shared_object.object_key)


def test_artifact_file_summary_is_bounded_and_paginates(db_session_factory,test_settings,owner,tmp_path):
    from app.services.lab_data_service import artifact_files
    directory=tmp_path/'large-manifest';directory.mkdir()
    (directory/'predict.py').write_text('print("predict")')
    (directory/'train.py').write_text('print("train")')
    for i in range(63): (directory/f'result-{i:03}.txt').write_text(str(i))
    with db_session_factory() as db:
        artifact=archive_artifact(db,owner.id,directory,{},test_settings)
        summary=artifact_projection(db,artifact)
        assert summary['file_count']==65 and summary['files_truncated']
        assert len(summary['files'])==50
        tail=artifact_files(db,artifact.id,page=2)
        assert len(tail['items'])==15
        assert len({f['id'] for f in summary['files']+tail['items']})==65
