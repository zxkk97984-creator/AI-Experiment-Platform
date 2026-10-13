"""批次 5 核心验收：学生做题、判题开关、额度、版本隔离与榜单。

每一条「不该发生」的断言都配对照组；回滚实现后必须转红。
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import CodeGrade, JudgeGradingVersion, QuestionRubric
from app.models.practice import PracticeProblem, PracticeSubmission, PracticeUsageLog
from app.services import grading_version_service as versions
from app.services.grading_values import encode_value
from practice_test_support import (
    as_admin, as_student, create_and_publish, expected_value_map, grade_now,
    problem_payload,
)

RUBRIC_JSON = {
    "rubric_version": 1,
    "question_type": "函数实现",
    "learning_objective": "掌握基本运算",
    "explicit_requirements": ["返回整数"],
    "teacher_constraints": [],
    "accepted_strategies": ["直接返回"],
    "algorithm_criteria": [
        {"id": "A1", "name": "正确性", "points": 10},
        {"id": "A2", "name": "完整性", "points": 10},
    ],
    "quality_criteria": [
        {"id": "Q1", "name": "可读性", "points": 3},
        {"id": "Q2", "name": "结构", "points": 3},
        {"id": "Q3", "name": "冗余", "points": 2},
        {"id": "Q4", "name": "规范", "points": 2},
    ],
    "uncertain_items": [],
}

AI_GRADE_JSON = {
    "rubric_version": 1,
    "algorithm": {
        "dimension_score": 20, "dimension_max": 20,
        "items": [
            {"criterion_id": "A1", "criterion": "正确性", "level": "complete",
             "score": 10, "max_score": 10, "evidence": "第 2 行返回两数之和"},
            {"criterion_id": "A2", "criterion": "完整性", "level": "complete",
             "score": 10, "max_score": 10, "evidence": "覆盖了负号输入"},
        ],
    },
    "code_quality": {
        "dimension_score": 10, "dimension_max": 10,
        "items": [
            {"criterion_id": "Q1", "criterion": "命名", "level": "complete",
             "score": 3, "max_score": 3, "evidence": "命名清晰"},
            {"criterion_id": "Q2", "criterion": "结构", "level": "complete",
             "score": 3, "max_score": 3, "evidence": "结构简单"},
            {"criterion_id": "Q3", "criterion": "冗余", "level": "complete",
             "score": 2, "max_score": 2, "evidence": "没有重复代码"},
            {"criterion_id": "Q4", "criterion": "规范", "level": "complete",
             "score": 2, "max_score": 2, "evidence": "符合规范"},
        ],
    },
    "student_feedback": {"strengths": ["结构清晰"], "issues": [], "suggestions": []},
    "needs_teacher_review": False,
}

CODE = "def solution(a, b):\n    return a + b\n"


class _FakeAIClient:
    """可控 AI 客户端：默认返回合法评分；``error`` 时抛可/不可重试错误。"""

    def __init__(self, settings=None, *args, **kwargs):
        self._settings = settings
        self.error = None
        self.retryable = False
        self.calls = 0

    def chat_json(self, messages, operation=None, **kwargs):
        self.calls += 1
        if self.error is not None:
            from app.services.ai_client import AIServiceError

            raise AIServiceError("ai_grade_failed", str(self.error),
                                 retryable=self.retryable)
        return dict(AI_GRADE_JSON)


def _install_fake_ai(monkeypatch, *, error=None, retryable=False) -> _FakeAIClient:
    from app.services import ai_client

    client = _FakeAIClient()
    client.error = error
    client.retryable = retryable
    monkeypatch.setattr(ai_client, "DeepSeekClient", lambda settings, **kw: client)
    return client


def _make_ai_ready(db_session_factory, problem_id, admin_id, *, cases=None):
    """把题目配成 AI 就绪：locked rubric + active 版本（F=60/R=10）+ 题目口径 ai。"""
    case_list = cases or [
        {"id": "f1", "args": [1, 2], "expected": 3},
        {"id": "r1", "args": [-1, -1], "expected": -2},
    ]
    with db_session_factory() as db:
        rubric = QuestionRubric(
            practice_problem_id=problem_id, version=1, status="locked",
            source_hash="z" * 64, source_snapshot={}, rubric_json=dict(RUBRIC_JSON),
            model_name="test-model", locked_at=datetime.now(timezone.utc),
        )
        db.add(rubric)
        db.flush()
        payload = {
            "schema_version": 2, "entry_function": "solution",
            "execution_model": "isolated_case_v2", "binary_cases": [],
            "groups": [
                {"id": "F1", "dimension": "F", "max_score": 60.0, "cases": [
                    {"id": case_list[0]["id"], "input": encode_value(tuple(case_list[0]["args"])),
                     "expected": encode_value(case_list[0]["expected"])}]},
                {"id": "R1", "dimension": "R", "max_score": 10.0, "cases": [
                    {"id": case_list[1]["id"], "input": encode_value(tuple(case_list[1]["args"])),
                     "expected": encode_value(case_list[1]["expected"])}]},
            ],
        }
        version = versions.create_draft(
            db, question_kind="practice_problem", question_id=problem_id, payload=payload,
            entry_function="solution", rubric_id=rubric.id, source_kind="manual",
            grading_mode_snapshot="active", actor_id=admin_id, commit=False,
        )
        versions.confirm_version(db, version, actor_id=admin_id,
                                 confirm_isolated_case_v2=True, commit=False)
        versions.activate_version(db, version, actor_id=admin_id, commit=False)
        problem = db.get(PracticeProblem, problem_id)
        problem.grading_mode = "ai"
        versions.set_question_current_version(
            db, question_kind="practice_problem", question_id=problem_id,
            version_id=version.id)
        db.commit()
        return version.id


def _switch_ai(client, admin_header, db_session_factory, problem_id, admin_id):
    """配好 AI 评分标准并把全局开关切到 ai（预检通过才允许）。"""
    _make_ai_ready(db_session_factory, problem_id, admin_id)
    response = client.patch("/api/v1/admin/practice/settings",
                            json={"grading_mode": "ai"}, headers=admin_header)
    assert response.status_code == 200, response.text
    assert response.json()["grading_mode"] == "ai"


def _student_ids(client, student_header, *, board="deterministic", scope="platform"):
    response = client.get(f"/api/v1/practice/leaderboard?board={board}&scope={scope}",
                          headers=student_header)
    assert response.status_code == 200, response.text
    return response.json()["items"]


# ─────────────────────────────────────────────────────────────
# 权限与隔离
# ─────────────────────────────────────────────────────────────

def test_student_a_cannot_read_student_b_submission(client, db_session_factory):
    """学生 A 查不到学生 B 的提交（404，与"不存在"同形）；对照组：本人 200。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, alice = as_student(client, db_session_factory, "alice-practice")
    _, bob = as_student(client, db_session_factory, "bob-practice")

    created = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                          json={"code": CODE, "client_request_id": "alice-1"},
                          headers=alice)
    assert created.status_code == 202, created.text
    submission_id = created.json()["id"]

    own = client.get(f"/api/v1/practice/submissions/{submission_id}", headers=alice)
    assert own.status_code == 200
    other = client.get(f"/api/v1/practice/submissions/{submission_id}", headers=bob)
    assert other.status_code == 404
    assert "code" not in other.text or "return a + b" not in other.text

    mine = client.get("/api/v1/practice/me", headers=bob).json()
    assert mine["total"] == 0, "B 的列表里绝不该出现 A 的提交"


