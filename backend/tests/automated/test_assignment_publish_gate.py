"""作业发布门禁测试：POST 仅 draft、PATCH 不可改 status、发布需至少一题、零题不视为已提交。"""

from conftest import auth_header, create_course_db, create_user, login
from teaching_test_support import grant_course_students

API = "/api/v1"


def _setup_teacher(client, db_session_factory):
    create_user(db_session_factory, "teacher", "teacher")
    student = create_user(db_session_factory, "audience-student", "student")
    token, _ = login(client, "teacher")
    course_id = create_course_db(db_session_factory, teacher_username="teacher", status="draft")
    grant_course_students(db_session_factory, course_id, ["audience-student"])
    return token, course_id


def _create_draft_assignment(client, db_sf, token, course_id, title="作业"):
    response = client.post(
        f"{API}/assignments",
        headers=auth_header(token),
        json={"course_id": course_id, "title": title, "status": "draft"},
    )
    assert response.status_code == 201, response.text
    grant_course_students(db_sf, course_id, ["audience-student"])
    return response.json()


def test_create_assignment_rejects_published_status(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    response = client.post(
        f"{API}/assignments",
        headers=auth_header(token),
        json={"course_id": course_id, "title": "Bypass", "status": "published"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_create_assignment_accepts_draft(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    assignment = _create_draft_assignment(client, db_session_factory, token, course_id)
    assert assignment["status"] == "draft"


def test_patch_cannot_change_status(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    assignment = _create_draft_assignment(client, db_session_factory, token, course_id)
    response = client.patch(
        f"{API}/assignments/{assignment['id']}",
        headers=auth_header(token),
        json={"status": "published"},
    )
    # status 字段被忽略：更新成功但状态必须保持 draft（发布只能走 /publish）
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"


def test_publish_requires_at_least_one_question(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    assignment = _create_draft_assignment(client, db_session_factory, token, course_id)
    response = client.post(
        f"{API}/assignments/{assignment['id']}/publish",
        headers=auth_header(token),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "ASSIGNMENT_HAS_NO_QUESTIONS"


def test_publish_succeeds_with_question(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    assignment = _create_draft_assignment(client, db_session_factory, token, course_id)
    question = client.post(
        f"{API}/assignments/{assignment['id']}/questions",
        headers=auth_header(token),
        json={
            "title": "两数相加",
            "grading_mode": "legacy",
            "function_name": "add",
            "signature": "def add(a: int, b: int) -> int",
            "starter_code": "def add(a, b):\n    return 0\n",
            "public_cases": [{"args": [1, 2], "expected": 3}],
            "hidden_tests": "def test_add():\n    assert user_code.add(1, 2) == 3\n",
        },
    )
    assert question.status_code == 201, question.text
    response = client.post(
        f"{API}/assignments/{assignment['id']}/publish",
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"


def test_unpublish_returns_to_draft(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    assignment = _create_draft_assignment(client, db_session_factory, token, course_id)
    client.post(
        f"{API}/assignments/{assignment['id']}/questions",
        headers=auth_header(token),
        json={
            "title": "题",
            "grading_mode": "legacy",
            "function_name": "f",
            "hidden_tests": "def test():\n    pass\n",
        },
    )
    assert (
        client.post(
            f"{API}/assignments/{assignment['id']}/publish",
            headers=auth_header(token),
        ).status_code
        == 200
    )
    response = client.post(
        f"{API}/assignments/{assignment['id']}/unpublish",
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"


def test_republish_is_idempotent(client, db_session_factory):
    token, course_id = _setup_teacher(client, db_session_factory)
    assignment = _create_draft_assignment(client, db_session_factory, token, course_id)
    client.post(
        f"{API}/assignments/{assignment['id']}/questions",
        headers=auth_header(token),
        json={
            "title": "题",
            "grading_mode": "legacy",
            "function_name": "f",
            "hidden_tests": "def test():\n    pass\n",
        },
    )
    first = client.post(
        f"{API}/assignments/{assignment['id']}/publish",
        headers=auth_header(token),
    )
    assert first.status_code == 200
    second = client.post(
        f"{API}/assignments/{assignment['id']}/publish",
        headers=auth_header(token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "published"
