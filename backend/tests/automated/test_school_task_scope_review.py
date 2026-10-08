"""Native class task and one-time historical attribution regressions."""
import pytest
from sqlalchemy import func, select

from app import models
from conftest import create_course_db, make_template_version
from test_school_offerings import school  # noqa: F401


@pytest.mark.parametrize("kind,model", [("assignments", models.Assignment), ("exams", models.Exam)])
def test_native_task_creation_binds_only_own_offering(client, school, db_session_factory, kind, model):  # noqa: F811
    payload = {"course_id": school["course"], "offering_id": school["offerings"][1], "title": "本班原创任务"}
    response = client.post(f"/api/v1/{kind}", json=payload, headers=school["headers"]["other"])
    assert response.status_code == 201, response.text
    assert response.json()["offering_id"] == school["offerings"][1]
    assert response.json()["status"] == "draft"
    task_id = response.json()["id"]
    with db_session_factory() as db:
        task = db.get(model, task_id)
        assert task.created_by_id == school["users"]["other"].id
    assert client.get(f"/api/v1/{kind}/{task_id}", headers=school["headers"]["owner"]).status_code == 404
    assert client.get(f"/api/v1/{kind}/{task_id}", headers=school["headers"]["b"]).status_code == 403
    assert client.post(f"/api/v1/{kind}", json={**payload, "status": "published"}, headers=school["headers"]["other"]).status_code == 422
    assert client.post(f"/api/v1/{kind}", json={"course_id": school["course"], "title": "越权模板"}, headers=school["headers"]["other"]).status_code == 403


@pytest.mark.parametrize("kind,model", [("assignments", models.Assignment), ("exams", models.Exam)])
def test_native_task_scope_rejection_has_no_database_write(client, school, db_session_factory, kind, model):  # noqa: F811
    other_course = create_course_db(db_session_factory, teacher_username=school["users"]["owner"].username)
    payload = {"course_id": school["course"], "offering_id": school["offerings"][1], "title": "拒绝越权"}
    with db_session_factory() as db:
        before = db.scalar(select(func.count()).select_from(model))
    assert client.post(f"/api/v1/{kind}", json=payload, headers=school["headers"]["owner"]).status_code == 403
    mismatched = client.post(f"/api/v1/{kind}", json={**payload, "course_id": other_course}, headers=school["headers"]["other"])
    assert mismatched.status_code == 422
    assert mismatched.json()["detail"]["code"] == "TASK_SCOPE_MISMATCH"
    assert client.post(f"/api/v1/{kind}", json={**payload, "offering_id": 0}, headers=school["headers"]["other"]).status_code == 422
    archived = client.patch(f"/api/v1/offerings/{school['offerings'][1]}", json={"status": "archived", "reason": "结课"}, headers=school["headers"]["admin"])
    assert archived.status_code == 200
    assert client.post(f"/api/v1/{kind}", json=payload, headers=school["headers"]["other"]).status_code == 409
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(model)) == before


@pytest.mark.parametrize("kind,model", [("assignments", models.Assignment), ("exams", models.Exam)])
def test_native_task_audience_cannot_store_another_classes_student_ids(client, school, db_session_factory, kind, model):  # noqa: F811
    payload = {"course_id": school["course"], "offering_id": school["offerings"][0], "title": "本班任务"}
    response = client.post(f"/api/v1/{kind}", json=payload, headers=school["headers"]["owner"])
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    for field in ("whitelist_student_ids", "excluded_student_ids"):
        invalid_create = client.post(f"/api/v1/{kind}", json={**payload, field: [school["users"]["b"].id]}, headers=school["headers"]["owner"])
        assert invalid_create.status_code == 422, invalid_create.text
        rejected = client.patch(f"/api/v1/{kind}/{task_id}", json={field: [school["users"]["b"].id]}, headers=school["headers"]["owner"])
        assert rejected.status_code == 422, rejected.text
        missing = client.patch(f"/api/v1/{kind}/{task_id}", json={field: [999999999]}, headers=school["headers"]["owner"])
        assert missing.status_code == 422, missing.text
        assert rejected.json()["detail"] == missing.json()["detail"]
    class_rejected = client.patch(f"/api/v1/{kind}/{task_id}", json={"audience_class_ids": [school["classes"][1]]}, headers=school["headers"]["owner"])
    assert class_rejected.status_code == 422
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(model)) == 1
        task = db.get(model, task_id)
        assert task.whitelist_student_ids == []
        assert task.excluded_student_ids == []
    valid = client.patch(f"/api/v1/{kind}/{task_id}", json={"whitelist_student_ids": [school["users"]["a"].id]}, headers=school["headers"]["owner"])
    assert valid.status_code == 200, valid.text