def test_teacher_cannot_read_practice_source_or_submission(client, db_session_factory):
    """教师拿不到学生源码与他人评分详情：提交详情 403、题目详情 403、被判题列表不含练习。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "kevin-practice")
    created = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                          json={"code": CODE, "client_request_id": "kevin-1"},
                          headers=student).json()
    from practice_test_support import as_teacher

    _, teacher = as_teacher(client, db_session_factory, "practice-teacher-read")
    assert client.get(f"/api/v1/practice/submissions/{created['id']}",
                      headers=teacher).status_code == 403
    assert client.get(f"/api/v1/practice/problems/{problem['id']}",
                      headers=teacher).status_code == 403
    assert client.get("/api/v1/ai-grading/grades", headers=teacher).json()["items"] == []


def test_teacher_leaderboard_is_aggregate_only(client, db_session_factory):
    """教师只读排行榜：能看到聚合名次，但响应里没有任何源码/学号字段。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "linda-practice")
    created = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                          json={"code": CODE, "client_request_id": "linda-1"},
                          headers=student).json()
    from practice_test_support import as_teacher

    _, teacher = as_teacher(client, db_session_factory, "practice-teacher-board")
    response = client.get("/api/v1/practice/leaderboard?board=deterministic&scope=platform",
                          headers=teacher)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == [], "还没判题就不该有成绩"
    # 判题后教师看到聚合行，但字段里没有 code/student_no/submission_id
    _grade_submission(db_session_factory, created["id"], problem["id"])

    body = client.get("/api/v1/practice/leaderboard?board=deterministic&scope=platform",
                      headers=teacher).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert set(item) == {"rank", "display_name", "solved_count", "total_score",
                         "avg_score", "achieved_at", "is_me"}
    assert "code" not in item and "student_no" not in item and "submission_id" not in item
    assert item["is_me"] is False, "教师不是学生，绝不能把任何行标成自己"


