"""Material authorization, signed media, atomic replacement and chapter scope."""
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from app import models
from app.models.course_material import CourseMaterial
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.services import course_material_service as service
from app.services.storage_reconcile import business_references
from app.storage import StorageArea, StorageService
from conftest import auth_header, create_course_db, create_user, login, seed_basic_environment
from material_test_files import minimal_pdf

API = "/api/v1"
PDF = minimal_pdf()


@pytest.fixture()
def context(client, db_session_factory):
    users = {name: create_user(db_session_factory, name, role) for name, role in (
        ("material-owner", "teacher"), ("material-teacher", "teacher"),
        ("material-other", "teacher"), ("material-student", "student"), ("material-outsider", "student"))}
    tokens = {name: login(client, name)[0] for name in users}
    cid = create_course_db(db_session_factory, teacher_username="material-owner", status="published", visibility="class")
    with db_session_factory() as db:
        term = models.AcademicTerm(code="material-term", name="测试学期", start_date=date(2026, 9, 1), end_date=date(2027, 1, 1), status="active")
        db.add(term); db.flush()
        klass = models.TeachingClass(academic_term_id=term.id, code="material-class", name="资料班", status="active")
        db.add(klass); db.flush()
        course = db.get(models.Course, cid); course.academic_term_id = term.id
        offering = CourseOffering(course_id=cid, teaching_class_id=klass.id, academic_term_id=term.id,
            teacher_id=users["material-teacher"].id, status="active")
        chapter = models.Chapter(course_id=cid, title="第一章")
        db.add_all([offering, chapter]); db.flush()
        db.add(OfferingEnrollment(offering_id=offering.id, student_id=users["material-student"].id,
            status="active", active_scope_key=f"{cid}:{term.id}:{users['material-student'].id}"))
        db.add(models.CourseEnrollment(course_id=cid, student_id=users["material-student"].id, status="enrolled", origin="class"))
        db.commit()
        return {"cid": cid, "chapter": chapter.id, "offering": offering.id, "tokens": tokens, "users": users}


def request(client, context, method, path, who="material-owner", **kwargs):
    return getattr(client, method)(API + path, headers=auth_header(context["tokens"][who]), **kwargs)


