"""Course authorization must retain its source and respect administrative freezes."""
import pytest

from app.models import CourseEnrollment, TeachingClassStudent
from app.models.teaching import OfferingEnrollment
from conftest import auth_header, create_course_db, create_user, login

API = "/api/v1"


@pytest.fixture()
def roster(client, db_session_factory):
    admin = create_user(db_session_factory, "roster-admin", "admin")
    teacher = create_user(db_session_factory, "roster-teacher", "teacher")
    student = create_user(db_session_factory, "roster-student", "student")
    admin_headers = auth_header(login(client, admin.username)[0])
    teacher_headers = auth_header(login(client, teacher.username)[0])
    student_headers = auth_header(login(client, student.username)[0])
    term = client.post(f"{API}/academic-terms", headers=admin_headers, json={
        "code": "2026-FALL", "name": "Fall", "start_date": "2026-09-01",
        "end_date": "2027-01-20", "status": "active",
    })
    assert term.status_code == 201, term.text
    created = client.post(f"{API}/teaching-classes", headers=admin_headers, json={
        "academic_term_id": term.json()["id"], "code": "CS-01", "name": "CS class",
    })
    assert created.status_code == 201, created.text
    class_id = created.json()["id"]
    course_id = create_course_db(db_session_factory, teacher_username=teacher.username,
                                 academic_term_id=term.json()["id"], status="published", visibility="public")
    denied = client.patch(f"{API}/courses/{course_id}", headers=teacher_headers, json={"teaching_class_ids": [class_id]})
    assert denied.status_code == 403, denied.text
    linked = client.post(f"{API}/offerings", headers=admin_headers, json={
        "course_id": course_id, "teaching_class_id": class_id,
        "academic_term_id": term.json()["id"], "teacher_id": teacher.id,
    })
    assert linked.status_code == 201, linked.text
    return student, admin_headers, teacher_headers, student_headers, class_id, course_id


def _join_class(client, roster):
    student, admin, _, _, class_id, _ = roster
    response = client.post(f"{API}/teaching-classes/{class_id}/students", headers=admin, json={"student_ids": [student.id]})
    assert response.status_code == 200, response.text


def _manual_add(client, roster):
    student, admin, _, _, _, course_id = roster
    offerings = client.get(f"{API}/offerings", headers=admin, params={"course_id": course_id})
    assert offerings.status_code == 200, offerings.text
    offering_id = offerings.json()["items"][0]["id"]
    response = client.post(f"{API}/offerings/{offering_id}/students", headers=admin,
                           json={"student_ids": [student.id], "reason": "管理员批准补选"})
    assert response.status_code == 200, response.text
    return offering_id


@pytest.mark.parametrize("origin", ["class", "manual"])
def test_repeat_self_enroll_preserves_authoritative_origin(client, db_session_factory, roster, origin):
    student, admin, teacher, headers, _, course_id = roster
    if origin == "class":
        _join_class(client, roster)
    else:
        denied = client.post(f"{API}/courses/{course_id}/students", headers=teacher, json={"student_id": student.id})
        assert denied.status_code == 403, denied.text
        offering_id = _manual_add(client, roster)
        with db_session_factory() as db:
            authority = db.query(OfferingEnrollment).filter_by(offering_id=offering_id, student_id=student.id).one()
            assert (authority.status, authority.origin) == ("active", "manual")
    response = client.post(f"{API}/courses/{course_id}/enroll", headers=headers)
    assert response.status_code in (200, 201), response.text
    with db_session_factory() as db:
        row = db.query(CourseEnrollment).filter_by(course_id=course_id, student_id=student.id).one()
        assert (row.status, row.origin) == ("enrolled", origin)
    assert client.delete(f"{API}/courses/{course_id}/enroll", headers=headers).status_code == 409