def _grade_submission(db_session_factory, submission_id, problem_id, *, pass_ids=None):
    """测试内同步判题：直接用假容器跑一次 worker 逻辑（不需要 Redis）。"""
    import unittest.mock as mock

    from app.config import get_settings
    from app.worker import practice_worker
    from practice_test_support import fake_docker_runner

    expected = expected_value_map(db_session_factory, problem_id)
    runner = fake_docker_runner(expected, pass_ids=pass_ids)
    with mock.patch.object(practice_worker, "_docker_runner", lambda: runner):
        practice_worker.grade_submission(db_session_factory, submission_id,
                                        get_settings(), None)


# ─────────────────────────────────────────────────────────────
# 普通判题 + 榜单
# ─────────────────────────────────────────────────────────────

def test_deterministic_grading_scores_and_board(client, db_session_factory,
                                                test_settings, redis_client, monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "score-student")
    created = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                          json={"code": CODE, "client_request_id": "score-1"},
                          headers=student)
    submission_id = created.json()["id"]
    assert created.json()["grading_status"] == "queued"
    assert created.json()["queue_position"] == 1

    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"]), test_settings)

    body = client.get(f"/api/v1/practice/submissions/{submission_id}",
                      headers=student).json()
    assert body["grading_status"] == "completed"
    assert body["score"] == 100.0
    assert body["ai_status"] == "skipped" and body["ai_result"] is None
    assert body["deterministic"]["passed_count"] == 2
    assert body["deterministic"]["total_count"] == 2

    board = _student_ids(client, student)
    assert board[0]["total_score"] == 100.0
    assert board[0]["solved_count"] == 1
    assert board[0]["is_me"] is True
    assert board[0]["display_name"].startswith("同学 "), "全平台榜必须匿名"
    # 两榜隔离：AI 榜不包含普通判题成绩
    assert _student_ids(client, student, board="ai") == []


def test_partial_pass_scores_proportionally(client, db_session_factory,
                                            test_settings, redis_client, monkeypatch):
    """2 个用例只过 1 个 → 50 分（对照组：全过 100 分）。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "partial-student")
    submission_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                                json={"code": CODE, "client_request_id": "partial-1"},
                                headers=student).json()["id"]
    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"]), test_settings,
              pass_ids={"c1"})
    body = client.get(f"/api/v1/practice/submissions/{submission_id}",
                      headers=student).json()
    assert body["score"] == 50.0
    assert body["deterministic"]["passed_count"] == 1


def test_idempotent_submissions_and_quota_charge(client, db_session_factory):
    """同一 client_request_id 提交 20 次：只有 1 条提交、只扣 1 次额度；
    换不同请求标识提交 20 次：20 条提交、扣 20 次（预算按实际判题次数扣减）。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "idem-student")

    first = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                        json={"code": CODE, "client_request_id": "same-key"},
                        headers=student)
    assert first.status_code == 202
    for _ in range(19):
        replay = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                             json={"code": CODE, "client_request_id": "same-key"},
                             headers=student)
        assert replay.status_code == 202
        assert replay.json()["id"] == first.json()["id"]

    with db_session_factory() as db:
        count = db.scalar(select(func.count()).select_from(PracticeSubmission)
                          .where(PracticeSubmission.problem_id == problem["id"]))
        assert count == 1, "重复提交不得新建记录"
        charged = db.scalar(
            select(PracticeUsageLog.amount).where(
                PracticeUsageLog.metric == "submissions",
                PracticeUsageLog.scope == "student")
        )
        assert charged == 1.0, "幂等重放不得重复扣额度"

    conflict = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                           json={"code": "def solution(a, b):\n    return a - b\n",
                                 "client_request_id": "same-key"},
                           headers=student)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "PRACTICE_IDEMPOTENCY_CONFLICT"

    for index in range(20):
        response = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                               json={"code": CODE, "client_request_id": f"distinct-{index}"},
                               headers=student)
        assert response.status_code == 202
    with db_session_factory() as db:
        count = db.scalar(select(func.count()).select_from(PracticeSubmission)
                          .where(PracticeSubmission.problem_id == problem["id"]))
        assert count == 21
        charged = db.scalar(
            select(PracticeUsageLog.amount).where(
                PracticeUsageLog.metric == "submissions",
                PracticeUsageLog.scope == "student")
        )
        assert charged == 21.0, "每次实际受理的提交都要记账"


