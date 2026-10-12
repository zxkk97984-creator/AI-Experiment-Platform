"""批次四：API Redis 有界客户端、认证关键路径 503 语义与指标聚合器。

要点：
- 客户端超时/重试/连接池上限是显式契约（故障时快速失败，不堆积）；
- 认证关键读取/写入故障必须 503，绝不"当成令牌不存在"或放行；
- 指标 `record` 不产生每请求 Redis I/O，flush 失败丢弃该批并计数。
"""
from __future__ import annotations

import pytest
import redis as redis_lib
from conftest import auth_header, create_user, login

from app.config import Settings
from app.dependencies import get_redis_client
from app.redis_client import (
    API_CONNECT_TIMEOUT_SECONDS,
    API_MAX_CONNECTIONS,
    API_READ_TIMEOUT_SECONDS,
    METRICS_CONNECT_TIMEOUT_SECONDS,
    METRICS_READ_TIMEOUT_SECONDS,
    build_api_redis,
    build_metrics_redis,
)
from app.services import op_metrics
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

API = "/api/v1"


class _FailingRedis:
    """所有命令都失败的客户端替身：模拟 Redis 不可达/超时。"""

    def __getattr__(self, name):
        def _fail(*args, **kwargs):
            raise redis_lib.exceptions.ConnectionError("redis down")
        return _fail


def _fail_redis(app):
    """在已取得令牌之后切换为故障 Redis（登录本身也需要 Redis）。"""
    app.dependency_overrides[get_redis_client] = lambda: _FailingRedis()
    return app


def test_api_redis_factory_is_bounded():
    settings = Settings(_env_file=None, database_url="sqlite://", redis_url="redis://127.0.0.1:1/15",
                        secret_key="x")
    client = build_api_redis(settings)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == API_CONNECT_TIMEOUT_SECONDS == 0.5
    assert kwargs["socket_timeout"] == API_READ_TIMEOUT_SECONDS == 0.5
    assert client.connection_pool.max_connections == API_MAX_CONNECTIONS == 32

    metrics = build_metrics_redis(settings)
    metrics_kwargs = metrics.connection_pool.connection_kwargs
    assert metrics_kwargs["socket_connect_timeout"] == METRICS_CONNECT_TIMEOUT_SECONDS == 0.1
    assert metrics_kwargs["socket_timeout"] == METRICS_READ_TIMEOUT_SECONDS == 0.1


def test_blacklist_check_failure_returns_503(client, db_session_factory, app):
    """鉴权关键 EXISTS 故障 → 503（不是 401，也绝不放行）。"""
    create_user(db_session_factory, "rl_user", "student")
    token, _ = login(client, "rl_user")
    _fail_redis(app)
    response = client.get(f"{API}/auth/me", headers=auth_header(token))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"