def create_file(client, context):
    response = request(client, context, "post", f"/courses/{context['cid']}/materials", json={
        "title": "第一章讲义", "chapter_id": context["chapter"], "source": "file"})
    assert response.status_code == 201, response.text
    row = response.json()
    response = request(client, context, "put", f"/materials/{row['id']}/file", data={"version": row["version"]},
        files={"file": ("第一章.pdf", PDF, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, context, row):
    response = request(client, context, "patch", f"/materials/{row['id']}", json={"version": row["version"], "status": "published"})
    assert response.status_code == 200, response.text
    return response.json()


def test_content_owner_writes_and_assigned_teacher_only_reads(client, context):
    row = create_file(client, context)
    for who in ("material-teacher", "material-student", "material-other"):
        response = request(client, context, "patch", f"/materials/{row['id']}", who=who,
            json={"version": row["version"], "title": "越权修改"})
        assert response.status_code == 403
    teacher = request(client, context, "get", f"/courses/{context['cid']}/materials", who="material-teacher")
    assert teacher.status_code == 200 and teacher.json()["can_edit"] is False
    assert request(client, context, "get", f"/courses/{context['cid']}/materials", who="material-other").status_code == 403
    student = request(client, context, "get", f"/courses/{context['cid']}/materials", who="material-student")
    assert student.json()["items"] == []
    assert request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student").status_code == 404
    publish(client, context, row)
    student = request(client, context, "get", f"/courses/{context['cid']}/materials", who="material-student")
    assert len(student.json()["items"]) == 1
    assert student.json()["items"][0]["history"] == []
    assert "storage_object_id" not in student.json()["items"][0]


def test_signed_download_range_and_revocation(client, context, db_session_factory):
    row = publish(client, context, create_file(client, context))
    access = request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student",
        params={"offering_id": context["offering"], "preview": True})
    assert access.status_code == 200, access.text
    url = access.json()["url"]
    response = client.get(url, headers={"Range": "bytes=0-8"})
    assert response.status_code == 206 and response.content == PDF[:9]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith("inline")
    with db_session_factory() as db:
        member = db.query(OfferingEnrollment).filter_by(offering_id=context["offering"]).one()
        member.status = "removed"; member.active_scope_key = None; db.commit()
    assert client.get(url).status_code == 403


def test_replacement_invalidates_url_and_preserves_metadata_history(client, context, db_session_factory, test_settings):
    row = publish(client, context, create_file(client, context))
    url = request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student").json()["url"]
    with db_session_factory() as db:
        old_id = db.get(CourseMaterial, row["id"]).storage_object_id
        old_key = db.get(models.StorageObject, old_id).object_key
        assert old_id in business_references(db)
    replacement = request(client, context, "put", f"/materials/{row['id']}/file", data={"version": row["version"]},
        files={"file": ("更新.pdf", minimal_pdf('updated'), "application/pdf")})
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["history"][-1]["filename"] == "第一章.pdf"
    assert client.get(url).status_code == 401
    assert not StorageService.from_settings(test_settings).exists(StorageArea.MATERIALS, old_key)
    assert "storage_object_id" not in replacement.json()


def test_storage_registration_failure_preserves_old_binding(client, context, db_session_factory, test_settings, monkeypatch):
    row = create_file(client, context)
    with db_session_factory() as db:
        old_id = db.get(CourseMaterial, row["id"]).storage_object_id
        old_key = db.get(models.StorageObject, old_id).object_key
        user = db.get(models.User, context["users"]["material-owner"].id)
        material = db.get(CourseMaterial, row["id"])
        from fastapi import UploadFile
        from io import BytesIO
        upload = UploadFile(filename="failed.pdf", file=BytesIO(minimal_pdf('failed')))
        def fail(*_args, **_kwargs):
            raise RuntimeError("database failure")
        monkeypatch.setattr(service, "register_active_object", fail)
        with pytest.raises(RuntimeError):
            service.replace_file(db, material, user, upload, row["version"], test_settings)
        assert db.get(CourseMaterial, row["id"]).storage_object_id == old_id
        storage = StorageService.from_settings(test_settings)
        assert storage.exists(StorageArea.MATERIALS, old_key)
        assert len(storage.list_objects(StorageArea.MATERIALS, "materials")) == 1


def test_invalid_files_limits_and_stale_versions(client, context, test_settings):
    row = create_file(client, context)
    for name, content in (("evil.html", b"<script>"), ("fake.pdf", b"bad"), ("empty.zip", b""), ("bad.ipynb", b"{}")):
        response = request(client, context, "put", f"/materials/{row['id']}/file", data={"version": row["version"]},
            files={"file": (name, content, "application/octet-stream")})
        assert response.status_code == 415, response.text
    test_settings.material_max_upload_bytes = 10
    assert request(client, context, "put", f"/materials/{row['id']}/file", data={"version": row["version"]},
        files={"file": ("large.pdf", PDF, "application/pdf")}).status_code == 413
    assert request(client, context, "patch", f"/materials/{row['id']}", json={"version": 1, "title": "stale"}).status_code == 409


def test_forged_container_replacement_preserves_old_file_and_download(client, context, db_session_factory, test_settings):
    row = publish(client, context, create_file(client, context))
    url = request(client, context, 'get', f"/materials/{row['id']}/access-url", who='material-student').json()['url']
    with db_session_factory() as db:
        old_id = db.get(CourseMaterial, row['id']).storage_object_id
        old_key = db.get(models.StorageObject, old_id).object_key
    for filename, body in [('forged.pdf', b'%PDF-1.7\nforged'), ('broken.zip', b'PK\x03\x04forged')]:
        rejected = request(client, context, 'put', f"/materials/{row['id']}/file", data={'version': row['version']},
            files={'file': (filename, body, 'application/octet-stream')})
        assert rejected.status_code == 415, rejected.text
        with db_session_factory() as db:
            material = db.get(CourseMaterial, row['id'])
            assert (material.storage_object_id, material.version) == (old_id, row['version'])
        storage = StorageService.from_settings(test_settings)
        assert storage.read(StorageArea.MATERIALS, old_key) == PDF
        assert len(storage.list_objects(StorageArea.MATERIALS, 'materials')) == 1
        assert client.get(url).content == PDF


def test_external_link_is_browser_only_and_invalid_urls_rejected(client, context):
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com", "https://example.com\\evil"):
        response = request(client, context, "post", f"/courses/{context['cid']}/materials",
            json={"title": "官网", "source": "external", "external_url": url})
        assert response.status_code == 422, response.text
    response = request(client, context, "post", f"/courses/{context['cid']}/materials",
        json={"title": "Python 官网", "category": "tool", "source": "external", "external_url": "https://www.python.org/downloads/"})
    row = publish(client, context, response.json())
    result = request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student")
    assert result.json() == {"url": "https://www.python.org/downloads/", "expires_at": None, "external": True, "previewable": False}


def test_chapter_links_are_offering_scoped_and_hide_excluded_students(client, context, db_session_factory):
    with db_session_factory() as db:
        seed_basic_environment(db)
        task = models.Assignment(course_id=context["cid"], offering_id=context["offering"], title="本班练习", status="published")
        db.add(task); db.commit(); ident = task.id
    payload = {"chapter_id": context["chapter"], "target_type": "assignment", "target_id": ident}
    result = request(client, context, "post", f"/offerings/{context['offering']}/chapter-activities", who="material-teacher", json=payload)
    assert result.status_code == 201, result.text
    assert request(client, context, "post", f"/offerings/{context['offering']}/chapter-activities", json=payload).status_code == 403
    rows = request(client, context, "get", f"/offerings/{context['offering']}/chapter-activities", who="material-student")
    assert len(rows.json()) == 1
    options = request(client, context, "get", f"/offerings/{context['offering']}/chapter-activity-options", who="material-teacher")
    assert options.json()[0]["title"] == "本班练习"
    with db_session_factory() as db:
        db.add(models.AssignmentAudienceStudent(assignment_id=ident, student_id=context["users"]["material-student"].id, kind="exclude")); db.commit()
    assert request(client, context, "get", f"/offerings/{context['offering']}/chapter-activities", who="material-student").json() == []
    assert request(client, context, "get", f"/offerings/{context['offering']}/chapter-activities", who="material-outsider").status_code == 403


@pytest.mark.parametrize('kind', ['assignment', 'exam'])
def test_admin_chapter_task_links_open_existing_editor_with_material_return_context(client, context, db_session_factory, kind):
    create_user(db_session_factory, 'material-admin', 'admin')
    admin_token = login(client, 'material-admin')[0]
    with db_session_factory() as db:
        task_type = models.Assignment if kind == 'assignment' else models.Exam
        task = task_type(course_id=context['cid'], offering_id=context['offering'], title='章节活动', status='published')
        db.add(task); db.commit(); task_id = task.id
    created = request(client, context, 'post', f"/offerings/{context['offering']}/chapter-activities", who='material-teacher',
        json={'chapter_id': context['chapter'], 'target_type': kind, 'target_id': task_id})
    assert created.status_code == 201, created.text
    response = client.get(f"{API}/offerings/{context['offering']}/chapter-activities", headers=auth_header(admin_token))
    assert response.status_code == 200, response.text
    url = urlsplit(response.json()[0]['route'])
    assert url.path == f'/admin/{kind}s/{task_id}/edit'
    assert parse_qs(url.query) == {'from': ['materials'], 'course_id': [str(context['cid'])], 'offering_id': [str(context['offering'])]}
    teacher = request(client, context, 'get', f"/offerings/{context['offering']}/chapter-activities", who='material-teacher')
    assert teacher.json()[0]['route'] == f"/teacher/{kind}s?offering_id={context['offering']}"
    student = request(client, context, 'get', f"/offerings/{context['offering']}/chapter-activities", who='material-student')
    assert student.json()[0]['route'] == f"/student/{kind}s/{task_id}?offering_id={context['offering']}"


def test_unpublish_archive_and_session_rotation_revoke_media(client, context, db_session_factory):
    row = publish(client, context, create_file(client, context))
    url = request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student").json()["url"]
    result = request(client, context, "patch", f"/materials/{row['id']}", json={"version": row["version"], "status": "draft"})
    assert result.status_code == 200
    assert client.get(url).status_code == 404
    row = publish(client, context, result.json())
    url = request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student").json()["url"]
    with db_session_factory() as db:
        user = db.get(models.User, context["users"]["material-student"].id)
        user.session_version += 1; db.commit()
    assert client.get(url).status_code == 401
    assert request(client, context, "delete", f"/materials/{row['id']}", params={"version": row["version"]}).status_code == 204
    assert request(client, context, "get", f"/courses/{context['cid']}/materials").json()["items"] == []


def test_invalid_chapter_and_other_offering_targets_are_rejected(client, context, db_session_factory):
    other_cid = create_course_db(db_session_factory, teacher_username="material-other", status="published")
    with db_session_factory() as db:
        other_chapter = models.Chapter(course_id=other_cid, title="其他课程章节")
        other_assignment = models.Assignment(course_id=other_cid, offering_id=None, title="未授权活动", status="draft")
        db.add_all([other_chapter, other_assignment]); db.commit()
        chapter_id, task_id = other_chapter.id, other_assignment.id
    result = request(client, context, "post", f"/courses/{context['cid']}/materials", json={"title": "跨课程", "chapter_id": chapter_id})
    assert result.status_code == 422
    result = request(client, context, "post", f"/offerings/{context['offering']}/chapter-activities", who="material-teacher",
        json={"chapter_id": context["chapter"], "target_type": "assignment", "target_id": task_id})
    assert result.status_code == 422


def test_material_objects_work_with_s3_private_backend(client, context, db_session_factory, test_settings):
    import boto3
    from moto import mock_aws
    with mock_aws():
        test_settings.storage_backend = "s3"
        test_settings.storage_s3_endpoint_url = None
        test_settings.storage_s3_bucket = "material-contract"
        test_settings.storage_s3_region = "us-east-1"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="material-contract")
        row = publish(client, context, create_file(client, context))
        with db_session_factory() as db:
            material = db.get(CourseMaterial, row["id"])
            obj = db.get(models.StorageObject, material.storage_object_id)
            assert obj.backend == "s3" and obj.namespace == "course-materials"
            assert StorageService.from_settings(test_settings).read(StorageArea.MATERIALS, obj.object_key) == PDF
        url = request(client, context, "get", f"/materials/{row['id']}/access-url", who="material-student").json()["url"]
        assert client.get(url).content == PDF
        assert request(client, context, "delete", f"/materials/{row['id']}", params={"version": row["version"]}).status_code == 204
        assert client.get(url).status_code == 404