def test_twenty_repeat_submissions_keep_best_score_and_charge_per_judging(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    """同一份代码提交 20 次：最高分与榜单不变；额度按**实际判题次数**扣减。

    * 同一 client_request_id 的 20 次点击 = 0 次新判题 → 不重复扣预算、不重复记分；
    * 20 个不同 client_request_id = 20 次实际受理（各扣 1 份预算），
      但每题只累计历史最高分，所以榜单仍是 100（不是 100×20）。
    """
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "repeat-student")
    expected = expected_value_map(db_session_factory, problem["id"])

    first = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                        json={"code": CODE, "client_request_id": "repeat-key"},
                        headers=student)
    assert first.status_code == 202
    grade_now(db_session_factory, redis_client, first.json()["id"], monkeypatch,
              expected, test_settings)
    assert _student_ids(client, student)[0]["total_score"] == 100.0
    ledger_after_first = _ledger(db_session_factory)
    assert ledger_after_first == 1.0

    for _ in range(19):
        replay = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                             json={"code": CODE, "client_request_id": "repeat-key"},
                             headers=student)
        assert replay.status_code == 202
        assert replay.json()["id"] == first.json()["id"]
    assert _ledger(db_session_factory) == ledger_after_first, "幂等重放不得扣预算"
    assert _student_ids(client, student)[0]["total_score"] == 100.0

    ids = []
    for index in range(20):
        created = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                              json={"code": CODE, "client_request_id": f"repeat-{index}"},
                              headers=student)
        assert created.status_code == 202
        ids.append(created.json()["id"])
    assert _ledger(db_session_factory) == 21.0, "每次实际受理占 1 份预算"
    for submission_id in ids:
        grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
                  expected, test_settings)
    with db_session_factory() as db:
        rows = db.scalar(select(func.count()).select_from(PracticeSubmission)
                         .where(PracticeSubmission.problem_id == problem["id"]))
        assert rows == 21
    board = _student_ids(client, student)
    assert len(board) == 1
    assert board[0]["total_score"] == 100.0, "只累计最高分，重复提交不得刷分"
    assert board[0]["solved_count"] == 1
    assert _ledger(db_session_factory) == 21.0, "判题不额外扣预算（提交时已扣）"


def _ledger(db_session_factory) -> float:
    with db_session_factory() as db:
        return float(db.scalar(
            select(PracticeUsageLog.amount).where(
                PracticeUsageLog.metric == "submissions",
                PracticeUsageLog.scope == "student")
        ) or 0.0)


