"""作业发布时间与硬截止行为。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from conftest import auth_header, create_course_db, create_user, login
from app.api import judge as judge_api
from app.models import Assignment
from teaching_test_support import grant_course_students


def _setup(client, db_session_factory):
    create_user(db_session_factory, "schedule_teacher", "teacher")
    create_user(db_session_factory, "schedule_other", "teacher")
    create_user(db_session_factory, "schedule_student", "student")
    teacher_token, _ = login(client, "schedule_teacher")
    other_token, _ = login(client, "schedule_other")
    student_token, _ = login(client, "schedule_student")
    course_id = create_course_db(
        db_session_factory, teacher_username="schedule_teacher",
        title="作业时间测试课", status="published", visibility="public",
    )
    client.post(f"/api/v1/courses/{course_id}/enroll", headers=auth_header(student_token))
    return teacher_token, other_token, student_token, course_id


def _assignment(client, db_sf, teacher_token, course_id, *, due_at=None):
    response = client.post(
        "/api/v1/assignments",
        headers=auth_header(teacher_token),
        json={
            "course_id": course_id,
            "title": "时间测试作业",
            "status": "draft",
            "due_at": due_at,
        },
    )
    assert response.status_code == 201, response.text
    grant_course_students(db_sf, course_id, ["schedule_student"])
    return response.json()


def _question(client, teacher_token, assignment_id):
    response = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        headers=auth_header(teacher_token),
        json={
            "title": "空样例题",
            "function_name": "answer",
            "signature": "def answer():",
            "public_cases": [],
            "hidden_tests": "",
            "grading_mode": "legacy",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _published_assignment_with_question(client, db_sf, teacher_token, course_id, *, due_at=None):
    assignment = _assignment(client, db_sf, teacher_token, course_id, due_at=due_at)
    question_id = _question(client, teacher_token, assignment["id"])
    response = client.post(
        f"/api/v1/assignments/{assignment['id']}/publish",
        headers=auth_header(teacher_token),
    )
    assert response.status_code == 200, response.text
    return response.json(), question_id


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def test_first_published_at_survives_unpublish_and_republish(client, db_session_factory):
    teacher, _, _, course_id = _setup(client, db_session_factory)
    assignment = _assignment(client, db_session_factory, teacher, course_id)
    _question(client, teacher, assignment["id"])

    first = client.post(
        f"/api/v1/assignments/{assignment['id']}/publish",
        headers=auth_header(teacher),
    )
    assert first.status_code == 200, first.text
    first_published_at = first.json()["published_at"]
    assert first_published_at

    client.post(
        f"/api/v1/assignments/{assignment['id']}/unpublish",
        headers=auth_header(teacher),
    )
    second = client.post(
        f"/api/v1/assignments/{assignment['id']}/publish",
        headers=auth_header(teacher),
    )
    assert second.status_code == 200, second.text
    assert second.json()["published_at"] == first_published_at


def test_direct_published_create_gets_first_published_at(client, db_session_factory):
    """首次发布写入 published_at（创建旁路已关闭：只能经 /publish 进入 published）"""
    teacher, _, _, course_id = _setup(client, db_session_factory)
    assignment = _assignment(client, db_session_factory, teacher, course_id)
    assert assignment["published_at"] is None
    _question(client, teacher, assignment["id"])
    published = client.post(
        f"/api/v1/assignments/{assignment['id']}/publish",
        headers=auth_header(teacher),
    )
    assert published.status_code == 200, published.text
    assert published.json()["published_at"]


def test_deadline_blocks_sample_run_and_submission(client, db_session_factory):
    teacher, _, student, course_id = _setup(client, db_session_factory)
    assignment, question_id = _published_assignment_with_question(
        client, db_session_factory, teacher, course_id, due_at=_iso(timedelta(minutes=-1))
    )

    sample = client.post(
        f"/api/v1/judge/questions/{question_id}/sample-run",
        headers=auth_header(student),
        json={"question_id": question_id, "code": "def answer(): return 42"},
    )
    submit = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student),
        json={"question_id": question_id, "code": "def answer(): return 42"},
    )

    for response in (sample, submit):
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "ASSIGNMENT_DEADLINE_PASSED"


def test_deadline_boundary_is_inclusive(monkeypatch):
    boundary = datetime(2026, 8, 12, 10, 30, tzinfo=timezone.utc)
    assignment = Assignment(course_id=1, title="边界作业", due_at=boundary)
    monkeypatch.setattr(judge_api, "utc_now", lambda: boundary)

    with pytest.raises(HTTPException) as error:
        judge_api.require_assignment_before_deadline(assignment)

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "ASSIGNMENT_DEADLINE_PASSED"


def test_extending_deadline_reopens_sample_run(client, db_session_factory):
    teacher, _, student, course_id = _setup(client, db_session_factory)
    assignment, question_id = _published_assignment_with_question(
        client, db_session_factory, teacher, course_id, due_at=_iso(timedelta(minutes=-1))
    )

    blocked = client.post(
        f"/api/v1/judge/questions/{question_id}/sample-run",
        headers=auth_header(student),
        json={"question_id": question_id, "code": "def answer(): return 42"},
    )
    assert blocked.status_code == 403

    extended = client.patch(
        f"/api/v1/assignments/{assignment['id']}",
        headers=auth_header(teacher),
        json={"due_at": _iso(timedelta(hours=1))},
    )
    assert extended.status_code == 200, extended.text

    reopened = client.post(
        f"/api/v1/judge/questions/{question_id}/sample-run",
        headers=auth_header(student),
        json={"question_id": question_id, "code": "def answer(): return 42"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "no_public_cases"


def test_clearing_deadline_reopens_assignment(client, db_session_factory):
    teacher, _, student, course_id = _setup(client, db_session_factory)
    assignment, question_id = _published_assignment_with_question(
        client, db_session_factory, teacher, course_id, due_at=_iso(timedelta(minutes=-1))
    )

    cleared = client.patch(
        f"/api/v1/assignments/{assignment['id']}",
        headers=auth_header(teacher),
        json={"due_at": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["due_at"] is None

    reopened = client.post(
        f"/api/v1/judge/questions/{question_id}/sample-run",
        headers=auth_header(student),
        json={"question_id": question_id, "code": "def answer(): return 42"},
    )
    assert reopened.status_code == 200, reopened.text


def test_no_deadline_remains_open_and_non_manager_cannot_change_it(client, db_session_factory):
    teacher, other, student, course_id = _setup(client, db_session_factory)
    assignment, question_id = _published_assignment_with_question(client, db_session_factory, teacher, course_id)

    open_response = client.post(
        f"/api/v1/judge/questions/{question_id}/sample-run",
        headers=auth_header(student),
        json={"question_id": question_id, "code": "def answer(): return 42"},
    )
    assert open_response.status_code == 200, open_response.text

    forbidden = client.patch(
        f"/api/v1/assignments/{assignment['id']}",
        headers=auth_header(other),
        json={"due_at": _iso(timedelta(days=1))},
    )
    assert forbidden.status_code == 404, forbidden.text  # Scoped lookup hides another teacher's task.

    with db_session_factory() as db:
        assert db.get(Assignment, assignment["id"]).due_at is None
