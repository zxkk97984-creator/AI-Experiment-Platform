"""批次 5 真库验收：并发 50 个提交、日预算只够 10 个 → 恰好 10 个受理。

为什么必须真 MySQL：额度扣减依赖 ``practice_settings`` 单行的 ``SELECT ... FOR UPDATE``
串行化；SQLite 的 ``with_for_update`` 是空操作，根本证明不了行锁。所以本文件在
SQLite 下 **skip**（skip 原因见 pytestmark），CI 通过 ``DAI_DATABASE_URL`` 指向真库时执行。

两个变体：
* HTTP 路径（Redis 计数可用）——端到端 202/429 计数；
* 服务路径（Redis 完全不可用）——证明"Redis 故障以库为准、不得放行"，行锁才是权威。
"""
from __future__ import annotations

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.models.practice import PracticeSubmission, PracticeUsageLog

MYSQL_URL = os.environ.get("DAI_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not MYSQL_URL.startswith("mysql"),
    reason="并发额度用例必须在真实 MySQL 上跑：SQLite 无行锁，SELECT ... FOR UPDATE 是空操作，"
           "无法证明『并发 50 个提交、日预算只够 10 个恰好受理 10 个』",
)

STUDENTS = 5
PER_STUDENT = 10
CONCURRENT = STUDENTS * PER_STUDENT
BUDGET_SLOTS = 10
CODE = "def solution(a, b):\n    return a + b\n"
# 同一个真库可能被多批并行测试复用：用户名/请求标识加运行期随机后缀，绝不撞旧数据
RUN = secrets.token_hex(4)


def _prepare(client, db_session_factory, *, token_estimate: int):
    """管理员录题 + 配好 AI + 5 个学生登录；返回 (problem_id, student_headers)。"""
    from conftest import auth_header, login
    from practice_test_support import (
        as_admin, as_student, create_and_publish, problem_payload,
    )
    from tests.automated.test_practice_grading_flow import _make_ai_ready

    _, admin_header = as_admin(client, db_session_factory)
    admin, _ = as_admin(client, db_session_factory, f"mysql-admin-{RUN}")
    created = client.post("/api/v1/admin/practice/problems", json=problem_payload(),
                          headers=admin_header)
    assert created.status_code == 201, created.text
    problem_id = created.json()["id"]
    published = client.post(f"/api/v1/admin/practice/problems/{problem_id}/publish",
                            headers=admin_header)
    assert published.status_code == 200, published.text
    _make_ai_ready(db_session_factory, problem_id, admin.id)

    headers = []
    for index in range(STUDENTS):
        username = f"mysql-student-{RUN}-{index}"
        as_student(client, db_session_factory, username)
        token, _ = login(client, username)
        headers.append(auth_header(token))
    return problem_id, headers, admin_header


def _reset_usage(db_session_factory):
    with db_session_factory() as db:
        db.query(PracticeUsageLog).delete()
        db.commit()