def test_quota_exhausted_returns_429_with_code(client, db_session_factory):
    """每生日额度用尽 → 429 QUOTA_EXHAUSTED；对照组：调高额度后可继续提交。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "quota-student")
    client.patch("/api/v1/admin/practice/settings",
                 json={"daily_submission_cap_per_student": 1}, headers=admin_header)

    first = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                        json={"code": CODE, "client_request_id": "q-1"}, headers=student)
    assert first.status_code == 202
    second = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                         json={"code": CODE, "client_request_id": "q-2"}, headers=student)
    assert second.status_code == 429, second.text
    detail = second.json()["detail"]
    assert detail["code"] == "QUOTA_EXHAUSTED"
    assert detail["message"] == "今日额度已用完"
    assert detail["fields"]["limit"] == 1

    client.patch("/api/v1/admin/practice/settings",
                 json={"daily_submission_cap_per_student": 3}, headers=admin_header)
    third = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                        json={"code": CODE, "client_request_id": "q-3"}, headers=student)
    assert third.status_code == 202

    meta = client.get("/api/v1/practice/problems", headers=student).json()["meta"]
    assert meta["quota"]["used"] == 2.0
    assert meta["quota_exhausted"] is False


def test_quota_still_enforced_when_redis_is_broken(client, db_session_factory,
                                                   redis_client, monkeypatch):
    """Redis 故障以库为准、不得放行：计数不可用时额度仍按台账拦截。"""
    import redis as _redis

    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "redis-broken")
    client.patch("/api/v1/admin/practice/settings",
                 json={"daily_submission_cap_per_student": 1}, headers=admin_header)

    def _boom(*args, **kwargs):
        raise _redis.exceptions.ConnectionError("redis down")

    # 只打掉额度计数（认证等其它 Redis 读仍可用），模拟"计数服务不可用"
    monkeypatch.setattr(redis_client, "incrbyfloat", _boom)
    monkeypatch.setattr(redis_client, "expire", _boom)

    first = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                        json={"code": CODE, "client_request_id": "rb-1"},
                        headers=student)
    assert first.status_code == 202, first.text
    second = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                         json={"code": CODE, "client_request_id": "rb-2"},
                         headers=student)
    assert second.status_code == 429, second.text
    assert second.json()["detail"]["code"] == "QUOTA_EXHAUSTED"


# ─────────────────────────────────────────────────────────────
# 判题开关：只影响新提交，两榜不互相污染
# ─────────────────────────────────────────────────────────────

def test_mode_switch_only_affects_new_submissions(client, db_session_factory,
                                                  test_settings, redis_client, monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    admin, _ = as_admin(client, db_session_factory, "practice-admin-2")
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "switch-student")

    old_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                         json={"code": CODE, "client_request_id": "old-1"},
                         headers=student).json()["id"]
    grade_now(db_session_factory, redis_client, old_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"]), test_settings)

    # 先验证门禁：题目尚未配 AI 时开关必须被拒
    switched = client.patch("/api/v1/admin/practice/settings",
                            json={"grading_mode": "ai"}, headers=admin_header)
    assert switched.status_code == 409, "未完成 AI 配置的已发布题目必须拦住开关"
    assert switched.json()["detail"]["code"] == "AI_PREFLIGHT_FAILED"

    # 对照组：配好 AI 后同一开关动作成功
    _switch_ai(client, admin_header, db_session_factory, problem["id"], admin.id)
    _install_fake_ai(monkeypatch)
    new_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                         json={"code": CODE, "client_request_id": "new-1"},
                         headers=student).json()["id"]
    with db_session_factory() as db:
        new_row = db.get(PracticeSubmission, new_id)
        old_row = db.get(PracticeSubmission, old_id)
        assert new_row.grading_mode == "ai", "题目口径已切 ai，新提交必须走 AI"
        assert old_row.grading_mode == "deterministic", "历史提交的判题方式不得被改写"
        assert old_row.score == 100.0

    # AI 判题（假容器 + 假 AI 客户端）
    grade_now(db_session_factory, redis_client, new_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"], mode="ai"), test_settings,
              pass_ids={"f1", "r1"})
    from app.worker import practice_worker

    with db_session_factory() as db:
        code_grade_id = db.get(PracticeSubmission, new_id).code_grade_id
    assert code_grade_id is not None
    practice_worker.process_ai_grade_message(db_session_factory, test_settings, code_grade_id)

    new_body = client.get(f"/api/v1/practice/submissions/{new_id}", headers=student).json()
    assert new_body["grading_status"] == "completed"
    assert new_body["score"] == 100.0  # F60 + A20 + R10 + Q10
    assert new_body["ai_status"] == "completed" and new_body["ai_result"] is not None

    old_body = client.get(f"/api/v1/practice/submissions/{old_id}", headers=student).json()
    assert old_body["grading_status"] == "completed" and old_body["score"] == 100.0
    assert old_body["ai_status"] == "skipped"

    det_board = _student_ids(client, student, board="deterministic")
    ai_board = _student_ids(client, student, board="ai")
    assert len(det_board) == 1 and len(ai_board) == 1
    # 两榜各算各的：AI 提交不进普通榜（普通榜仍只有旧提交的 100 分），反之亦然
    assert det_board[0]["solved_count"] == 1
    assert ai_board[0]["solved_count"] == 1
    with db_session_factory() as db:
        new_row = db.get(PracticeSubmission, new_id)
        assert new_row.grading_mode == "ai"
        old_row = db.get(PracticeSubmission, old_id)
        assert old_row.grading_mode == "deterministic"


def test_problem_edit_moves_old_submissions_off_current_board(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    """改题生成新评分版本：旧提交从当前榜消失但历史可查；对照组：不改题仍在榜。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, published = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "version-student")
    submission_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                                json={"code": CODE, "client_request_id": "v-1"},
                                headers=student).json()["id"]
    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"]), test_settings)

    assert len(_student_ids(client, student)) == 1
    with db_session_factory() as db:
        first_version_id = db.get(PracticeProblem, problem["id"]).current_grading_version_id

    # 对照组：不改题重新发布 → 复用同一版本，成绩仍在榜
    again = client.post(f"/api/v1/admin/practice/problems/{problem['id']}/publish",
                        headers=admin_header)
    assert again.status_code == 200
    assert again.json()["grading_version_id"] == first_version_id
    assert len(_student_ids(client, student)) == 1

    patched = client.patch(
        f"/api/v1/admin/practice/problems/{problem['id']}",
        json={"hidden_tests": [{"id": "c1", "args": [1, 2], "expected": 3},
                               {"id": "c2", "args": [3, 4], "expected": 7},
                               {"id": "c3", "args": [5, 6], "expected": 11}]},
        headers=admin_header)
    assert patched.status_code == 200, patched.text
    republished = client.post(f"/api/v1/admin/practice/problems/{problem['id']}/publish",
                              headers=admin_header)
    assert republished.status_code == 200
    assert republished.json()["grading_version_id"] != first_version_id, \
        "改题必须生成新的评分版本"

    assert _student_ids(client, student) == [], "旧成绩不得与新版本混排"
    history = client.get(f"/api/v1/practice/submissions/{submission_id}",
                         headers=student).json()
    assert history["score"] == 100.0, "历史成绩必须保留"
    assert history["grading_version_stale"] is True, "UI 要能提示「本题评分标准已更新」"
    mine = client.get("/api/v1/practice/me", headers=student).json()
    assert mine["total"] == 1


