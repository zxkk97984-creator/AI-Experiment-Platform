"""TASK-012：session_version 全会话撤销。

- 改密（本人/管理员重置）后旧 Access/Refresh 下一次请求立即 401；
- 禁用用户后旧 Token 立即 401；
- 旧 Token 不含 sv（上线前签发）统一要求重新登录；
- 并发刷新消费同一 Refresh 仍被原子 GETDEL 拦截。
"""

from conftest import auth_header, create_user, login

API = "/api/v1"

RIGHT = "Passw0rd!"


def _me(token):
    return {"Authorization": f"Bearer {token}"}


def test_access_token_revoked_after_self_password_change(client, db_session_factory):
    create_user(db_session_factory, "sv_stu", "student")
    token, refresh = login(client, "sv_stu")
    assert client.get(f"{API}/auth/me", headers=_me(token)).status_code == 200

    me = client.get(f"{API}/auth/me", headers=_me(token)).json()
    resp = client.patch(
        f"{API}/users/{me['id']}/password",
        headers=_me(token),
        json={"password": "NewPass123!", "current_password": RIGHT},
    )
    assert resp.status_code == 200, resp.text

    # 旧 Access 立即 401
    revoked = client.get(f"{API}/auth/me", headers=_me(token))
    assert revoked.status_code == 401, revoked.text
    assert revoked.json()["detail"]["code"] == "SESSION_REVOKED"
    # 旧 Refresh 立即 401
    refresh_resp = client.post(
        f"{API}/auth/refresh",
        json={"refresh_token": refresh},
    )
    assert refresh_resp.status_code == 401, refresh_resp.text


def test_refresh_token_revoked_after_admin_reset(client, db_session_factory):
    create_user(db_session_factory, "sv_rst", "student")
    create_user(db_session_factory, "sv_admin", "admin")
    student_token, refresh = login(client, "sv_rst")
    admin_token, _ = login(client, "sv_admin")

    uid = client.get(
        f"{API}/users", headers=_me(admin_token), params={"role": "student"}
    ).json()["items"][0]["id"]
    resp = client.post(
        f"{API}/users/{uid}/password-reset",
        headers=_me(admin_token),
        json={"current_password": RIGHT, "reason": "Requested reset"},
    )
    assert resp.status_code == 200, resp.text

    refresh_resp = client.post(
        f"{API}/auth/refresh",
        json={"refresh_token": refresh},
        cookies={"dai_refresh_token": refresh},
    )
    assert refresh_resp.status_code == 401, refresh_resp.text
    # 旧 Access 同样失效
    assert client.get(f"{API}/auth/me", headers=_me(student_token)).status_code == 401


def test_tokens_revoked_after_disable(client, db_session_factory):
    create_user(db_session_factory, "sv_dis", "student")
    create_user(db_session_factory, "sv_adm2", "admin")
    student_token, _ = login(client, "sv_dis")
    admin_token, _ = login(client, "sv_adm2")

    uid = client.get(
        f"{API}/users", headers=_me(admin_token), params={"role": "student"}
    ).json()["items"][0]["id"]
    resp = client.patch(
        f"{API}/users/{uid}/status",
        headers=_me(admin_token),
        json={"status": "disabled"},
    )
    assert resp.status_code == 200, resp.text
    revoked = client.get(f"{API}/auth/me", headers=_me(student_token))
    assert revoked.status_code == 401
    assert revoked.json()["detail"]["code"] in ("USER_NOT_ACTIVE", "SESSION_REVOKED")


def test_token_without_sv_is_rejected(client, db_session_factory):
    """上线前签发的无 sv Token 统一要求重新登录"""
    from app.security import create_token
    from app.config import Settings

    create_user(db_session_factory, "sv_legacy", "student")
    user_id = 1  # 该测试库首个用户 id 为 1
    settings = Settings(
        database_url="sqlite://", secret_key="test-secret-key", algorithm="HS256",
    )
    # 模拟旧版签名（无 sv 字段）：手工构造 payload 再签名
    import datetime as dt
    import jwt

    payload = {
        "sub": str(user_id),
        "role": "student",
        "type": "access",
        "jti": "legacy-jti",
        "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
    }
    legacy_token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    resp = client.get(f"{API}/auth/me", headers=_me(legacy_token))
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "SESSION_REVOKED"


def test_new_login_works_after_revocation(client, db_session_factory):
    """撤销后重新登录签发新 sv 的 Token 正常工作"""
    create_user(db_session_factory, "sv_relog", "student")
    old_token, _ = login(client, "sv_relog")
    me = client.get(f"{API}/auth/me", headers=_me(old_token)).json()
    client.patch(
        f"{API}/users/{me['id']}/password",
        headers=_me(old_token),
        json={"password": "RelogPass1!", "current_password": RIGHT},
    )
    assert client.get(f"{API}/auth/me", headers=_me(old_token)).status_code == 401
    new_token, _ = login(client, "sv_relog", password="RelogPass1!")
    assert client.get(f"{API}/auth/me", headers=_me(new_token)).status_code == 200