def test_gc_rechecks_a_material_binding_created_after_scan(client, context, db_session_factory, test_settings):
    from datetime import datetime, timedelta, timezone
    from app.services.storage_reconcile import StorageReconcileService
    from app.services.storage_gc import StorageGarbageCollector
    row = create_file(client, context)
    now = datetime.now(timezone.utc)
    storage = StorageService.from_settings(test_settings)
    with db_session_factory() as db:
        material = db.get(CourseMaterial, row["id"])
        object_id = material.storage_object_id
        key = db.get(models.StorageObject, object_id).object_key
        material.storage_object_id = None; db.commit()
        StorageReconcileService(db, storage, quarantine_retention=timedelta(seconds=1)).scan(now=now, dry_run=False)
        material.storage_object_id = object_id; db.commit()
        result = StorageGarbageCollector(db, storage).collect(now=now + timedelta(seconds=2), dry_run=False)
        assert result.skipped_referenced_count == 1
        assert storage.exists(StorageArea.MATERIALS, key)


def test_shared_material_content_is_editable_after_legacy_term_closes(client, context, db_session_factory):
    with db_session_factory() as db:
        term = db.get(models.AcademicTerm, db.get(models.Course, context["cid"]).academic_term_id)
        term.status = "closed"; db.commit()
    response = request(client, context, "post", f"/courses/{context['cid']}/materials", json={"title": "跨学期复用的资料"})
    assert response.status_code == 201, response.text
    assert request(client, context, "get", f"/courses/{context['cid']}/materials").json()["can_edit"] is True
    response = request(client, context, "post", f"/offerings/{context['offering']}/chapter-activities", who="material-teacher",
        json={"chapter_id": context["chapter"], "target_type": "assignment", "target_id": 999})
    assert response.status_code == 409
