"""StorageObject 元数据、生命周期和单对象完整性检查测试。"""

from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import (
    StorageObject,
    StorageObjectBackend,
    StorageObjectStatus,
)
from app.services.storage_object_service import (
    StorageIntegrityStatus,
    StorageObjectConflict,
    StorageObjectNotFound,
    StorageObjectService,
    StorageObjectStateError,
    StorageObjectValidationError,
)
from app.storage import StorageArea, StorageService
from conftest import constraint_violation, create_user


def _make_service(db, settings):
    return StorageObjectService(db, StorageService.from_settings(settings))


def _create_object(db, service, **overrides):
    values = {
        "namespace": "course-covers",
        "object_key": "covers/1/cover.jpg",
        "original_filename": "cover.jpg",
        "content_type": "image/jpeg",
        "metadata_json": {"source": "test"},
    }
    values.update(overrides)
    obj = service.create_staging(**values)
    db.commit()
    db.refresh(obj)
    return obj


def test_create_staging_persists_metadata_and_created_by(db_session_factory, test_settings):
    user = create_user(db_session_factory, "storage-owner", "teacher")
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        obj = _create_object(db, service, created_by_id=user.id)

        assert obj.id is not None
        assert obj.namespace == "course-covers"
        assert obj.object_key == "covers/1/cover.jpg"
        assert obj.backend == StorageObjectBackend.LOCAL.value
        assert obj.status == StorageObjectStatus.STAGING.value
        assert obj.original_filename == "cover.jpg"
        assert obj.content_type == "image/jpeg"
        assert obj.size_bytes is None
        assert obj.sha256 is None
        assert obj.metadata_json == {"source": "test"}
        assert obj.created_by_id == user.id
        assert obj.created_at is not None
        assert obj.updated_at is not None
        assert obj.deleted_at is None
        assert obj.version == 1


def test_namespace_and_object_key_are_unique(db_session_factory, test_settings):
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        _create_object(db, service)

        with pytest.raises(StorageObjectConflict):
            service.create_staging(
                namespace="course-covers",
                object_key="covers/1/cover.jpg",
            )


def test_create_staging_rejects_unsafe_keys_and_unknown_backend(db_session_factory, test_settings):
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        for bad_key in ("", ".", "./", "../escape", "covers/../escape", "/absolute", "a\\b"):
            with pytest.raises(StorageObjectValidationError):
                service.create_staging(namespace="course-covers", object_key=bad_key)

        with pytest.raises(StorageObjectValidationError):
            service.create_staging(
                namespace="course-covers",
                object_key="covers/1/s3.jpg",
                backend="minio",
            )

        obj = service.create_staging(
            namespace="lesson-videos",
            object_key="lessons/1/video.mp4",
            backend=StorageObjectBackend.S3,
        )
        assert obj.backend == StorageObjectBackend.S3.value


def test_storage_object_rejects_invalid_status_and_negative_size(db_session_factory):
    with db_session_factory() as db:
        invalid_status = StorageObject(
            namespace="course-covers",
            object_key="covers/invalid-status",
            status="unknown",
            size_bytes=0,
            metadata_json={},
        )
        db.add(invalid_status)
        with constraint_violation() as rejected:
            db.commit()
        if isinstance(rejected.value, OperationalError):
            assert rejected.value.orig.args[0] == 3819
        assert "ck_storage_objects_status" in str(rejected.value.orig)
        db.rollback()

        negative_size = StorageObject(
            namespace="course-covers",
            object_key="covers/negative-size",
            status=StorageObjectStatus.STAGING.value,
            size_bytes=-1,
            metadata_json={},
        )
        db.add(negative_size)
        with constraint_violation() as rejected:
            db.commit()
        if isinstance(rejected.value, OperationalError):
            assert rejected.value.orig.args[0] == 3819
        assert "ck_storage_objects_size_nonnegative" in str(rejected.value.orig)


