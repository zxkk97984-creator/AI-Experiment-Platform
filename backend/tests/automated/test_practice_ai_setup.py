"""批次 5：练习题库复用批次 2 的一键 AI 配置流水线。

必过验收（管理员完整录题流程里的「配 AI」一步）：

* `question_kind='practice_problem'` 被 setup-jobs 接受，**但只有 admin**——
  教师 403（不是 404/422）；
* 幂等：重复点击复用同一 job，库里只有 1 行；
* 生成阶段只出草稿（rubric 不锁定、无 active 版本），显式 enable 后
  题目口径转 `ai`、出现 active 版本且绑定 locked rubric；
* AI 预检在 enable 前列出未就绪题目、enable 后为 ready（对照组）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import AiGradingSetupJob, JudgeGradingVersion, QuestionRubric
from app.models.practice import PracticeProblem
from app.schemas.ai_grading import TestGroup
from app.services import ai_grading_setup_service as setup
from app.services import grading_version_service as versions
from practice_test_support import as_admin, as_teacher, problem_payload

API = "/api/v1/ai-grading"

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

TEST_GROUPS = [
    TestGroup(id="F1", name="功能", dimension="F", max_score=60.0,
              tests="def test_solution():\n    assert solution(1, 2) == 3\n"),
    TestGroup(id="R1", name="边界", dimension="R", max_score=10.0,
              tests="def test_boundary():\n    assert solution(0, 0) == 0\n"),
]


class _FakeAIClient:
    def __init__(self, settings=None, *args, **kwargs):
        self._settings = settings

    def chat_json(self, messages, operation=None, **kwargs):
        return RUBRIC_JSON


def _patch_ai(monkeypatch):
    monkeypatch.setattr(setup, "DeepSeekClient", _FakeAIClient)

    def fake_generate(client, snapshot, settings, **kwargs):
        return SimpleNamespace(test_groups=list(TEST_GROUPS), warnings=[],
                               generation_id="gen-practice-1")

    monkeypatch.setattr(setup, "generate_test_groups", fake_generate)


def _create_problem(client, admin_header) -> int:
    response = client.post("/api/v1/admin/practice/problems",
                           json=problem_payload(hidden_tests=[
                               {"id": "c1", "args": [1, 2], "expected": 3}]),
                           headers=admin_header)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _job_count(db_session_factory, problem_id: int) -> int:
    with db_session_factory() as db:
        return int(db.scalar(
            select(func.count()).select_from(AiGradingSetupJob).where(
                AiGradingSetupJob.question_kind == "practice_problem",
                AiGradingSetupJob.question_id == problem_id)
        ) or 0)


def test_teacher_cannot_create_practice_setup_job(client, db_session_factory):
    """教师写接口必须 403：练习题库的 AI 配置入口同样只对管理员开放。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem_id = _create_problem(client, admin_header)
    _, teacher_header = as_teacher(client, db_session_factory)
    response = client.post(f"{API}/setup-jobs", headers=teacher_header,
                           json={"question_kind": "practice_problem",
                                 "question_id": problem_id})
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "FORBIDDEN"
    assert _job_count(db_session_factory, problem_id) == 0


