"""批次 6 真库容量验收：200 并发混合业务——正式判题不劣化 + 练习超额明确排队。

必须在 **真实 MySQL + 真实 Redis** 上跑（本文件在 SQLite 下 skip）：练习额度台账、
队列隔离与"正式判题不劣化"都依赖真实行锁与真实 Redis 队列；SQLite 的
``with_for_update`` 是空操作，fakeredis 也证明不了队列隔离。

证据：
* 200 个并发请求（160 个练习提交 + 40 个正式作业提交）同批发射；
* 练习侧：全部 202 且停在 ``queued``（并发上限 2、无 worker），队列深度 = 160，
  绝不挤占 ``judge:queue``；
* 正式侧：40 个全部受理且进入 ``judge:queue``（不混入练习消息），
  并发混合下的 p95 与无练习负载基线的 p95 对比（打印真数字）。
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.models import Assignment, JudgeQuestion, Submission
from app.models.practice import PracticeSubmission
from test_school_offerings import school  # noqa: F401 - 复用官方作业/教学班夹具

MYSQL_URL = os.environ.get("DAI_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not MYSQL_URL.startswith("mysql"),
    reason="200 并发混合业务验收必须在真实 MySQL + Redis 上跑（SQLite/fakeredis 证明不了队列隔离与行锁）",
)

PRACTICE_THREADS = 160
OFFICIAL_THREADS = 40
TOTAL_THREADS = PRACTICE_THREADS + OFFICIAL_THREADS
PRACTICE_STUDENTS = 20
PER_STUDENT = PRACTICE_THREADS // PRACTICE_STUDENTS
BASELINE_OFFICIAL = 40
CODE = "def f(): return 1"
RUN = secrets.token_hex(4)


def _prepare_practice(client, db_session_factory):
    from app.models.practice import PracticeProblem  # noqa: F401
    from practice_test_support import as_admin, as_student, create_and_publish

    _, admin_header = as_admin(client, db_session_factory, f"cap-admin-{RUN}")
    problem, _version = create_and_publish(client, admin_header, title=f"容量题-{RUN}")
    headers = []
    for index in range(PRACTICE_STUDENTS):
        username = f"cap-student-{RUN}-{index}"
        as_student(client, db_session_factory, username)
        from conftest import auth_header, login

        token, _ = login(client, username)
        headers.append(auth_header(token))
    updated = client.patch("/api/v1/admin/practice/settings",
                           json={"daily_submission_cap_per_student": 100,
                                 "max_concurrent_practice_jobs": 2},
                           headers=admin_header)
    assert updated.status_code == 200, updated.text
    return problem["id"], headers


def _prepare_official(school, db_session_factory):
    with db_session_factory() as db:
        task = Assignment(course_id=school["course"], offering_id=school["offerings"][0],
                          title=f"容量正式作业-{RUN}", status="published")
        db.add(task)
        db.flush()
        question = JudgeQuestion(assignment_id=task.id, title="容量正式题",
                                 function_name="f", hidden_tests="private-check",
                                 public_cases=[{"args": [], "expected": 1}],
                                 grading_mode="legacy")
        db.add(question)
        db.commit()
        return question.id


def _text(value) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return round(ordered[index] * 1000, 1)  # ms


def _run_burst(app, app_client_cls, *, official_question_id, official_headers,
               practice_problem_id, practice_headers) -> dict:
    """一次性发射 200 个并发请求：160 练习 + 40 正式，返回各侧延迟与状态。"""
    barrier = threading.Barrier(TOTAL_THREADS)

    def _practice(index: int):
        own = app_client_cls(app)
        header = practice_headers[index % len(practice_headers)]
        try:
            barrier.wait(timeout=60)
            started = time.monotonic()
            response = own.post(
                f"/api/v1/practice/problems/{practice_problem_id}/submissions",
                json={"code": "def solution(a, b):\n    return a + b\n",
                      "client_request_id": f"cap-{RUN}-{index}"},
                headers=header)
            return ("practice", response.status_code, time.monotonic() - started, response.text)
        finally:
            own.close()

    def _official(index: int):
        own = app_client_cls(app)
        header = official_headers[index % len(official_headers)]
        try:
            barrier.wait(timeout=60)
            started = time.monotonic()
            response = own.post("/api/v1/judge/submissions",
                                json={"question_id": official_question_id, "code": CODE},
                                headers=header)
            return ("official", response.status_code, time.monotonic() - started, response.text)
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=TOTAL_THREADS) as pool:
        futures = [pool.submit(_practice, index) for index in range(PRACTICE_THREADS)]
        futures += [pool.submit(_official, index) for index in range(OFFICIAL_THREADS)]
        results = [future.result(timeout=120) for future in futures]

    def _side(kind):
        rows = [row for row in results if row[0] == kind]
        return {
            "statuses": sorted({row[1] for row in rows}),
            "latencies": [row[2] for row in rows],
            "bodies": rows,
        }

    return {"practice": _side("practice"), "official": _side("official")}


def test_two_hundred_concurrent_mixed_requests_keep_official_judging_healthy(
        school, client, app, db_session_factory, test_settings, redis_client):
    from app.config import get_settings
    from app.services import practice_queue
    from conftest import SynchronousASGIClient

    settings = get_settings()
    problem_id, practice_headers = _prepare_practice(client, db_session_factory)
    question_id = _prepare_official(school, db_session_factory)
    # 只用作业受众内的学生（学生 b 不在该 offering 受众内，会得到同形 404）
    official_headers = [school["headers"]["a"]]

    # ── 基线：无练习负载时 40 个并发正式提交 ──
    # ── 同并发度基线：200 个并发正式提交（无练习负载），作为「不劣化」的对照 ──
    baseline_barrier = threading.Barrier(TOTAL_THREADS)

    def _baseline_official(index: int):
        own = SynchronousASGIClient(app)
        try:
            baseline_barrier.wait(timeout=60)
            started = time.monotonic()
            response = own.post("/api/v1/judge/submissions",
                                json={"question_id": question_id, "code": CODE},
                                headers=official_headers[index % len(official_headers)])
            return response.status_code, time.monotonic() - started, response.text
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=TOTAL_THREADS) as pool:
        baseline_rows = list(pool.map(_baseline_official, range(TOTAL_THREADS)))
    assert all(row[0] in (200, 201, 202) for row in baseline_rows), \
        [row[0] for row in baseline_rows if row[0] not in (200, 201, 202)][:3]
    baseline_ms = [row[1] for row in baseline_rows]
    baseline_p95 = _percentile(baseline_ms, 0.95)
    print(f"[evidence-capacity-baseline] official_only={TOTAL_THREADS} "
          f"p50={_percentile(baseline_ms, 0.5)}ms p95={baseline_p95}ms")

    # ── 混合：160 练习 + 40 正式同时发射 ──
    burst = _run_burst(app, SynchronousASGIClient, official_question_id=question_id,
                       official_headers=official_headers,
                       practice_problem_id=problem_id,
                       practice_headers=practice_headers)
    practice_side, official_side = burst["practice"], burst["official"]
    print(f"[evidence-capacity-mixed] total={TOTAL_THREADS} "
          f"practice={PRACTICE_THREADS} official={OFFICIAL_THREADS} "
          f"practice_statuses={practice_side['statuses']} "
          f"official_statuses={official_side['statuses']} "
          f"practice_p95={_percentile(practice_side['latencies'], 0.95)}ms "
          f"official_p95={_percentile(official_side['latencies'], 0.95)}ms")

    # 练习侧：全部受理并明确排队（没有 worker 消费）
    assert practice_side["statuses"] == [202], practice_side["statuses"]
    with db_session_factory() as db:
        queued = db.scalar(
            select(func.count()).select_from(PracticeSubmission).where(
                PracticeSubmission.problem_id == problem_id,
                PracticeSubmission.grading_status == "queued"))
        running = db.scalar(
            select(func.count()).select_from(PracticeSubmission).where(
                PracticeSubmission.problem_id == problem_id,
                PracticeSubmission.grading_status == "running"))
    assert queued == PRACTICE_THREADS, (queued, running)
    assert running == 0, "没有 worker 时不得出现 running"
    depth = practice_queue.queue_depth(redis_client)
    assert depth == PRACTICE_THREADS, depth
    print(f"[evidence-capacity-queue] practice_queued={queued} practice_queue_depth={depth} "
          f"max_concurrent={settings.practice_judge_queue_name}")

    # 队列隔离：正式消息只在 judge:queue，练习消息只在 practice:judge:queue
    official_queue = int(redis_client.llen(settings.judge_queue_name))
    # 基线 200 条正式消息 + 混合批次的 40 条正式消息；练习一条都没混进来
    assert official_queue == TOTAL_THREADS + OFFICIAL_THREADS, official_queue
    messages = redis_client.lrange(settings.practice_judge_queue_name, 0, -1)
    assert len(messages) == PRACTICE_THREADS, len(messages)
    assert all("practice_judge" in _text(message) for message in messages)
    official_messages = redis_client.lrange(settings.judge_queue_name, 0, -1)
    assert all("practice" not in _text(message) for message in official_messages), \
        "正式判题队列里绝不能出现练习消息"
    print(f"[evidence-capacity-isolation] judge_queue={official_queue} "
          f"practice_queue={len(messages)} cross_talk=0")

    # 正式侧：全部受理，且落库为待判题（练习积压不影响正式业务）
    assert all(code in (200, 201, 202) for code in official_side["statuses"]), \
        official_side["statuses"]
    with db_session_factory() as db:
        official_pending = db.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.question_id == question_id))
    assert official_pending == TOTAL_THREADS + OFFICIAL_THREADS, official_pending

    # 不劣化：同一并发度（200）下，练习负载在场时正式提交的 p95 不得超过
    # 「纯正式 200 并发」基线的 1.5 倍（并给绝对上限，避免基线过小时误报）
    mixed_p95 = _percentile(official_side["latencies"], 0.95)
    assert mixed_p95 <= max(baseline_p95 * 1.5, 800.0), (baseline_p95, mixed_p95)
    print(f"[evidence-capacity-compare] baseline200_official_p95={baseline_p95}ms "
          f"mixed_official_p95={mixed_p95}ms "
          f"ratio={round(mixed_p95 / max(baseline_p95, 0.001), 2)} "
          f"practice_p95={_percentile(practice_side['latencies'], 0.95)}ms")