def test_lifecycle_transitions_are_explicit_and_idempotent(db_session_factory, test_settings):
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        obj = _create_object(db, service)

        activated = service.activate(
            obj.id,
            size_bytes=5,
            sha256=hashlib.sha256(b"hello").hexdigest(),
            etag="etag-1",
        )
        db.commit()
        db.refresh(activated)
        assert activated.status == StorageObjectStatus.ACTIVE.value
        assert activated.size_bytes == 5
        assert activated.version == 2
        assert activated.deleted_at is None

        version_after_activate = activated.version
        assert service.activate(obj.id, size_bytes=5).version == version_after_activate

        deleting = service.mark_deleting(obj.id)
        db.commit()
        assert deleting.status == StorageObjectStatus.DELETING.value
        assert deleting.version == version_after_activate + 1

        deleted = service.mark_deleted(obj.id)
        db.commit()
        db.refresh(deleted)
        assert deleted.status == StorageObjectStatus.DELETED.value
        assert deleted.deleted_at is not None

        deleted_again = service.mark_deleted(obj.id)
        assert deleted_again.status == StorageObjectStatus.DELETED.value
        assert deleted_again.deleted_at == deleted.deleted_at

        with pytest.raises(StorageObjectStateError):
            service.activate(obj.id, size_bytes=5)


def test_failed_transition_and_invalid_transition(db_session_factory, test_settings):
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        obj = _create_object(db, service)

        failed = service.mark_failed(obj.id)
        db.commit()
        assert failed.status == StorageObjectStatus.FAILED.value
        assert service.mark_failed(obj.id).status == StorageObjectStatus.FAILED.value

        with pytest.raises(StorageObjectStateError):
            service.mark_deleted(obj.id)


def test_missing_storage_object_raises_consistent_error(db_session_factory, test_settings):
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        with pytest.raises(StorageObjectNotFound):
            service.get(999999)


def test_integrity_check_matches_physical_object(db_session_factory, test_settings):
    storage = StorageService.from_settings(test_settings)
    key = "covers/2/cover.jpg"
    body = b"hello"
    storage.put(StorageArea.COVERS, key, BytesIO(body))

    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        obj = _create_object(
            db,
            service,
            object_key=key,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        service.activate(obj.id, size_bytes=len(body), sha256=obj.sha256)
        db.commit()

        report = service.check_integrity(
            obj.id,
            area=StorageArea.COVERS,
            verify_sha256=True,
        )
        assert report.status == StorageIntegrityStatus.OK.value
        assert report.is_consistent is True
        assert report.physical_exists is True
        assert report.actual_size == len(body)
        assert report.actual_sha256 == obj.sha256


def test_integrity_check_detects_missing_and_size_mismatch(db_session_factory, test_settings):
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        missing = _create_object(
            db,
            service,
            object_key="covers/3/missing.jpg",
            size_bytes=5,
        )
        service.activate(missing.id, size_bytes=5)
        db.commit()
        report = service.check_integrity(missing.id, area=StorageArea.COVERS)
        assert report.status == StorageIntegrityStatus.MISSING.value
        assert report.is_consistent is False

    storage = StorageService.from_settings(test_settings)
    storage.put(StorageArea.COVERS, "covers/4/size.jpg", BytesIO(b"hello"))
    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        obj = _create_object(db, service, object_key="covers/4/size.jpg", size_bytes=4)
        service.activate(obj.id, size_bytes=4)
        db.commit()
        report = service.check_integrity(obj.id, area=StorageArea.COVERS)
        assert report.status == StorageIntegrityStatus.SIZE_MISMATCH.value
        assert report.actual_size == 5


def test_integrity_check_detects_deleted_object_still_on_storage(db_session_factory, test_settings):
    storage = StorageService.from_settings(test_settings)
    key = "covers/5/deleted.jpg"
    storage.put(StorageArea.COVERS, key, BytesIO(b"hello"))

    with db_session_factory() as db:
        service = _make_service(db, test_settings)
        obj = _create_object(db, service, object_key=key, size_bytes=5)
        service.activate(obj.id, size_bytes=5)
        service.mark_deleting(obj.id)
        service.mark_deleted(obj.id)
        db.commit()

        report = service.check_integrity(obj.id, area=StorageArea.COVERS)
        assert report.status == StorageIntegrityStatus.DELETED_PHYSICAL_PRESENT.value
        assert report.is_consistent is False


def test_model_exposes_only_metadata_key_not_absolute_path(db_session_factory):
    table = StorageObject.__table__
    assert "object_key" in table.c
    assert "storage_path" not in table.c
    assert "absolute_path" not in table.c
    assert db_session_factory is not None
