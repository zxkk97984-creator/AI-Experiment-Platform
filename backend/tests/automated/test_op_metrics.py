"""TASK-029（F-36）：最小运维指标。

- 指标增量、白名单标签基数防注入、Redis 故障 no-op
- 生产 JSON 日志格式的敏感键脱敏
- /metrics 管理员权限与字段（无提交内容泄露）
- 中间件状态类别计数 + 路径模板低基数

A/B/C 分类：B 类（最小父行）——CodeGrade 的 submission/rubric 外键经共享工厂
make_submission / make_rubric 建真实父行。
"""
import json
import logging

import pytest
from conftest import auth_header, create_user, login

from app.services import op_metrics

API = "/api/v1"


# ── 计数器单元测试 ──


def test_record_and_read_increments(redis_client):
    op_metrics.record(redis_client, "http_requests_total", label="5xx")
    op_metrics.record(redis_client, "http_requests_total", value=4, label="5xx")
    assert op_metrics.read(redis_client, "http_requests_total", label="5xx") == 5


def test_unknown_metric_name_rejected(redis_client):
    """标签基数防线：未登记指标名不落库、不抛出。"""
    op_metrics.record(redis_client, "student_id_12345", label="whatever")
    assert op_metrics.read(redis_client, "student_id_12345", label="whatever") == 0
    assert redis_client.keys("opmetrics:*") == []


def test_unknown_label_rejected(redis_client):
    """标签值白名单：用户 id/任意串不能成为标签。"""
    op_metrics.record(redis_client, "http_requests_total", label="user_42")
    assert op_metrics.read(redis_client, "http_requests_total", label="user_42") == 0
    assert redis_client.keys("opmetrics:*") == []


def test_record_redis_failure_is_noop(redis_client, monkeypatch):
    """指标路径绝不阻断业务：Redis 抛错时静默降级。"""
    monkeypatch.setattr(redis_client, "incrby", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    op_metrics.record(redis_client, "judge_failures_total", label="permanent")  # 不抛


def test_ai_metrics_sink_accumulates(redis_client, monkeypatch):
    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: redis_client)
    sink = op_metrics.ai_metrics_sink()
    sink({"operation": "ai_grading", "prompt_tokens": 120, "completion_tokens": 30})
    sink({"operation": "ai_grading", "prompt_tokens": 80, "completion_tokens": None})
    sink({"operation": "unknown_op", "prompt_tokens": 999})  # 未登记操作忽略
    assert op_metrics.read(redis_client, "ai_requests_total", label="ai_grading") == 2
    assert op_metrics.read(redis_client, "ai_prompt_tokens_total", label="ai_grading") == 200
    assert op_metrics.read(redis_client, "ai_completion_tokens_total", label="ai_grading") == 30
    assert op_metrics.read(redis_client, "ai_requests_total", label="unknown_op") == 0


# ── 生产 JSON 日志脱敏 ──


