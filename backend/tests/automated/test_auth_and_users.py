from __future__ import annotations

from conftest import auth_header, create_user, grant_teaching_fixture, login


def test_admin_can_create_user_and_logout_blacklists_token(client, db_session_factory):
    create_user(db_session_factory, "admin", "admin")
    access_token, refresh_token = login(client, "admin")

    me_response = client.get("/api/v1/auth/me", headers=auth_header(access_token))
    assert me_response.status_code == 200
    assert me_response.json()["role"] == "admin"

    create_response = client.post(
        "/api/v1/users",
        headers=auth_header(access_token),
        json={
            "username": "student001",
            "password": "Passw0rd!",
            "real_name": "Student One",
            "student_no": "student001",
            "role": "student",
        },
    )
    assert create_response.status_code == 201, create_response.text
    assert create_response.json()["username"] == "student001"
    assert 'password' not in create_response.json()
    assert 'password_hash' not in create_response.json()
    assert create_response.json()['must_change_password'] is True

    list_response = client.get("/api/v1/users", headers=auth_header(access_token))
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 2

    refresh_response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_response.status_code == 200
    assert refresh_response.json()["access_token"] != access_token

    logout_response = client.post(
        "/api/v1/auth/logout",
        headers=auth_header(access_token),
        json={"refresh_token": refresh_token},
    )
    assert logout_response.status_code == 204

    blocked_response = client.get("/api/v1/auth/me", headers=auth_header(access_token))
    assert blocked_response.status_code == 401
    assert blocked_response.json()["detail"]["code"] == "TOKEN_REVOKED"


def test_admin_user_list_exactly_matches_username_student_no_or_real_name(client, db_session_factory):
    create_user(db_session_factory, "search-admin", "admin")
    create_user(db_session_factory, "search-username", "teacher", real_name="其他用户")
    create_user(db_session_factory, "search-student", "student", real_name="李四")
    create_user(db_session_factory, "search-name", "student", real_name="张三")

    with db_session_factory() as db:
        from sqlalchemy import select
        from app.models import User

        student = db.scalar(select(User).where(User.username == "search-student"))
        student.student_no = "20260001"
        db.commit()

    access_token, _ = login(client, "search-admin")
    headers = auth_header(access_token)

    for query, expected_username in (
        ("search-username", "search-username"),
        ("20260001", "search-student"),
        ("张三", "search-name"),
    ):
        response = client.get("/api/v1/users", headers=headers, params={"q": query})
        assert response.status_code == 200, response.text
        assert [item["username"] for item in response.json()["items"]] == [expected_username]

    partial_response = client.get("/api/v1/users", headers=headers, params={"q": "search-stu"})
    assert partial_response.status_code == 200
    assert partial_response.json()["items"] == []


def test_teacher_student_candidate_search_matches_partial_student_no(client, db_session_factory):
    teacher = create_user(db_session_factory, "student-search-teacher", "teacher")
    create_user(
        db_session_factory,
        "student-search-target",
        "student",
        real_name="目标学生",
    )
    outsider = create_user(db_session_factory, 'student-search-outsider', 'student')
    with db_session_factory() as db:
        from sqlalchemy import select
        from app.models import Course, User

        student = db.scalar(select(User).where(User.username == "student-search-target"))
        student.student_no = "2026001234"
        db.get(User, outsider.id).student_no = '202699001234'
        course = Course(title='Student search fixture', teacher_id=teacher.id, status='published')
        db.add(course)
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    access_token, _ = login(client, "student-search-teacher")
    response = client.get(
        "/api/v1/users/students",
        headers=auth_header(access_token),
        params={"q": "001234", "page": 1, "page_size": 20},
    )

    assert response.status_code == 200, response.text
    assert [item["username"] for item in response.json()["items"]] == [
        "student-search-target"
    ]


def test_student_cannot_create_users(client, db_session_factory):
    create_user(db_session_factory, "student", "student")
    access_token, _ = login(client, "student")

    response = client.post(
        "/api/v1/users",
        headers=auth_header(access_token),
        json={
            "username": "teacher001",
            "password": "Passw0rd!",
            "real_name": "Teacher One",
            "role": "teacher",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


def test_admin_can_only_assign_supported_user_roles(client, db_session_factory):
    create_user(db_session_factory, "roles-admin", "admin")
    access_token, _ = login(client, "roles-admin")
    headers = auth_header(access_token)

    create_response = client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "username": "legacy-role-user",
            "password": "Passw0rd!",
            "real_name": "Legacy Role",
            "role": "legacy",
        },
    )
    assert create_response.status_code == 422, create_response.text

    target = create_user(db_session_factory, "roles-target", "student")
    update_response = client.patch(
        f"/api/v1/users/{target.id}",
        headers=headers,
        json={"role": "legacy"},
    )
    assert update_response.status_code == 422, update_response.text


def test_unsupported_database_role_cannot_login(client, db_session_factory):
    create_user(db_session_factory, "legacy-role-login", "legacy")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "legacy-role-login", "password": "Passw0rd!"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "ROLE_NOT_SUPPORTED"