# ─────────────────────────────────────────────────────────────
# AI 失败不写 0 分
# ─────────────────────────────────────────────────────────────

def test_ai_failure_keeps_deterministic_score_and_never_zero(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    admin, _ = as_admin(client, db_session_factory, "practice-admin-3")
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "ai-fail-student")
    _switch_ai(client, admin_header, db_session_factory, problem["id"], admin.id)

    submission_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                                json={"code": CODE, "client_request_id": "aifail-1"},
                                headers=student).json()["id"]
    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"], mode="ai"),
              test_settings, pass_ids={"f1", "r1"})
    with db_session_factory() as db:
        row = db.get(PracticeSubmission, submission_id)
        assert row.deterministic_score == 70.0, "确定性部分应先落库（F60+R10）"
        assert row.score is None, "AI 未完成前不得公布总分"
        code_grade_id = row.code_grade_id

    _install_fake_ai(monkeypatch, error="模拟 AI 服务不可用", retryable=False)
    from app.worker import practice_worker

    practice_worker.process_ai_grade_message(db_session_factory, test_settings, code_grade_id)

    with db_session_factory() as db:
        row = db.get(PracticeSubmission, submission_id)
        assert row.grading_status == "review_required"
        assert row.score == 70.0, "AI 失败时必须展示已完成的确定性分"
        assert row.score != 0.0, "绝不把未完成评分写成 0 分"
        assert row.last_error and "AI 评分未完成" in row.last_error

    body = client.get(f"/api/v1/practice/submissions/{submission_id}", headers=student).json()
    assert body["ai_status"] == "failed"
    assert body["ai_result"] is None, "未完成的 AI 评分绝不冒充完整成绩"
    assert body["score"] == 70.0
    assert body["deterministic"]["passed_count"] == 2


def test_ai_success_merges_four_dimensions(client, db_session_factory, test_settings,
                                           redis_client, monkeypatch):
    """对照组：AI 正常完成时四维合分 100，ai_result 有内容且 ai_status=completed。"""
    _, admin_header = as_admin(client, db_session_factory)
    admin, _ = as_admin(client, db_session_factory, "practice-admin-4")
    problem, _ = create_and_publish(client, admin_header)
    _, student = as_student(client, db_session_factory, "ai-ok-student")
    _switch_ai(client, admin_header, db_session_factory, problem["id"], admin.id)
    submission_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                                json={"code": CODE, "client_request_id": "aiok-1"},
                                headers=student).json()["id"]
    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem["id"], mode="ai"),
              test_settings, pass_ids={"f1", "r1"})
    fake = _install_fake_ai(monkeypatch)
    from app.worker import practice_worker

    with db_session_factory() as db:
        code_grade_id = db.get(PracticeSubmission, submission_id).code_grade_id
    practice_worker.process_ai_grade_message(db_session_factory, test_settings, code_grade_id)
    assert fake.calls == 1

    body = client.get(f"/api/v1/practice/submissions/{submission_id}", headers=student).json()
    assert body["grading_status"] == "completed"
    assert body["score"] == 100.0
    assert body["ai_status"] == "completed"
    assert body["ai_result"]["functional_score"] == 60.0
    assert body["ai_result"]["algorithm_score"] == 20
    assert body["ai_result"]["quality_score"] == 10
    assert body["can_appeal"] is True


