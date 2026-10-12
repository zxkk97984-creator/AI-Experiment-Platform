"""A13 回归：容量验证脚本的 passed 判据必须覆盖错误数、人数覆盖与成功样本。

评审场景：纯内存 Fake 探针 15 次保存全部 500，旧判据只比较延迟分位数 → 仍报告
``passed=true``。这里直接锁定 ``build_targets`` 的语义，避免门禁再次失效。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify_exam_capacity.py"


def _load_module():
    import sys

    spec = importlib.util.spec_from_file_location("verify_exam_capacity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器需要模块已在 sys.modules 中
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope="module")
def capacity():
    return _load_module()


def _healthy(**overrides):
    params = dict(read_p95=10.0, save_p95=100.0, save_p99=200.0, missing=0, mismatched=0,
                  unverified=0, regressions=0, error_count=0, expected_students=30,
                  saved_students=30, verified_students=30, save_samples=300)
    params.update(overrides)
    return params


def test_all_500_saves_must_fail_the_gate(capacity):
    """全员 500（15 次保存全失败）：即使延迟"很好看"也必须 passed=false。"""
    targets, coverage, passed = capacity.build_targets(**_healthy(
        save_p95=1.0, save_p99=2.0, read_p95=1.0,
        error_count=15, saved_students=0, verified_students=0,
        unverified=30, save_samples=15))
    assert passed is False
    assert targets["zero_errors"] is False
    assert targets["every_student_saved"] is False
    assert targets["final_answers_verified"] is False
    assert targets["full_coverage"] is False
    assert coverage["saved_students"] == 0 and coverage["expected_students"] == 30


def test_latency_only_success_is_not_enough(capacity):
    """延迟达标但隐藏了未验证学生：不得通过。"""
    targets, _, passed = capacity.build_targets(**_healthy(unverified=3, verified_students=27))
    assert passed is False and targets["final_answers_verified"] is False


def test_zero_students_never_passes(capacity):
    targets, _, passed = capacity.build_targets(**_healthy(
        expected_students=0, saved_students=0, verified_students=0, save_samples=0))
    assert passed is False
    assert targets["every_student_saved"] is False


def test_healthy_run_passes_and_reports_coverage(capacity):
    targets, coverage, passed = capacity.build_targets(**_healthy())
    assert passed is True and all(targets.values())
    assert coverage == {"expected_students": 30, "saved_students": 30,
                        "verified_students": 30, "unverified_students": 0,
                        "save_samples": 300}


def test_cli_exit_code_mapping(capacity):
    """退出码口径：passed=false → 1（失败必须非零，否则 CI 会放过不合格容量）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'return 0 if report["passed"] else 1' in source


# ─────────────────────────────────────────────────────────────
# A14：验证启动器必须清理测试覆盖变量并注入隔离配置
# ─────────────────────────────────────────────────────────────

