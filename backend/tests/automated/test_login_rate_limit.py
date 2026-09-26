"""登录限流测试：账户/IP 双维度、时间窗口、成功复位、未知用户、Redis 故障与转发头信任。

- 账户维度：规范化用户名 15 分钟窗口内最多 10 次失败。
- IP 维度：单 IP 15 分钟窗口内最多 30 次尝试（换用户名不可绕过）。
- 超限返回 429 + Retry-After；成功登录清除该账户失败计数。
- Redis 故障返回 503，不造成永久锁定。
- 只有 immediate peer 命中可信代理 CIDR/主机配置时才解析 X-Forwarded-For。
"""

import time

import pytest
import redis as redis_lib
from starlette.requests import Request

from conftest import create_user
from app.api.auth import _client_ip

WRONG = "wrong-password-1"
RIGHT = "Passw0rd!"


def login_attempt(client, username, password=RIGHT, headers=None):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers=headers or {},
    )


def test_user_dimension_blocks_after_10_failures(client, db_session_factory):
    create_user(db_session_factory, "alice", "student")
    for _ in range(10):
        assert login_attempt(client, "alice", WRONG).status_code == 401
    # 第 11 次即使密码正确也被限流
    blocked = login_attempt(client, "alice", RIGHT)
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 0


def test_ip_dimension_cannot_be_bypassed_by_switching_usernames(client):
    # 30 个不同用户名各失败一次 → IP 维度封禁
    for i in range(30):
        assert login_attempt(client, f"ghost{i}", WRONG).status_code == 401
    blocked = login_attempt(client, "ghost30", WRONG)
    assert blocked.status_code == 429


def test_success_clears_user_failure_count(client, db_session_factory):
    create_user(db_session_factory, "bob", "student")
    for _ in range(5):
        assert login_attempt(client, "bob", WRONG).status_code == 401
    # 成功登录清零账户失败计数
    assert login_attempt(client, "bob", RIGHT).status_code == 200
    # 重新计数：前 10 次失败均返回 401，第 11 次才被限流
    for _ in range(10):
        assert login_attempt(client, "bob", WRONG).status_code == 401
    assert login_attempt(client, "bob", WRONG).status_code == 429


def test_window_expiry_unblocks_account(client, redis_client, db_session_factory):
    create_user(db_session_factory, "carol", "student")
    for _ in range(10):
        assert login_attempt(client, "carol", WRONG).status_code == 401
    assert login_attempt(client, "carol", RIGHT).status_code == 429
    # 模拟窗口过期（将窗口缩短到 1 秒）
    for key in redis_client.scan_iter("rl:user:*"):
        redis_client.expire(key, 1)
    time.sleep(1.2)
    assert login_attempt(client, "carol", RIGHT).status_code == 200


def test_unknown_user_failures_count_toward_limits(client):
    for _ in range(10):
        assert login_attempt(client, "nobody", WRONG).status_code == 401
    assert login_attempt(client, "nobody", WRONG).status_code == 429


def test_redis_failure_returns_503_without_permanent_lockout(
    client, redis_client, monkeypatch, db_session_factory
):
    create_user(db_session_factory, "dave", "student")

    def boom(*args, **kwargs):
        raise redis_lib.ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get", boom)
    monkeypatch.setattr(redis_client, "incr", boom)
    response = login_attempt(client, "dave", RIGHT)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    # Redis 恢复后同一账户可正常登录（未被永久锁定）
    monkeypatch.undo()
    assert login_attempt(client, "dave", RIGHT).status_code == 200


def test_forged_xff_ignored_without_trusted_proxy(client, test_settings):
    assert test_settings.trusted_proxy is False
    for i in range(30):
        assert login_attempt(client, f"ghost{i}", WRONG).status_code == 401
    # 伪造不同来源 IP 不能绕过真实客户端 IP 的封禁
    blocked = login_attempt(
        client, "ghost30", WRONG, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    assert blocked.status_code == 429


def test_xff_honored_only_with_configured_trusted_proxy(client, test_settings, monkeypatch):
    monkeypatch.setattr(test_settings, "trusted_proxy_cidrs", "testclient")
    for i in range(30):
        response = login_attempt(
            client, f"ghost{i}", WRONG, headers={"X-Forwarded-For": "203.0.113.9"}
        )
        assert response.status_code == 401
    # 该转发 IP 已被封禁
    blocked = login_attempt(
        client, "ghost30", WRONG, headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert blocked.status_code == 429
    # 不带转发头的真实客户端 IP 不受该转发 IP 封禁影响
    assert login_attempt(client, "ghost31", WRONG).status_code == 401


def _request_with_xff(value: str | None) -> Request:
    headers = [] if value is None else [(b"x-forwarded-for", value.encode())]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    })


def test_proxy_chain_requires_trusted_peer_and_trusted_intermediate_hops():
    class SettingsStub:
        trusted_proxy_cidrs = "testclient,10.0.0.0/8"

    settings = SettingsStub()
    assert _client_ip(_request_with_xff("203.0.113.9"), settings) == "203.0.113.9"
    assert _client_ip(_request_with_xff("203.0.113.9, 10.1.2.3"), settings) == "203.0.113.9"
    assert _client_ip(_request_with_xff("203.0.113.9, 198.51.100.2"), settings) == "testclient"
    no_trust = type("NoTrust", (), {"trusted_proxy_cidrs": ""})()
    assert _client_ip(_request_with_xff("203.0.113.9"), no_trust) == "testclient"


def test_username_normalization_for_rate_limit_key(client, db_session_factory):
    create_user(db_session_factory, "eve", "student")
    for _ in range(10):
        assert login_attempt(client, "  EVE ", WRONG).status_code == 401
    # 规范化后同一账户被识别并限流
    assert login_attempt(client, "eve", RIGHT).status_code == 429


def test_classroom_sixty_students_share_ip_without_collective_lockout(client, db_session_factory):
    """Successful class logins do not consume the IP failure budget."""
    from app.models import User
    from app.security import hash_password
    with db_session_factory() as db:
        # Fixture-only reuse makes user setup cheap; production creation salts each account.
        password_hash = hash_password(RIGHT)
        db.add_all([User(username=f'classroom-{i}', student_no=f'CLASS-{i}', real_name=f'Student {i}',
                         role='student', status='active', password_hash=password_hash) for i in range(60)])
        db.commit()
    for i in range(60):
        if i % 5 == 0:
            assert login_attempt(client, f'classroom-{i}', WRONG).status_code == 401
        assert login_attempt(client, f'classroom-{i}').status_code == 200