def test_login_rate_limit_failure_returns_503(client, db_session_factory, app):
    _fail_redis(app)
    create_user(db_session_factory, "rl_login", "student")
    response = client.post(f"{API}/auth/login", json={"username": "rl_login", "password": "Passw0rd!"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"


def test_refresh_failure_returns_503(client, db_session_factory, app):
    """GETDEL 结果未知：不重放，统一 503 让用户重新登录。"""
    create_user(db_session_factory, "rl_refresh", "student")
    _, refresh = login(client, "rl_refresh")
    assert refresh
    _fail_redis(app)
    response = client.post(f"{API}/auth/refresh",
                           headers={"Cookie": f"dai_refresh_token={refresh}"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"


def test_logout_write_failure_returns_503(client, db_session_factory, app):
    create_user(db_session_factory, "rl_logout", "student")
    token, _ = login(client, "rl_logout")
    _fail_redis(app)
    response = client.post(f"{API}/auth/logout", headers=auth_header(token))
    assert response.status_code == 503, response.text


def test_metrics_record_does_not_touch_redis(monkeypatch):
    """每请求指标只写内存：Redis 调用计数必须为 0。"""
    calls = {"n": 0}

    class _Pipe:
        def incrby(self, *args, **kwargs):
            calls["n"] += 1
            return self

        def expire(self, *args, **kwargs):
            calls["n"] += 1
            return self

        def execute(self):
            return [1]

    class _CountingRedis:
        def pipeline(self, transaction=False):
            calls["n"] += 1
            return _Pipe()

        def __getattr__(self, name):
            def _call(*args, **kwargs):
                calls["n"] += 1
                return 1
            return _call

    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: _CountingRedis())
    op_metrics.reset_pending()
    recorder = op_metrics.http_metrics_recorder()
    for _ in range(50):
        recorder("2xx", 12.5)
    assert calls["n"] == 0, "record 不得产生 Redis I/O"
    pending = op_metrics.pending_snapshot()
    assert pending[("http_requests_total", "2xx")] == 50
    # 延迟按毫秒取整累加：50 × int(12.5) = 600
    assert pending[("http_latency_ms_sum", "2xx")] == 600

    flushed = op_metrics.flush_pending()
    assert flushed == 2
    assert op_metrics.pending_snapshot() == {}
    assert calls["n"] > 0, "flush 才真正写 Redis"


def test_metrics_flush_failure_drops_batch(monkeypatch, caplog):
    class _BrokenRedis:
        def pipeline(self, transaction=False):
            raise redis_lib.exceptions.ConnectionError("down")

    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: _BrokenRedis())
    op_metrics.reset_pending()
    op_metrics.record(name="judge_failures_total", label="permanent")
    assert op_metrics.flush_pending() == 0
    assert op_metrics.pending_snapshot() == {}, "失败批次被丢弃，不重放、不堆积"


def test_metrics_never_block_request(client, db_session_factory, monkeypatch):
    """指标 Redis 不可用时业务请求仍成功（指标路径绝不阻断）。"""
    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: None)
    created = create_user(db_session_factory, "metrics_user", "student")
    assert created is not None
    token, _ = login(client, "metrics_user")
    response = client.get(f"{API}/auth/me", headers=auth_header(token))
    assert response.status_code == 200, response.text


def test_flush_task_start_and_stop_are_bounded():
    """启动/停止 flush 任务在事件循环中可用且有界（不阻塞退出）。"""
    import asyncio

    async def _run():
        task = op_metrics.start_flush_task()
        assert task is not None
        await op_metrics.stop_flush_task(timeout=0.5)
        return task.cancelled() or task.done()

    assert asyncio.run(_run()) is True


# ─────────────────────────────────────────────────────────────
# A15：Worker 进程的指标必须能到达 Redis（独立于 API lifespan）
# ─────────────────────────────────────────────────────────────

def test_a15_worker_flush_writes_metrics_to_redis(monkeypatch):
    stored = {}

    class _Pipe:
        def incrby(self, key, value):
            stored[key] = stored.get(key, 0) + value
            return self

        def expire(self, key, ttl):
            return self

        def execute(self):
            return [1]

    class _Redis:
        def pipeline(self, transaction=False):
            return _Pipe()

    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: _Redis())
    op_metrics.reset_pending()

    # 模拟 Judge Worker 记录失败/调用指标后冲刷
    op_metrics.record(name="judge_failures_total", label="retryable")
    op_metrics.record(name="judge_failures_total", label="retryable")
    op_metrics.record(name="ai_requests_total", label="ai_grading")
    written = op_metrics.flush_for_worker(force=True)

    assert written == 2, "两条聚合行都应写入"
    assert any(key.startswith("opmetrics:judge_failures_total:retryable:")
               and value == 2 for key, value in stored.items()), stored
    assert any(key.startswith("opmetrics:ai_requests_total:ai_grading:")
               and value == 1 for key, value in stored.items()), stored
    assert op_metrics.pending_snapshot() == {}, "冲刷后内存聚合器应清空"


def test_a15_worker_flush_is_rate_limited(monkeypatch):
    calls = {"n": 0}

    class _Pipe:
        def incrby(self, *a, **k):
            return self

        def expire(self, *a, **k):
            return self

        def execute(self):
            calls["n"] += 1
            return [1]

    class _Redis:
        def pipeline(self, transaction=False):
            return _Pipe()

    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: _Redis())
    op_metrics.reset_pending()
    op_metrics.record(name="judge_failures_total", label="permanent")
    assert op_metrics.flush_for_worker(force=True) == 1
    op_metrics.record(name="judge_failures_total", label="permanent")
    assert op_metrics.flush_for_worker() == 0, "限频窗口内不得再次写 Redis"
    assert op_metrics.pending_snapshot(), "未冲刷的指标保留在内存里，等下一次窗口"


def test_a15_worker_paths_call_flush():
    """Judge/AI Worker 的任务结束路径必须调用 flush（异常路径也覆盖）。"""
    source = (REPO_ROOT / "backend" / "app" / "worker" / "judge_worker.py").read_text(
        encoding="utf-8")
    assert source.count("flush_for_worker()") >= 2, "作业与考试两条路径都要冲刷"
    assert "def flush_for_worker" in (
        REPO_ROOT / "backend" / "app" / "services" / "op_metrics.py").read_text(encoding="utf-8")