def test_child_environment_strips_test_overrides(monkeypatch, tmp_path, capacity_env_module=None):
    import importlib.util
    import sys

    script = REPO_ROOT / "scripts" / "verify_env.py"
    spec = importlib.util.spec_from_file_location("verify_env_under_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("DAI_TEST_MYSQL_URL", "mysql+pymysql://root:real@10.0.0.9/prod")
    monkeypatch.setenv("DAI_TEST_NGINX_IMAGE", "nginx:alpine")
    monkeypatch.setenv("DAI_DATABASE_URL", "mysql+pymysql://root:real@10.0.0.9/prod")
    monkeypatch.setenv("DAI_REDIS_URL", "redis://10.0.0.9:6379/0")

    env = module.child_environment({
        "run_id": "unit", "secret_key": "s", "redis_url": "redis://127.0.0.1:6399/15",
        "empty_dotenv": str(empty),
    })
    assert "DAI_TEST_MYSQL_URL" not in env, "DAI_TEST_* 覆盖变量必须清理"
    assert "DAI_TEST_NGINX_IMAGE" not in env
    assert env["DAI_DATABASE_URL"] if "DAI_DATABASE_URL" in env else True
    assert env.get("DAI_DATABASE_URL") is None, "业务库配置不得继承"
    assert env["DAI_REDIS_URL"] == "redis://127.0.0.1:6399/15", "必须注入本轮隔离 Redis"
    assert env["DAI_ENV_FILE"] == str(empty), "dotenv 必须指向空文件"
    assert env["DAI_ENVIRONMENT"] == "development"
    assert env["DAI_MIGRATION_MODE"] == "disposable"


def test_launcher_defaults_to_isolated_temp_cwd():
    source = (REPO_ROOT / "scripts" / "verify_env.py").read_text(encoding="utf-8")
    assert 'run.add_argument("--cwd", default="temp"' in source
    assert "tempfile.mkdtemp(prefix=f\"dai-verify-{args.run_id}-\")" in source


def test_settings_honors_explicit_and_environment_dotenv_paths(monkeypatch, tmp_path):
    from app.config import Settings

    selected = tmp_path / "selected.env"
    selected.write_text("DAI_DATABASE_URL=sqlite:///selected.db\n", encoding="utf-8")
    other = tmp_path / "explicit.env"
    other.write_text("DAI_DATABASE_URL=sqlite:///explicit.db\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DAI_DATABASE_URL", raising=False)
    monkeypatch.setenv("DAI_ENV_FILE", str(selected))

    assert Settings().database_url == "sqlite:///selected.db"
    assert Settings(_env_file=other).database_url == "sqlite:///explicit.db"
    assert Settings(_env_file=None).database_url != "sqlite:///selected.db"

    monkeypatch.setenv("DAI_ENV_FILE", "")
    assert Settings().database_url != "sqlite:///selected.db"

    monkeypatch.delenv("DAI_ENV_FILE")
    (tmp_path / ".env").write_text("DAI_DATABASE_URL=sqlite:///default.db\n", encoding="utf-8")
    assert Settings().database_url == "sqlite:///default.db"


def test_child_environment_does_not_leak_dotenv_or_ai_configuration(monkeypatch, tmp_path):
    import importlib.util
    import sys

    script = REPO_ROOT / "scripts" / "verify_env.py"
    spec = importlib.util.spec_from_file_location("verify_env_settings_under_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("DAI_ENV_FILE", "/home/zxk/Projects/dai-experiment-platform/backend/.env")
    monkeypatch.setenv("DAI_AI_API_KEY", "must-not-leak")
    env = module.child_environment({"run_id": "settings", "secret_key": "isolated",
                                    "redis_url": "redis://127.0.0.1:6388/15",
                                    "empty_dotenv": str(empty)})
    assert env["DAI_ENV_FILE"] == str(empty)
    assert env["DAI_AI_API_KEY"] == ""
    assert env["DAI_AI_ENABLED"] == "false"

    for key in ("DAI_ENV_FILE", "DAI_AI_API_KEY", "DAI_AI_ENABLED", "DAI_REDIS_URL"):
        if key in env:
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)
    from app.config import Settings
    settings = Settings()
    assert settings.redis_url == "redis://127.0.0.1:6388/15"
    assert settings.ai_ready is False


def test_run_load_rejects_http_200_with_missing_save_result(capacity):
    import httpx

    saves = {"count": 0}

    def handler(request):
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={
                "submission": {"id": 7, "status": "started"}, "saved_answers": [],
            })
        if request.method == "PUT" and request.url.path.endswith("/answers"):
            saves["count"] += 1
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"unexpected mock request: {request.method} {request.url}")

    manifest = {"base_url": "http://capacity.test", "exam_id": 4, "question_ids": [9],
                "students": [{"username": "student1", "password": "fixture"}]}
    report = asyncio.run(capacity.run_load(
        manifest, ramp_seconds=0, steady_seconds=0.08, interval=0.001,
        transport=httpx.MockTransport(handler)))
    assert saves["count"] > 0
    assert report["passed"] is False
    assert report["error_count"] > 0
    assert report["coverage"]["saved_students"] == 0
    assert report["successful_save_samples"] == 0


def test_run_load_counts_only_confirmed_save_and_final_read(capacity):
    import httpx

    state = {"version": 0, "selection": []}

    def handler(request):
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.path.endswith("/session"):
            saved = ([{"question_id": 9, "version": state["version"],
                       "selected_options": state["selection"]}] if state["version"] else [])
            return httpx.Response(200, json={
                "submission": {"id": 7, "status": "started"}, "saved_answers": saved,
            })
        if request.method == "PUT" and request.url.path.endswith("/answers"):
            body = json.loads(request.content)
            answer = body["answers"][0]
            state["version"] += 1
            state["selection"] = answer["selected_options"]
            return httpx.Response(200, json={"results": [{
                "question_id": 9, "ok": True, "version": state["version"],
            }]})
        raise AssertionError(f"unexpected mock request: {request.method} {request.url}")

    manifest = {"base_url": "http://capacity.test", "exam_id": 4, "question_ids": [9],
                "students": [{"username": "student1", "password": "fixture"}]}
    report = asyncio.run(capacity.run_load(
        manifest, ramp_seconds=0, steady_seconds=0.08, interval=0.001,
        transport=httpx.MockTransport(handler)))
    assert report["passed"] is True, report
    assert report["coverage"]["saved_students"] == 1
    assert report["coverage"]["verified_students"] == 1
    assert report["successful_save_samples"] > 0


def test_capacity_students_refresh_with_independent_cookie_jars(capacity):
    import httpx
    import time

    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie", ""))
        return httpx.Response(200, json={"access_token": f"new-{len(seen)}", "expires_in": 1800})

    async def run():
        transport = httpx.MockTransport(handler)
        async with capacity._client("http://capacity.test", transport=transport) as first_client, \
                capacity._client("http://capacity.test", transport=transport) as second_client:
            first_client.cookies.set("dai_refresh_token", "student-one")
            second_client.cookies.set("dai_refresh_token", "student-two")
            first = capacity.Student("student1", "x", access_expires_at=time.monotonic() - 1)
            second = capacity.Student("student2", "x", access_expires_at=time.monotonic() - 1)
            assert await capacity._refresh_if_needed(first_client, first)
            assert await capacity._refresh_if_needed(second_client, second)
            assert first.token != second.token

    asyncio.run(run())
    assert len(seen) == 2
    assert "student-one" in seen[0]
    assert "student-two" in seen[1]