def test_json_formatter_drops_sensitive_extra_keys():
    from app.logging_config import JsonFormatter

    record = logging.LogRecord(
        name="dai.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    record.request_id = "rid123"
    record.operation = "ai_grading"
    record.api_key = "sk-secret-should-not-leak"
    record.authorization = "Bearer token"
    record.prompt_tokens = 12

    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello"
    assert payload["rid"] == "rid123"
    assert payload["operation"] == "ai_grading"
    assert payload["prompt_tokens"] == 12
    assert "api_key" not in payload
    assert "authorization" not in payload


# ── /metrics 权限与字段 ──


def test_metrics_requires_admin(client, db_session_factory):
    create_user(db_session_factory, "ops-student", "student")
    token, _ = login(client, "ops-student")
    resp = client.get(f"{API}/metrics", headers=auth_header(token))
    assert resp.status_code == 403


def test_metrics_admin_payload(client, db_session_factory, redis_client):
    create_user(db_session_factory, "ops-admin", "admin")
    token, _ = login(client, "ops-admin")
    resp = client.get(f"{API}/metrics", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    metrics = resp.json()["metrics"]
    # 无提交内容字段；包含运维核心字段
    assert "op_metrics" in metrics
    assert "judge_queue_depth" in metrics
    assert "db_ok" in metrics
    assert "redis_ok" in metrics
    serialized = json.dumps(metrics)
    assert "hidden_tests" not in serialized
    assert "code" not in serialized


def test_metrics_reports_assignment_exam_and_ai_queue_depths_separately(
    client, db_session_factory, redis_client, test_settings
):
    from app.worker.judge_worker import EXAM_JUDGE_QUEUE

    create_user(db_session_factory, "ops-queue-admin", "admin")
    token, _ = login(client, "ops-queue-admin")
    redis_client.rpush(test_settings.judge_queue_name, "assignment")
    redis_client.rpush(EXAM_JUDGE_QUEUE, "exam-1", "exam-2")
    redis_client.rpush(test_settings.ai_queue_name, "ai-1", "ai-2", "ai-3")

    response = client.get(f"{API}/metrics", headers=auth_header(token))

    assert response.status_code == 200, response.text
    metrics = response.json()["metrics"]
    assert metrics["judge_queue_depth"] == 1
    assert metrics["exam_queue_depth"] == 2
    assert metrics["ai_queue_depth"] == 3


# ── 中间件：状态类别计数 + 低基数路径 ──


def test_middleware_counts_5xx_and_logs_template(
    client, app, db_session_factory, redis_client, monkeypatch, caplog,
):
    """5xx 请求 → 计数 + 结构化日志；路径模板不含资源 id。

    本分支 readiness 语义（TASK-003）：DB 故障时 /health/ready 返回 503 degraded
    （不回显异常）而非 500——同为 5xx，中间件计数与日志断言不变。
    health_ready 不走 get_db 依赖（内部自建 SessionLocal），故 monkeypatch 模块级
    SessionLocal 制造 DB 故障。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: redis_client)

    import app.database as database_module

    def broken_session_local():
        raise RuntimeError("db down")

    monkeypatch.setattr(database_module, "SessionLocal", broken_session_local)
    # raise_server_exceptions=False：让 ServerErrorMiddleware 返回 500 而不是上抛
    local_client = TestClient(app, raise_server_exceptions=False)
    try:
        with caplog.at_level(logging.WARNING, logger="dai.main"):
            resp = local_client.get(f"{API}/health/ready")
    finally:
        local_client.close()
        monkeypatch.undo()

    assert resp.status_code == 503
    assert op_metrics.read(redis_client, "http_requests_total", label="5xx") >= 1
    http_logs = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert http_logs
    rec = http_logs[-1]
    assert rec.status_code == 503
    assert rec.path == f"{API}/health/ready"
    assert rec.latency_ms >= 0


def test_middleware_path_template_is_low_cardinality(
    client, db_session_factory, redis_client, monkeypatch, caplog,
):
    """带 id 的路由记录的是模板（{course_id}），不是具体数字——防高基数。"""
    monkeypatch.setattr(op_metrics, "get_metrics_redis", lambda: redis_client)
    create_user(db_session_factory, "ops-teacher", "teacher")
    token, _ = login(client, "ops-teacher")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="dai.main"):
        resp = client.get(f"{API}/courses/987654", headers=auth_header(token))
    assert resp.status_code == 404
    rec = next(r for r in caplog.records if r.getMessage() == "http_request")
    assert rec.path == f"{API}/courses/{{course_id}}"
    assert "987654" not in rec.path


# ── 判题失败计数 ──


def test_fail_ai_grade_records_permanent_counter(db_session_factory, redis_client):
    from app.models import CodeGrade
    from app.services.ai_grading_queue import claim_ai_grade, fail_ai_grade
    from app.config import Settings
    from conftest import make_rubric, make_submission

    submission_id = make_submission(
        db_session_factory, status="queued", grading_status="queued",
    )
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(submission_id=submission_id, rubric_id=rubric_id,
                          mode="active", status="queued")
        db.add(grade)
        db.commit()
        grade_id = grade.id

    settings = Settings(_env_file=None, ai_enabled=False, ai_api_key="")
    # 必须显式关闭 session：泄漏的连接会持有 MySQL 元数据锁，
    # 导致 teardown 的 DROP TABLE 永久等待（2026-08 MySQL 回归卡死根因）。
    with db_session_factory() as db:
        # 失败写入受认领栅栏保护：必须先认领（真实 Worker 序列）
        assert claim_ai_grade(db, grade_id)
        fail_ai_grade(db, redis_client, grade_id,
                      "AI 服务未启用", retryable=False, max_attempts=3)
    assert op_metrics.read(redis_client, "judge_failures_total", label="permanent") == 1
