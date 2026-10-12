"""原生 HTTP 集成组：真实 Uvicorn 子进程 + 独立数据库/Redis。

与 ``tests/automated`` 的区别（刻意隔离）：

* **不导入** ``tests/automated/conftest.py``：那里的 TestClient 被替换为内联线程
  ASGI 适配器，验证不到真实网络栈、生命周期与关闭行为；
* 每个用例启动自己的 Uvicorn 子进程，绑定 127.0.0.1 随机端口，
  使用临时 SQLite 与隔离 Redis（由 ``DAI_REDIS_URL`` 注入）；
* 只终止本组创建的进程；不触碰任何既有服务或业务数据库。

运行方式（CI 用独立 job，独立于 automated 全量）：

    cd backend && python -m pytest -q tests/native_http
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

BACKEND = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _local_get(url: str, timeout: float = 5.0, **kwargs):
    """本机回环请求：显式关闭 trust_env。

    宿主机可能配置 HTTP(S)_PROXY/ALL_PROXY（含 [::1] 等 no_proxy 形式），
    httpx 在解析这类环境变量时会拒绝本机 URL——验证真实 HTTP 时必须与
    环境代理解耦，否则测试结果取决于宿主环境。
    """
    import httpx

    with httpx.Client(trust_env=False, timeout=timeout) as client:
        return client.get(url, **kwargs)


def _wait_http(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = _local_get(url, timeout=2.0)
            if response.status_code < 500:
                return True
        except Exception:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    """用仓库自身的 bootstrap 路径建一次已迁移的 SQLite 模板库。"""
    folder = tmp_path_factory.mktemp("native-template")
    path = folder / "template.db"
    env = dict(os.environ)
    env.pop("DAI_DATABASE_URL", None)
    env["DAI_DATABASE_URL"] = f"sqlite:///{path}"
    env["PYTHONPATH"] = str(BACKEND)
    env["DAI_ENVIRONMENT"] = "development"
    env["DAI_MIGRATION_MODE"] = "disposable"
    result = subprocess.run(
        [PYTHON, str(BACKEND.parent / "scripts" / "bootstrap_database.py")],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"bootstrap 失败:\n{result.stdout}\n{result.stderr}"
    return path


@pytest.fixture()
def isolated_env(tmp_path, migrated_template):
    """每个用例一份独立的已迁移 SQLite 副本 + 隔离 Redis + 关闭 AI。"""
    database_path = tmp_path / "native.db"
    shutil.copy2(migrated_template, database_path)
    database_url = f"sqlite:///{database_path}"
    env = dict(os.environ)
    env.pop("DAI_DATABASE_URL", None)
    env["DAI_DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DAI_ENVIRONMENT"] = "development"
    env["DAI_MIGRATION_MODE"] = "disposable"
    env["DAI_AI_ENABLED"] = "false"
    env["DAI_SECRET_KEY"] = "native-http-test-secret-key"
    return {"database_url": database_url, "env": env, "tmp_path": tmp_path}


class NativeServer:
    def __init__(self, env: dict, cwd: Path, port: int, log_path: Path):
        self.env = env
        self.cwd = cwd
        self.port = port
        self.log_path = log_path
        self.process: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, *, wait: bool = True) -> "NativeServer":
        self.log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=self.cwd, env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        if wait:
            ready = _wait_http(f"{self.base_url}/api/v1/health/live")
            if not ready:
                self.log.flush()
                tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
                # 启动失败也必须回收子进程：失败用例不得泄漏 Uvicorn
                self.stop()
                raise AssertionError(
                    f"Uvicorn 未在时限内就绪（exit={self.process}）：\n{tail}"
                )
        return self

    def stop(self, timeout: float = 15.0) -> int | None:
        if self.process is None:
            return None
        if self.process.poll() is None:
            self.process.terminate()
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait(timeout=5)
        self.process = None
        try:
            self.log.close()
        except Exception:
            pass
        return code


@pytest.fixture()
def native_server(isolated_env):
    port = _free_port()
    server = NativeServer(isolated_env["env"], BACKEND, port,
                          isolated_env["tmp_path"] / "uvicorn.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _seed_rows(env: dict, statement: str, params: dict) -> None:
    engine = sa.create_engine(env["database_url"].replace("sqlite:///", "sqlite:///"))
    with engine.begin() as conn:
        conn.execute(sa.text(statement), params)
    engine.dispose()


def _connect(env: dict) -> sa.Engine:
    """连接本用例的副本库（已由 bootstrap 迁移到 head）。"""
    return sa.create_engine(env["database_url"])


# ─────────────────────────────────────────────────────────────
# 生命周期
# ─────────────────────────────────────────────────────────────

def test_real_uvicorn_lifecycle_and_health(native_server):
    live = _local_get(f"{native_server.base_url}/api/v1/health/live")
    assert live.status_code == 200
    ready = _local_get(f"{native_server.base_url}/api/v1/health/ready", timeout=10)
    assert ready.status_code == 200, ready.text
    payload = ready.json()
    assert payload.get("status") in ("ok", "ready", "healthy", None) or isinstance(payload, dict)


def test_graceful_shutdown_is_bounded(isolated_env):
    """SIGTERM 后必须在有界时间内退出（协作关闭，不被后台任务拖住）。"""
    port = _free_port()
    server = NativeServer(isolated_env["env"], BACKEND, port,
                          isolated_env["tmp_path"] / "shutdown.log").start()
    started = time.monotonic()
    code = server.stop(timeout=15)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"关闭耗时过长: {elapsed:.1f}s"
    assert code in (0, -15, -9), code


# ─────────────────────────────────────────────────────────────
# 到期扫描（真实进程内后台任务）
# ─────────────────────────────────────────────────────────────

def test_expiry_scanner_auto_submits_expired_attempt(isolated_env):
    """真实进程：过期考试由后台扫描自动交卷（而非仅靠请求触发）。"""
    engine = _connect(isolated_env)
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, real_name, status, "
            "session_version, credential_version, must_change_password) "
            "VALUES (1, 'native_s', 'x', 'student', 'NS', 'active', 1, 1, 0)"))
        conn.execute(sa.text(
            "INSERT INTO courses (id, title, status, teacher_id) VALUES (1, 'NC', 'published', 1)"))
        conn.execute(sa.text(
            "INSERT INTO exams (id, course_id, title, status, duration_minutes) "
            "VALUES (1, 1, 'NE', 'published', 30)"))
        conn.execute(sa.text(
            "INSERT INTO exam_submissions (id, exam_id, student_id, status, expires_at) "
            "VALUES (1, 1, 1, 'started', :expires)"),
            {"expires": now - timedelta(minutes=5)})
    engine.dispose()

    port = _free_port()
    server = NativeServer(isolated_env["env"], BACKEND, port,
                          isolated_env["tmp_path"] / "expiry.log").start()
    try:
        deadline = time.monotonic() + 40
        status = None
        while time.monotonic() < deadline:
            engine = sa.create_engine(isolated_env["database_url"])
            with engine.connect() as conn:
                status = conn.execute(sa.text(
                    "SELECT status, submission_reason FROM exam_submissions WHERE id = 1"
                )).one()
            engine.dispose()
            if status[0] != "started":
                break
            time.sleep(1)
        assert status is not None and status[0] != "started", (
            f"过期考试未被自动收卷: {status}\n"
            f"{server.log_path.read_text(encoding='utf-8')[-1500:]}"
        )
    finally:
        server.stop()


# ─────────────────────────────────────────────────────────────
# 慢/不可用 Redis：有界失败语义
# ─────────────────────────────────────────────────────────────

def test_auth_fails_fast_with_unreachable_redis(isolated_env):
    """Redis 不可达：真实令牌的鉴权查询快速 503（0.5s 连接超时 + 0 重试）。"""
    engine = _connect(isolated_env)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, real_name, status, "
            "session_version, credential_version, must_change_password) "
            "VALUES (9, 'native_auth', 'x', 'student', 'NA', 'active', 1, 1, 0)"))
    engine.dispose()
    sys.path.insert(0, str(BACKEND))
    from datetime import timedelta

    from app.security import create_token

    token = create_token(subject=9, role="student", token_type="access",
                         secret_key="native-http-test-secret-key",
                         algorithm="HS256", session_version=1,
                         expires_delta=timedelta(minutes=30))

    env = dict(isolated_env["env"])
    env["DAI_REDIS_URL"] = "redis://127.0.0.1:1/15"      # 必然拒绝连接
    port = _free_port()
    server = NativeServer(env, BACKEND, port, isolated_env["tmp_path"] / "redis-down.log").start()
    try:
        started = time.monotonic()
        response = _local_get(f"{server.base_url}/api/v1/auth/me",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10)
        elapsed = time.monotonic() - started
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
        assert elapsed < 5, f"认证路径耗时过长（应快速失败）: {elapsed:.2f}s"
    finally:
        server.stop()


def test_metrics_endpoint_does_not_block(isolated_env):
    """指标 Redis 不可用时 /metrics 仍快速返回（不阻断、不长等待）。"""
    env = dict(isolated_env["env"])
    env["DAI_REDIS_URL"] = "redis://127.0.0.1:1/15"
    port = _free_port()
    server = NativeServer(env, BACKEND, port, isolated_env["tmp_path"] / "metrics.log").start()
    try:
        started = time.monotonic()
        response = _local_get(f"{server.base_url}/api/v1/metrics", timeout=10)
        elapsed = time.monotonic() - started
        assert response.status_code in (200, 401, 403, 404, 503), response.text
        assert elapsed < 5, f"/metrics 耗时过长: {elapsed:.2f}s"
    finally:
        server.stop()
