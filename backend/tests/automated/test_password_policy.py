"""TASK-011：密码边界与改密语义。

- 所有密码入口共享同一形状校验：≥8 字符、UTF-8 ≤72 字节、不得全空白。
- 密码不得等同规范化用户名。
- 本人改密必须提交正确的 current_password；管理员重置需验证管理员当前密码，无需学生旧密码。
"""

import pytest
from io import BytesIO
from openpyxl import load_workbook

from app import cli
from conftest import auth_header, create_user, login

API = "/api/v1"

RIGHT = "Passw0rd!"


@pytest.mark.parametrize(
    ("username", "password"),
    [("cli_admin", "short"), ("same_admin", "same_admin")],
)
def test_create_admin_cli_rejects_invalid_password_before_database(
    monkeypatch, username, password
):
    def unexpected_database_access(*args, **kwargs):
        raise AssertionError("invalid CLI password reached the database")

    monkeypatch.setattr(cli.Base.metadata, "create_all", unexpected_database_access)

    with pytest.raises(SystemExit, match="密码不符合要求"):
        cli.create_admin(username, password, "Administrator")


def _create_student(client, db_session_factory, username, password=RIGHT):
    create_user(db_session_factory, username, "student", password=password)
    token, _ = login(client, username, password=password)
    return token


def _create_admin(client, db_session_factory):
    create_user(db_session_factory, "pw_admin", "admin")
    token, _ = login(client, "pw_admin")
    return token


def test_create_user_rejects_short_password(client, db_session_factory):
    admin_tok = _create_admin(client, db_session_factory)
    resp = client.post(
        f"{API}/users",
        headers=auth_header(admin_tok),
        json={
            "username": "short_pw", "password": "Abc123!",
            "real_name": "短密码", "role": "student", "student_no": "S9001",
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_create_user_rejects_overlong_multibyte_password(client, db_session_factory):
    admin_tok = _create_admin(client, db_session_factory)
    # 25 个 CJK 字符 = 75 字节 > 72
    resp = client.post(
        f"{API}/users",
        headers=auth_header(admin_tok),
        json={
            "username": "bytes_pw", "password": "密" * 25,
            "real_name": "字节超限", "role": "student", "student_no": "S9002",
        },
    )
    assert resp.status_code == 422, resp.text


def test_create_user_rejects_whitespace_only_password(client, db_session_factory):
    admin_tok = _create_admin(client, db_session_factory)
    resp = client.post(
        f"{API}/users",
        headers=auth_header(admin_tok),
        json={
            "username": "blank_pw", "password": "        ",
            "real_name": "空白密码", "role": "student", "student_no": "S9003",
        },
    )
    assert resp.status_code == 422, resp.text


def test_teacher_password_equal_username_rejected_on_create(client, db_session_factory):
    admin_tok = _create_admin(client, db_session_factory)
    resp = client.post(
        f"{API}/users",
        headers=auth_header(admin_tok),
        json={
            "username": "samepw_1", "password": "samepw_1",
            "real_name": "同名密码", "role": "teacher",
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "PASSWORD_EQUALS_USERNAME"


def test_self_change_requires_correct_current_password(client, db_session_factory):
    token = _create_student(client, db_session_factory, "self_pw")
    me = client.get(f"{API}/auth/me", headers=auth_header(token)).json()
    # 缺 current_password → 422
    resp = client.patch(
        f"{API}/users/{me['id']}/password",
        headers=auth_header(token),
        json={"password": "NewPass123!"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "CURRENT_PASSWORD_INVALID"
    # 错误旧密码 → 422
    resp = client.patch(
        f"{API}/users/{me['id']}/password",
        headers=auth_header(token),
        json={"password": "NewPass123!", "current_password": "WrongOld"},
    )
    assert resp.status_code == 422, resp.text
    # 正确旧密码 → 200，新密码可登录
    resp = client.patch(
        f"{API}/users/{me['id']}/password",
        headers=auth_header(token),
        json={"password": "NewPass123!", "current_password": RIGHT},
    )
    assert resp.status_code == 200, resp.text
    assert login(client, "self_pw", password="NewPass123!")


def test_admin_reset_requires_admin_password_but_not_student_password(client, db_session_factory):
    _create_student(client, db_session_factory, "rst_stu")
    admin_tok = _create_admin(client, db_session_factory)
    target = client.get(f"{API}/users", headers=auth_header(admin_tok), params={"role": "student"}).json()["items"]
    uid = next(u["id"] for u in target if u["username"] == "rst_stu")
    legacy = client.patch(
        f"{API}/users/{uid}/password", headers=auth_header(admin_tok),
        json={"password": "ResetPass1!"},
    )
    assert legacy.status_code == 403, legacy.text
    invalid = client.post(
        f"{API}/users/{uid}/password-reset", headers=auth_header(admin_tok),
        json={"current_password": "WrongPassword!", "reason": "Student requested reset"},
    )
    assert invalid.status_code == 422, invalid.text
    resp = client.post(
        f"{API}/users/{uid}/password-reset", headers=auth_header(admin_tok),
        json={"current_password": RIGHT, "reason": "Student requested reset"},
    )
    assert resp.status_code == 200, resp.text
    deliveries = client.get(f'{API}/admin/credential-deliveries', headers=auth_header(admin_tok), params={'user_id': uid}).json()['items']
    exported = client.post(f"{API}/admin/credential-deliveries/{deliveries[0]['id']}/export", headers=auth_header(admin_tok), json={'current_password': RIGHT})
    assert exported.status_code == 200
    book = load_workbook(BytesIO(exported.content), read_only=True)
    temporary_password = list(book.active.iter_rows(min_row=2, values_only=True))[0][3]
    book.close()
    token, _ = login(client, "rst_stu", password=temporary_password)
    assert client.get(f"{API}/auth/me", headers=auth_header(token)).json()["must_change_password"] is True


def test_self_change_rejects_password_equal_username(client, db_session_factory):
    token = _create_student(client, db_session_factory, "pw_eq_us")
    me = client.get(f"{API}/auth/me", headers=auth_header(token)).json()
    resp = client.patch(
        f"{API}/users/{me['id']}/password",
        headers=auth_header(token),
        json={"password": "pw_eq_us", "current_password": RIGHT},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "PASSWORD_EQUALS_USERNAME"


def test_self_change_rejects_invalid_shape(client, db_session_factory):
    token = _create_student(client, db_session_factory, "shape_pw")
    me = client.get(f"{API}/auth/me", headers=auth_header(token)).json()
    resp = client.patch(
        f"{API}/users/{me['id']}/password",
        headers=auth_header(token),
        json={"password": "short", "current_password": RIGHT},
    )
    assert resp.status_code == 422, resp.text