# ─────────────────────────────────────────────────────────────
# 队列隔离：练习积压不影响正式判题
# ─────────────────────────────────────────────────────────────

def test_practice_queue_backlog_does_not_touch_formal_judge_queues(
        client, db_session_factory, redis_client, test_settings, monkeypatch):
    """练习积压时正式判题队列零增长；AI 评分也按目标分流到各自的队列。"""
    from app.config import get_settings
    from app.worker import judge_worker, practice_worker
    from app.services import ai_grading_queue, practice_queue

    settings = get_settings()
    # 0) 练习 worker 只消费练习队列，正式 worker 不消费练习队列（双向零交集）
    practice_queues = set(practice_worker.worker_queue_names(settings))
    assert practice_queues == {settings.practice_judge_queue_name,
                               settings.practice_ai_queue_name}
    assert not practice_queues & {settings.judge_queue_name, settings.ai_queue_name,
                                  settings.ai_setup_queue_name,
                                  judge_worker.EXAM_JUDGE_QUEUE}
    # 1) 角色隔离：正式 worker 不监听练习队列
    judge_queues = judge_worker._worker_queue_names(SimpleNamespace(
        worker_role="judge", judge_queue_name=settings.judge_queue_name,
        ai_queue_name=settings.ai_queue_name, ai_setup_queue_name=settings.ai_setup_queue_name,
        ai_enabled=True, practice_judge_queue_name=settings.practice_judge_queue_name,
        practice_ai_queue_name=settings.practice_ai_queue_name))
    assert settings.practice_judge_queue_name not in judge_queues
    assert settings.practice_ai_queue_name not in judge_queues

    # 2) 练习积压 50 条：正式队列长度不变
    for index in range(50):
        practice_queue.push_submission(redis_client, index + 1)
    assert redis_client.llen(settings.practice_judge_queue_name) == 50
    assert redis_client.llen(settings.judge_queue_name) == 0
    assert redis_client.llen(settings.ai_queue_name) == 0
    assert redis_client.llen(settings.exam_judge_queue_name if hasattr(
        settings, "exam_judge_queue_name") else judge_worker.EXAM_JUDGE_QUEUE) == 0

    # 3) AI 评分按目标分流：练习 → practice:ai:queue；作业 → judge:ai:queue（对照组）
    from conftest import make_code_grade

    assignment_grade_id = make_code_grade(db_session_factory)
    with db_session_factory() as db:
        assert ai_grading_queue.enqueue_ai_grade(db, redis_client, assignment_grade_id) is True
    assert redis_client.llen(settings.ai_queue_name) == 1
    assert redis_client.llen(settings.practice_ai_queue_name) == 0

    _, admin_header = as_admin(client, db_session_factory)
    admin, _ = as_admin(client, db_session_factory, "practice-admin-queue")
    problem, _ = create_and_publish(client, admin_header)
    _switch_ai(client, admin_header, db_session_factory, problem["id"], admin.id)
    _, student = as_student(client, db_session_factory, "queue-student")
    submission_id = client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                                json={"code": CODE, "client_request_id": "queue-1"},
                                headers=student).json()["id"]
    from practice_test_support import fake_docker_runner, install_fake_runner
    from app.worker import practice_worker

    expected = expected_value_map(db_session_factory, problem["id"], mode="ai")
    install_fake_runner(monkeypatch, fake_docker_runner(expected), redis_client=redis_client)
    practice_worker.grade_submission(db_session_factory, submission_id, settings, redis_client)
    assert redis_client.llen(settings.practice_ai_queue_name) == 1, \
        "练习 AI 评分必须进独立队列"
    assert redis_client.llen(settings.ai_queue_name) == 1, "正式 AI 队列不受练习影响"
