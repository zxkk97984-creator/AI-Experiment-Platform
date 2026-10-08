"""Completion rechecks authority after a long copy without locking its bytes."""
from io import BytesIO

from fastapi import HTTPException, UploadFile
import pytest

from app import models
from app.models.course_material import CourseMaterial
from app.services import course_material_service as service
from app.services.account_service import lock_policy
from app.storage import StorageArea, StorageError, StorageService
from material_test_files import minimal_pdf
from test_course_materials import context as material_context, create_file, publish, request, PDF

context = material_context


@pytest.mark.parametrize('change', ['owner', 'session', 'credential', 'disabled', 'role'])
def test_copy_completion_rejects_authority_changed_in_another_transaction(
        client, context, db_session_factory, test_settings, monkeypatch, change):
    row = publish(client, context, create_file(client, context))
    old_url = request(client, context, 'get', f"/materials/{row['id']}/access-url",
                      who='material-student').json()['url']
    storage = StorageService.from_settings(test_settings)
    original_copy = StorageService.copy
    with db_session_factory() as db:
        material = db.get(CourseMaterial, row['id'])
        actor = db.get(models.User, context['users']['material-owner'].id)
        old_object_id = material.storage_object_id
        old_key = db.get(models.StorageObject, old_object_id).object_key

        def copy_and_change(self, area, source, destination):
            assert not db.in_transaction(), 'storage copy must not hold a database transaction'
            result = original_copy(self, area, source, destination)
            with db_session_factory() as other:
                lock_policy(other)
                if change == 'owner':
                    other.get(models.Course, context['cid']).teacher_id = context['users']['material-other'].id
                else:
                    user = other.get(models.User, actor.id)
                    if change in ('session', 'credential'):
                        field = change + '_version'
                        setattr(user, field, getattr(user, field) + 1)
                    else:
                        setattr(user, 'status' if change == 'disabled' else 'role',
                                'disabled' if change == 'disabled' else 'student')
                other.commit()
            return result

        monkeypatch.setattr(StorageService, 'copy', copy_and_change)
        upload = UploadFile(filename='new.pdf', file=BytesIO(minimal_pdf('replacement')))
        with pytest.raises(HTTPException) as failure:
            service.replace_file(db, material, actor, upload, row['version'], test_settings)
        assert failure.value.status_code == 403
        current = db.get(CourseMaterial, row['id'])
        assert current.storage_object_id == old_object_id and current.version == row['version']
        assert storage.read(StorageArea.MATERIALS, old_key) == PDF
        assert len(storage.list_objects(StorageArea.MATERIALS, 'materials')) == 1
        assert client.get(old_url).status_code == 200
        assert client.get(old_url).content == PDF


@pytest.mark.parametrize('change', ['archive', 'version'])
def test_copy_completion_uses_current_material_state(client, context, db_session_factory,
                                                     test_settings, monkeypatch, change):
    row = create_file(client, context)
    original_copy = StorageService.copy
    with db_session_factory() as db:
        material = db.get(CourseMaterial, row['id'])
        old_object_id = material.storage_object_id
        actor = db.get(models.User, context['users']['material-owner'].id)

        def copy_and_update(self, area, source, destination):
            assert not db.in_transaction()
            result = original_copy(self, area, source, destination)
            with db_session_factory() as other:
                lock_policy(other)
                current = other.get(CourseMaterial, row['id'])
                if change == 'archive':
                    current.status = 'archived'
                else:
                    current.version += 1
                other.commit()
            return result

        monkeypatch.setattr(StorageService, 'copy', copy_and_update)
        with pytest.raises(HTTPException) as failure:
            service.replace_file(db, material, actor, UploadFile(filename='new.pdf', file=BytesIO(PDF)),
                                 row['version'], test_settings)
        assert failure.value.status_code == 409
        assert db.get(CourseMaterial, row['id']).storage_object_id == old_object_id
        assert len(StorageService.from_settings(test_settings).list_objects(StorageArea.MATERIALS, 'materials')) == 1


def test_copy_failure_preserves_old_bytes_download_and_audit(client, context, db_session_factory,
                                                           test_settings, monkeypatch):
    row = publish(client, context, create_file(client, context))
    old_url = request(client, context, 'get', f"/materials/{row['id']}/access-url",
                      who='material-student').json()['url']

    def failed_copy(*args, **kwargs):
        raise StorageError('test storage copy failure')

    monkeypatch.setattr(StorageService, 'copy', failed_copy)
    result = request(client, context, 'put', f"/materials/{row['id']}/file",
                     data={'version': row['version']}, files={'file': ('new.pdf', PDF, 'application/pdf')})
    assert result.status_code == 503
    with db_session_factory() as db:
        material = db.get(CourseMaterial, row['id'])
        assert material.version == row['version']
        assert db.query(models.AuditEvent).filter_by(action='material.upload', target_id=str(row['id'])).count() == 1
    assert client.get(old_url).content == PDF
    assert len(StorageService.from_settings(test_settings).list_objects(StorageArea.MATERIALS, 'materials')) == 1