def test_manual_removal_cannot_be_undone_in_public_course(client, db_session_factory, roster):
    student, admin, teacher, headers, _, course_id = roster
    _join_class(client, roster)
    assert client.delete(f"{API}/courses/{course_id}/students/{student.id}", headers=teacher).status_code == 403
    assert client.delete(f"{API}/courses/{course_id}/students/{student.id}", headers=admin).status_code == 204
    response = client.post(f"{API}/courses/{course_id}/enroll", headers=headers)
    assert response.status_code == 403, response.text
    with db_session_factory() as db:
        row = db.query(CourseEnrollment).filter_by(course_id=course_id, student_id=student.id).one()
        assert (row.status, row.origin) == ("dropped", "manual")


def test_formal_class_assignment_reactivates_previous_self_withdrawal(client, db_session_factory, roster):
    student, _, _, headers, _, course_id = roster
    # 新系统拒绝自行授权；直接保留一条旧系统历史退课投影模拟升级数据。
    assert client.post(f"{API}/courses/{course_id}/enroll", headers=headers).status_code == 403
    with db_session_factory() as db:
        db.add(CourseEnrollment(course_id=course_id, student_id=student.id, status="dropped", origin="self"))
        db.commit()
    _join_class(client, roster)
    with db_session_factory() as db:
        row = db.query(CourseEnrollment).filter_by(course_id=course_id, student_id=student.id).one()
        assert (row.status, row.origin) == ("enrolled", "class")


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_archived_class_freezes_membership(client, db_session_factory, roster, operation):
    student, admin, _, _, class_id, _ = roster
    if operation == "remove":
        _join_class(client, roster)
    archived = client.delete(f"{API}/teaching-classes/{class_id}", headers=admin)
    assert archived.status_code == 200, archived.text
    if operation == "add":
        response = client.post(f"{API}/teaching-classes/{class_id}/students", headers=admin, json={"student_ids": [student.id]})
    else:
        response = client.delete(f"{API}/teaching-classes/{class_id}/students/{student.id}", headers=admin)
    assert response.status_code == 409, response.text
    with db_session_factory() as db:
        row = db.query(TeachingClassStudent).filter_by(teaching_class_id=class_id, student_id=student.id).one_or_none()
        assert (row is None) if operation == "add" else row.status == "active"


def test_administrator_manual_enrollment_survives_class_exit(client, db_session_factory, roster):
    student, admin, _, headers, class_id, course_id = roster
    assert client.post(f"{API}/courses/{course_id}/enroll", headers=headers).status_code == 403
    offering_id = _manual_add(client, roster)
    _join_class(client, roster)
    assert client.delete(f"{API}/teaching-classes/{class_id}/students/{student.id}", headers=admin).status_code == 204
    with db_session_factory() as db:
        row = db.query(CourseEnrollment).filter_by(course_id=course_id, student_id=student.id).one()
        assert (row.status, row.origin) == ("enrolled", "manual")
        authority = db.query(OfferingEnrollment).filter_by(offering_id=offering_id, student_id=student.id).one()
        assert (authority.status, authority.origin) == ("active", "manual")
    assert client.get(f"{API}/courses/{course_id}", headers=headers).status_code == 200


@pytest.mark.parametrize('removed', [False, True])
def test_csv_reimport_neither_rewrites_unchanged_nor_restores_manual_removal(client, db_session_factory, roster, removed):
    student, admin, teacher, _, _, course_id = roster
    _join_class(client, roster)
    if removed:
        assert client.delete(f'{API}/courses/{course_id}/students/{student.id}', headers=admin).status_code == 204
    denied = client.post(f'{API}/courses/{course_id}/students/import', headers=teacher,
                         files={'file': ('roster.csv', f'username\n{student.username}\n'.encode(), 'text/csv')})
    assert denied.status_code == 403, denied.text
    response = client.post(f'{API}/courses/{course_id}/students/import', headers=admin,
                           files={'file': ('roster.csv', f'username\n{student.username}\n'.encode(), 'text/csv')})
    assert response.status_code == 200, response.text
    assert response.json()['skipped'] == 1
    assert response.json()['updated'] == 0
    with db_session_factory() as db:
        row = db.query(CourseEnrollment).filter_by(course_id=course_id, student_id=student.id).one()
        assert (row.status, row.origin) == (('dropped', 'manual') if removed else ('enrolled', 'class'))