@pytest.mark.parametrize("kind", ["assignments", "exams"])
def test_native_task_audience_csv_cannot_resolve_another_classes_identity(client, school, kind):  # noqa: F811
    response = client.post(f"/api/v1/{kind}", json={"course_id": school["course"], "offering_id": school["offerings"][0], "title": "本班导入"}, headers=school["headers"]["owner"])
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    imported = client.post(f"/api/v1/{kind}/{task_id}/audience/import", params={"kind": "include"}, headers=school["headers"]["owner"],
                           files={"file": ("students.csv", b"username\nschool-b\nmissing-student\n", "text/csv")})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert (body["created"], body["updated"], body["skipped"]) == (0, 0, 2)
    assert body["errors"][0]["message"] == body["errors"][1]["message"]
    saved = client.get(f"/api/v1/{kind}/{task_id}", headers=school["headers"]["owner"])
    assert saved.json()["whitelist_student_ids"] == []
    included = client.post(f"/api/v1/{kind}/{task_id}/audience/import", params={"kind": "include"}, headers=school["headers"]["owner"],
                           files={"file": ("students.csv", b"username,name\nschool-a,school-a\n", "text/csv")})
    assert included.status_code == 200, included.text
    assert included.json()["created"] == 1
    saved = client.get(f"/api/v1/{kind}/{task_id}", headers=school["headers"]["owner"])
    assert saved.json()["whitelist_student_ids"] == [school["users"]["a"].id]


def test_legacy_record_assignment_is_admin_only_and_cannot_overwrite_attribution(client, school, db_session_factory):  # noqa: F811
    version_id = make_template_version(db_session_factory)
    with db_session_factory() as db:
        record = models.ExperimentRecord(lesson_id=school["lessons"][0], template_version_id=version_id, student_id=school["users"]["a"].id)
        db.add(record); db.commit(); record_id = record.id
    path = f"/api/v1/offerings/{school['offerings'][0]}/legacy-records/{record_id}/assign"
    assert client.post(path, json={"reason": "确认历史班级"}, headers=school["headers"]["owner"]).status_code == 403
    response = client.post(path, json={"reason": "确认历史班级"}, headers=school["headers"]["admin"])
    assert response.status_code == 200, response.text
    assert client.post(path, json={"reason": "重复归属"}, headers=school["headers"]["admin"]).status_code == 409
    with db_session_factory() as db:
        record = db.get(models.ExperimentRecord, record_id)
        assert (record.offering_id, record.scope_key) == (school["offerings"][0], f"offering:{school['offerings'][0]}")


def test_duplicate_historical_entry_rejected_without_overwriting_either_record(client, school, db_session_factory):  # noqa: F811
    version_id = make_template_version(db_session_factory)
    with db_session_factory() as db:
        common = {"lesson_id": school["lessons"][0], "template_version_id": version_id, "student_id": school["users"]["a"].id}
        old = models.ExperimentRecord(**common)
        current = models.ExperimentRecord(**common, offering_id=school["offerings"][0], scope_key=f"offering:{school['offerings'][0]}")
        db.add_all([old, current]); db.commit(); old_id, current_id = old.id, current.id
    response = client.post(f"/api/v1/offerings/{school['offerings'][0]}/legacy-records/{old_id}/assign", json={"reason": "历史归属"}, headers=school["headers"]["admin"])
    assert response.status_code == 409, response.text
    with db_session_factory() as db:
        assert db.get(models.ExperimentRecord, old_id).offering_id is None
        assert db.get(models.ExperimentRecord, current_id).offering_id == school["offerings"][0]