def test_mysql_fifty_concurrent_submissions_exactly_ten_accepted(
        client, app, db_session_factory, test_settings):
    """HTTP 端到端：50 个并发提交抢 10 个 AI 预算名额 → 恰好 10 个 202、40 个 429。"""
    budget = BUDGET_SLOTS * test_settings.practice_ai_token_estimate
    problem_id, headers, admin_header = _prepare(
        client, db_session_factory,
        token_estimate=test_settings.practice_ai_token_estimate)
    configured = client.patch("/api/v1/admin/practice/settings",
                              json={"grading_mode": "ai",
                                    "daily_ai_budget_tokens": budget,
                                    "daily_submission_cap_per_student": 100},
                              headers=admin_header)
    assert configured.status_code == 200, configured.text
    _reset_usage(db_session_factory)

    from conftest import SynchronousASGIClient

    barrier = threading.Barrier(CONCURRENT)

    def _submit(index: int):
        student_header = headers[index % len(headers)]
        own_client = SynchronousASGIClient(app)
        try:
            barrier.wait(timeout=30)
            response = own_client.post(
                f"/api/v1/practice/problems/{problem_id}/submissions",
                json={"code": CODE, "client_request_id": f"race-{index}"},
                headers=student_header)
            return response.status_code, response.text
        finally:
            own_client.close()

    with ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
        results = list(pool.map(_submit, range(CONCURRENT)))

    accepted = [item for item in results if item[0] == 202]
    rejected = [item for item in results if item[0] == 429]
    print(f"[evidence-mysql-http] concurrent={CONCURRENT} budget_slots={BUDGET_SLOTS} "
          f"accepted={len(accepted)} rejected={len(rejected)} "
          f"statuses={sorted({code for code, _ in results})}")
    assert len(accepted) == BUDGET_SLOTS, (
        f"日预算只够 {BUDGET_SLOTS} 个，实际受理 {len(accepted)} 个："
        f"{[item[0] for item in results]}")
    assert len(rejected) == CONCURRENT - BUDGET_SLOTS
    for _code, text in rejected:
        assert "QUOTA_EXHAUSTED" in text, text

    with db_session_factory() as db:
        submissions = db.scalar(select(func.count()).select_from(PracticeSubmission)
                                .where(PracticeSubmission.problem_id == problem_id))
        ledger = db.scalar(
            select(PracticeUsageLog.amount).where(
                PracticeUsageLog.metric == "ai_tokens",
                PracticeUsageLog.scope == "platform")
        )
    print(f"[evidence-mysql-http] db_submissions={submissions} ledger_tokens={ledger} "
          f"budget_tokens={budget}")
    assert submissions == BUDGET_SLOTS, "受理数必须与落库提交数一致（不超发、不少发）"
    assert float(ledger) == float(budget), "台账必须恰好记 10 份预算"


def test_mysql_quota_holds_when_redis_counter_is_down(
        client, db_session_factory, test_settings):
    """Redis 计数完全不可用：库仍是权威 → 仍然恰好 10 个受理（行锁串行化）。"""
    import redis as _redis

    from app.services import practice_service

    problem_id, _headers, admin_header = _prepare(
        client, db_session_factory,
        token_estimate=test_settings.practice_ai_token_estimate)
    budget = BUDGET_SLOTS * test_settings.practice_ai_token_estimate
    configured = client.patch("/api/v1/admin/practice/settings",
                              json={"grading_mode": "ai",
                                    "daily_ai_budget_tokens": budget,
                                    "daily_submission_cap_per_student": 100},
                              headers=admin_header)
    assert configured.status_code == 200, configured.text
    _reset_usage(db_session_factory)

    from app.models import User

    with db_session_factory() as db:
        student_ids = [row.id for row in db.scalars(
            select(User).where(User.username.like(f"mysql-student-{RUN}-%")).order_by(User.id)
        ).all()]
    assert len(student_ids) == STUDENTS

    class _BrokenRedis:
        def __getattr__(self, name):
            def _boom(*args, **kwargs):
                raise _redis.exceptions.ConnectionError("redis counter down")

            return _boom

    broken = _BrokenRedis()
    barrier = threading.Barrier(CONCURRENT)

    def _submit(index: int):
        from fastapi import HTTPException

        with db_session_factory() as db:
            barrier.wait(timeout=30)
            try:
                practice_service.create_submission(
                    db, broken,
                    student=_student(db, student_ids[index % len(student_ids)]),
                    problem=_problem(db, problem_id), code=CODE,
                    client_request_id=f"svc-race-{index}",
                    token_estimate=test_settings.practice_ai_token_estimate,
                )
                return 202
            except HTTPException as exc:
                return exc.status_code

    with ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
        statuses = list(pool.map(_submit, range(CONCURRENT)))

    print(f"[evidence-mysql-broken-redis] concurrent={CONCURRENT} budget_slots={BUDGET_SLOTS} "
          f"accepted={statuses.count(202)} rejected={statuses.count(429)} "
          f"statuses={sorted(set(statuses))}")
    assert statuses.count(202) == BUDGET_SLOTS, (
        f"Redis 不可用时仍必须恰好受理 {BUDGET_SLOTS} 个，实际 {statuses.count(202)}")
    assert statuses.count(429) == CONCURRENT - BUDGET_SLOTS


def _student(db, student_id: int):
    from app.models import User

    return db.get(User, student_id)


def _problem(db, problem_id: int):
    from app.models.practice import PracticeProblem

    return db.get(PracticeProblem, problem_id)