def test_admin_practice_setup_job_is_idempotent_and_enables_ai(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    _patch_ai(monkeypatch)
    _, admin_header = as_admin(client, db_session_factory)
    problem_id = _create_problem(client, admin_header)
    # 先按普通判题发布：此时题目是已发布但 AI 未就绪，正是预检要拦的场景
    published = client.post(f"/api/v1/admin/practice/problems/{problem_id}/publish",
                            headers=admin_header)
    assert published.status_code == 200, published.text
    assert published.json()["grading_mode"] == "deterministic"

    preflight_before = client.get("/api/v1/admin/practice/ai-preflight",
                                  headers=admin_header).json()
    assert preflight_before["ready"] is False, "已发布但未配 AI 的题目必须被列出"
    listed = {item["id"]: item for item in preflight_before["problems"]}
    assert problem_id in listed
    assert "active_version" in listed[problem_id]["missing"]
    assert listed[problem_id]["status"] == "published"

    blocked = client.patch("/api/v1/admin/practice/settings",
                           json={"grading_mode": "ai"}, headers=admin_header)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "AI_PREFLIGHT_FAILED"
    assert [item["id"] for item in blocked.json()["detail"]["fields"]["problems"]] == [problem_id]

    created = client.post(f"{API}/setup-jobs", headers=admin_header,
                          json={"question_kind": "practice_problem",
                                "question_id": problem_id,
                                "grading_mode": "active"})
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert created.json()["stage"] == "queued"
    assert created.json()["reused"] is False
    assert _job_count(db_session_factory, problem_id) == 1

    repeated = client.post(f"{API}/setup-jobs", headers=admin_header,
                           json={"question_kind": "practice_problem",
                                 "question_id": problem_id,
                                 "grading_mode": "active"})
    assert repeated.status_code == 202
    assert repeated.json()["reused"] is True
    assert repeated.json()["id"] == job_id, "重复点击必须复用同一任务"
    assert _job_count(db_session_factory, problem_id) == 1, "重复点击不得新建任务"

    # 生成阶段只出草稿：rubric 未锁定、没有 active 版本
    with db_session_factory() as db:
        setup.run_setup_job(db, redis_client, test_settings, job_id)
    detail = client.get(f"{API}/setup-jobs/{job_id}", headers=admin_header)
    assert detail.status_code == 200, detail.text
    assert detail.json()["stage"] == "draft_ready", detail.json()
    assert detail.json()["draft_rubric_status"] == "draft"
    with db_session_factory() as db:
        # 发布普通判题版本是另一条口径；这里断言**没有 AI 口径**的评分版本
        assert db.scalar(
            select(func.count()).select_from(JudgeGradingVersion).where(
                JudgeGradingVersion.practice_problem_id == problem_id,
                JudgeGradingVersion.grading_mode_snapshot == "active")
        ) == 0, "草稿阶段不得出现 AI 评分版本"

    enabled = client.post(f"{API}/setup-jobs/{job_id}/enable", headers=admin_header, json={})
    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert body["status"] == "active"
    assert body["grading_mode"] == "ai"

    with db_session_factory() as db:
        problem = db.get(PracticeProblem, problem_id)
        assert problem.grading_mode == "ai", "enable 必须把题目口径切到 ai"
        version = db.get(JudgeGradingVersion, body["version_id"])
        assert version is not None
        assert version.practice_problem_id == problem_id
        assert version.judge_question_id is None and version.exam_question_id is None
        assert version.grading_mode_snapshot == "active"
        assert problem.current_grading_version_id == version.id
        rubric = db.get(QuestionRubric, version.rubric_id)
        assert rubric.status == "locked" and rubric.practice_problem_id == problem_id
        assert versions.rubric_binding_ok(db, version) is True
    assert _job_count(db_session_factory, problem_id) == 1

    ready = client.get("/api/v1/admin/practice/ai-preflight", headers=admin_header).json()
    assert ready["ready"] is True
    assert ready["problems"] == []

    # 对照组：预检通过后同一个开关动作成功
    switched = client.patch("/api/v1/admin/practice/settings",
                            json={"grading_mode": "ai"}, headers=admin_header)
    assert switched.status_code == 200, switched.text
    assert switched.json()["grading_mode"] == "ai"


def test_teacher_cannot_read_or_enable_practice_setup_job(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    _patch_ai(monkeypatch)
    _, admin_header = as_admin(client, db_session_factory)
    problem_id = _create_problem(client, admin_header)
    job_id = client.post(f"{API}/setup-jobs", headers=admin_header,
                         json={"question_kind": "practice_problem",
                               "question_id": problem_id}).json()["id"]
    _, teacher_header = as_teacher(client, db_session_factory)
    assert client.get(f"{API}/setup-jobs/{job_id}",
                      headers=teacher_header).status_code == 403
    assert client.post(f"{API}/setup-jobs/{job_id}/retry",
                       headers=teacher_header).status_code == 403
    assert client.post(f"{API}/setup-jobs/{job_id}/enable", headers=teacher_header,
                       json={}).status_code == 403
