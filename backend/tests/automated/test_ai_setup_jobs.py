"""批次 2 验收：一键 AI 配置（草稿不提前启用 / 失败不半提交 / 幂等）与草稿试判。

对照组设计：每条「不该发生」的断言，都配一条同条件下「应当发生」的断言，
回滚实现后测试必须转红。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import (
    AiGradingSetupJob,
    Assignment,
    CodeGrade,
    ExamAnswer,
    JudgeGradingVersion,
    JudgeQuestion,
    QuestionRubric,
    RubricTrialRun,
    Submission,
)
from app.schemas.ai_grading import TestGroup
from app.services import ai_grading_setup_service as setup
from app.services import grading_version_service as versions
from app.services import rubric_service
from tests.automated.conftest import (
    auth_header,
    login,
    make_assignment,
    make_judge_question,
    make_submission,
    make_teacher,
)

API = "/api/v1/ai-grading"

RUBRIC_JSON = {
    "rubric_version": 1,
    "question_type": "函数实现",
    "learning_objective": "掌握循环与边界处理",
    "explicit_requirements": ["返回整数"],
    "teacher_constraints": ["不得使用递归"],
    "accepted_strategies": ["迭代"],
    "algorithm_criteria": [
        {"id": "A1", "name": "正确性", "points": 10},
        {"id": "A2", "name": "完整性", "points": 10},
    ],
    "quality_criteria": [
        {"id": "Q1", "name": "可读性与命名", "points": 3},
        {"id": "Q2", "name": "代码结构", "points": 3},
        {"id": "Q3", "name": "重复与冗余", "points": 2},
        {"id": "Q4", "name": "接口、规范与安全", "points": 2},
    ],
    "uncertain_items": [],
}

AI_GRADE_JSON = {
    "rubric_version": 1,
    "algorithm": {
        "dimension_score": 20, "dimension_max": 20,
        "items": [
            {"criterion_id": "A1", "criterion": "正确性", "level": "complete",
             "score": 10, "max_score": 10, "evidence": "第 2 行返回了输入值"},
            {"criterion_id": "A2", "criterion": "完整性", "level": "complete",
             "score": 10, "max_score": 10, "evidence": "覆盖了基本输入"},
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

TEST_GROUPS = [
    TestGroup(id="F1", name="功能", dimension="F", max_score=60.0,
              tests="def test_solve():\n    assert solve(1) == 1\n"),
    TestGroup(id="R1", name="边界", dimension="R", max_score=10.0,
              tests="def test_boundary():\n    assert solve(0) == 0\n"),
]


class _FakeAIClient:
    def __init__(self, settings=None, *args, **kwargs):
        self._settings = settings

    def chat_json(self, messages, operation=None, **kwargs):
        return RUBRIC_JSON if operation == "rubric_generation" else AI_GRADE_JSON


class _RecordingClient:
    """发布门禁用的记录式客户端：被调用即说明草稿没有满足「已锁定 Rubric」。"""

    def __init__(self, settings=None, *args, **kwargs):
        self._settings = settings or SimpleNamespace(ai_model="test-model")
        self.calls = 0

    def chat_json(self, messages, operation=None, **kwargs):
        self.calls += 1
        return RUBRIC_JSON


def _patch_ai(monkeypatch, *, groups=None, fail_groups=False):
    monkeypatch.setattr(setup, "DeepSeekClient", _FakeAIClient)

    def fake_generate(client, snapshot, settings, **kwargs):
        if fail_groups:
            raise RuntimeError("模拟测试组生成失败")
        return SimpleNamespace(test_groups=list(groups or TEST_GROUPS), warnings=[],
                               generation_id="gen-test-1")

    monkeypatch.setattr(setup, "generate_test_groups", fake_generate)


@pytest.fixture()
def world(db_session_factory):
    """教师 + 未发布作业 + 一道 active 模式代码题（可配置一键 AI）。"""
    make_teacher(db_session_factory)
    assignment_id = make_assignment(db_session_factory, status="draft")
    question_id = make_judge_question(db_session_factory, assignment_id=assignment_id,
                                      function_name="solve")
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        question.grading_mode = "active"
        # 题目现有测试组与「AI 生成结果」保持一致：让发布门禁的来源摘要比对有真实意义
        question.test_groups = [group.model_dump() for group in TEST_GROUPS]
        db.commit()
    return SimpleNamespace(assignment_id=assignment_id, question_id=question_id)


def _headers(client):
    token, _ = login(client, "teacher")
    return auth_header(token)


def _create_job(client, headers, world, **extra):
    payload = {"question_kind": "assignment", "question_id": world.question_id}
    payload.update(extra)
    response = client.post(f"{API}/setup-jobs", headers=headers, json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def _run_job(db_session_factory, test_settings, redis_client, job_id):
    with db_session_factory() as db:
        setup.run_setup_job(db, redis_client, test_settings, job_id)


def _job(db_session_factory, job_id) -> AiGradingSetupJob:
    with db_session_factory() as db:
        return db.get(AiGradingSetupJob, job_id)


def _table_counts(db_session_factory) -> dict:
    with db_session_factory() as db:
        return {
            "submissions": db.scalar(select(func.count()).select_from(Submission)),
            "exam_answers": db.scalar(select(func.count()).select_from(ExamAnswer)),
            "code_grades": db.scalar(select(func.count()).select_from(CodeGrade)),
            "attempts": db.scalar(select(func.coalesce(func.sum(Submission.attempt_count), 0))),
        }


# ── 验收 A：草稿不会提前启用 ─────────────────────────────────────


def test_draft_ready_job_keeps_rubric_draft_and_gate_closed(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    detail = client.get(f"{API}/setup-jobs/{job_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["stage"] == "draft_ready", body
    assert body["progress"] == 100
    assert body["draft_rubric_status"] == "draft"

    with db_session_factory() as db:
        rubric = db.get(QuestionRubric, body["draft_rubric_id"])
        assert rubric.status == "draft", "生成阶段不得锁定评分标准"
        assert rubric.locked_at is None
        assert rubric_service.get_latest_locked_rubric(
            db, kind="assignment", question_id=world.question_id) is None
        assert versions.active_version_for(
            db, question_kind="assignment", question_id=world.question_id) is None
        assert db.scalar(select(func.count()).select_from(JudgeGradingVersion).where(
            JudgeGradingVersion.judge_question_id == world.question_id)) == 0

        # 发布门禁：草稿不满足门禁，门禁仍会去生成并锁定一份新的 Rubric
        question = db.get(JudgeQuestion, world.question_id)
        recorder = _RecordingClient()
        rubric_service.ensure_locked_rubrics_for_publish(db, recorder, [question])
        db.commit()
        assert recorder.calls == 1, "草稿不得满足发布门禁"
        assert db.scalar(select(func.count()).select_from(QuestionRubric).where(
            QuestionRubric.judge_question_id == world.question_id)) == 2
        assert rubric_service.get_latest_locked_rubric(
            db, kind="assignment", question_id=world.question_id) is not None


def test_enable_locks_rubric_and_opens_gate(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    """对照组：显式 enable 之后同一门禁断言必须成立（已经不用再生成）。"""
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    response = client.post(f"{API}/setup-jobs/{job_id}/enable", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"

    with db_session_factory() as db:
        rubric = db.get(QuestionRubric, response.json()["rubric_id"])
        assert rubric.status == "locked"
        assert rubric.locked_at is not None
        active = versions.active_version_for(
            db, question_kind="assignment", question_id=world.question_id)
        assert active is not None and active.id == response.json()["version_id"]
        question = db.get(JudgeQuestion, world.question_id)
        assert question.current_grading_version_id == active.id

        recorder = _RecordingClient()
        rubric_service.ensure_locked_rubrics_for_publish(db, recorder, [question])
        assert recorder.calls == 0, "启用后已锁定且来源一致的 Rubric 不应重复生成"


# ── 验收 B：失败不会半提交 ───────────────────────────────────────


def test_enable_rolls_back_everything_when_activate_fails(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)
    with db_session_factory() as db:
        rubric_id = db.get(AiGradingSetupJob, job_id).draft_rubric_id

    def boom(*args, **kwargs):
        raise RuntimeError("注入 activate 失败")

    monkeypatch.setattr(versions, "activate_version", boom)
    with pytest.raises(RuntimeError):
        client.post(f"{API}/setup-jobs/{job_id}/enable", headers=headers, json={})

    with db_session_factory() as db:
        rubric = db.get(QuestionRubric, rubric_id)
        assert rubric.status == "draft", "失败后 rubric 不得是 locked"
        assert rubric.locked_at is None
        assert db.scalar(select(func.count()).select_from(JudgeGradingVersion).where(
            JudgeGradingVersion.judge_question_id == world.question_id)) == 0, \
            "失败后不得残留评分版本行"
        assert db.get(JudgeQuestion, world.question_id).current_grading_version_id is None
    assert _job(db_session_factory, job_id).stage == "draft_ready"


# ── 验收 B 对照组：正常路径三条断言全真 ──────────────────────────


def test_enable_normal_path_commits_all_stages(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    response = client.post(f"{API}/setup-jobs/{job_id}/enable", headers=headers, json={})
    assert response.status_code == 200, response.text

    with db_session_factory() as db:
        rubric = db.get(QuestionRubric, response.json()["rubric_id"])
        assert rubric.status == "locked" and rubric.locked_at is not None
        version = db.get(JudgeGradingVersion, response.json()["version_id"])
        assert version.status == "active"
        assert version.confirmed_at is not None
        assert db.get(JudgeQuestion, world.question_id).current_grading_version_id == version.id
        assert db.get(AiGradingSetupJob, job_id).stage == "enabled"


# ── 验收 C：重复点击幂等 ─────────────────────────────────────────


def test_five_clicks_create_exactly_one_job(client, db_session_factory, world, monkeypatch):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    ids = [_create_job(client, headers, world)["id"] for _ in range(5)]
    assert len(set(ids)) == 1, "重复点击必须返回同一个任务"
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AiGradingSetupJob)) == 1


def test_retry_resumes_failed_stage_and_keeps_draft(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch, fail_groups=True)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    failed = _job(db_session_factory, job_id)
    assert failed.stage == "failed"
    assert failed.failed_stage == "generating_test_groups"
    draft_rubric_id = failed.draft_rubric_id
    assert draft_rubric_id is not None, "失败也要保留已生成的草稿"
    with db_session_factory() as db:
        assert db.get(QuestionRubric, draft_rubric_id).status == "draft"

    # 恢复：重试从失败阶段续跑，不重新生成 Rubric
    _patch_ai(monkeypatch, fail_groups=False)
    response = client.post(f"{API}/setup-jobs/{job_id}/retry", headers=headers)
    assert response.status_code == 202, response.text
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    resumed = _job(db_session_factory, job_id)
    assert resumed.stage == "draft_ready"
    assert resumed.draft_rubric_id == draft_rubric_id
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionRubric).where(
            QuestionRubric.judge_question_id == world.question_id)) == 1, \
            "续跑不得重复生成 Rubric"
        assert resumed.draft_test_groups


# ── 启用前分值强校验 ────────────────────────────────────────────


def test_enable_rejects_algorithm_score_over_limit(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    """A=60（算法评分项合计 60 > 20）时 enable 必须 422，且不产生任何启用痕迹。"""
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    with db_session_factory() as db:
        rubric = db.get(QuestionRubric, db.get(AiGradingSetupJob, job_id).draft_rubric_id)
        broken = dict(rubric.rubric_json)
        broken["algorithm_criteria"] = [
            {"id": "A1", "name": "正确性", "points": 20},
            {"id": "A2", "name": "完整性", "points": 20},
            {"id": "A3", "name": "效率", "points": 20},
        ]
        rubric.rubric_json = broken
        db.commit()

    response = client.post(f"{API}/setup-jobs/{job_id}/enable", headers=headers, json={})
    assert response.status_code == 422, response.text

    with db_session_factory() as db:
        assert db.get(QuestionRubric, db.get(AiGradingSetupJob, job_id).draft_rubric_id).status == "draft"
        assert versions.active_version_for(
            db, question_kind="assignment", question_id=world.question_id) is None


def test_enable_rejects_functional_groups_over_limit(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    """F=70（> 60）时 enable 必须 422，对照组是 F=60 可用例正常启用。"""
    oversized = [
        TestGroup(id="F1", name="功能", dimension="F", max_score=60.0,
                  tests="def test_solve():\n    assert solve(1) == 1\n"),
        TestGroup(id="F2", name="功能2", dimension="F", max_score=10.0,
                  tests="def test_solve2():\n    assert solve(2) == 2\n"),
        TestGroup(id="R1", name="边界", dimension="R", max_score=10.0,
                  tests="def test_boundary():\n    assert solve(0) == 0\n"),
    ]
    _patch_ai(monkeypatch, groups=oversized)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    response = client.post(f"{API}/setup-jobs/{job_id}/enable", headers=headers, json={})
    assert response.status_code == 422, response.text
    assert "F" in [item["dimension"] for item in
                   response.json()["detail"]["fields"]["violations"]]


# ── 验收 E：试判不产生提交、不消耗 attempts ──────────────────────


def test_trial_run_creates_no_submission_and_consumes_no_attempts(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    # 另建一条真实提交作为对照样本：试判不得动它
    other_submission_id = make_submission(db_session_factory, attempt_count=3)
    before = _table_counts(db_session_factory)

    created = client.post(
        f"{API}/setup-jobs/{job_id}/trial-runs", headers=headers,
        json={"code": "def solve(n):\n    return n", "client_request_id": "trial-1"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    with db_session_factory() as db:
        setup.run_trial_run(db, redis_client, test_settings, run_id)

    assert _table_counts(db_session_factory) == before, "试判不得新增提交/评分行或改动 attempts"
    with db_session_factory() as db:
        assert db.get(Submission, other_submission_id).attempt_count == 3
        run = db.get(RubricTrialRun, run_id)
        assert run.status == "completed", run.error_message
        assert run.algorithm_score == 20.0
        assert run.quality_score == 10.0
        assert run.preview_score_100 == 30.0, "试判只预览 AI 维度（A+Q），不含确定性 F/R"
        assert run.ai_result["student_feedback"]["strengths"] == ["结构清晰"]

        detail = client.get(f"{API}/trial-runs/{run_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["includes_deterministic"] is False


def test_trial_run_client_request_id_is_idempotent(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    first = client.post(f"{API}/setup-jobs/{job_id}/trial-runs", headers=headers,
                        json={"code": "def solve():\n    return 1",
                              "client_request_id": "same-request"})
    second = client.post(f"{API}/setup-jobs/{job_id}/trial-runs", headers=headers,
                         json={"code": "def solve():\n    return 2",
                               "client_request_id": "same-request"})
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(RubricTrialRun)) == 1


def test_trial_run_requires_draft_rubric(client, db_session_factory, world):
    """没有草稿 Rubric 时不得试判（fail-closed，不产生空白评分）。"""
    with db_session_factory() as db:
        job = AiGradingSetupJob(
            question_kind="assignment", question_id=world.question_id, teacher_id=1,
            stage="queued", progress=5, active_key="assignment:pending",
        )
        db.add(job)
        db.commit()
        job_id = job.id
    headers = _headers(client)
    response = client.post(f"{API}/setup-jobs/{job_id}/trial-runs", headers=headers,
                           json={"code": "def solve():\n    return 1"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "INVALID_STATE"


# ── 分值上限校验（单元层，覆盖 API schema 无法构造的越界数据） ──────


def test_validate_enable_scores_flags_over_limit_dimensions():
    document = {
        "algorithm_criteria": [{"id": "A1", "points": 20}, {"id": "A2", "points": 20},
                               {"id": "A3", "points": 20}],
        "quality_criteria": [{"id": "Q1", "points": 30}],
    }
    payload = {"groups": [{"dimension": "F", "max_score": 60}, {"dimension": "R", "max_score": 10}]}

    violations = setup.validate_enable_scores(document, payload)
    assert [item["dimension"] for item in violations] == ["A", "Q"]
    assert violations[0]["value"] == 60.0 and violations[0]["max"] == 20.0

    # 对照组：合法配置不得产生任何违规
    legal_document = {"algorithm_criteria": [{"id": "A1", "points": 20}],
                      "quality_criteria": [{"id": "Q1", "points": 10}]}
    assert setup.validate_enable_scores(legal_document, payload) == []


# ── 关闭「同时生成测试用例」：只出评分标准草稿，enable 回落到题目已有测试组 ──


def test_setup_job_can_skip_test_group_generation(
    client, db_session_factory, test_settings, redis_client, world, monkeypatch
):
    _patch_ai(monkeypatch)
    headers = _headers(client)
    job_id = _create_job(client, headers, world, generate_test_groups=False)["id"]
    _run_job(db_session_factory, test_settings, redis_client, job_id)

    detail = client.get(f"{API}/setup-jobs/{job_id}", headers=headers).json()
    assert detail["stage"] == "draft_ready"
    assert detail["generate_test_groups"] is False
    assert detail["draft_test_groups"] == [], "关闭时不得调用测试组生成"
    assert detail["draft_rubric_status"] == "draft"

    # enable 回落到题目上已有的测试组（高级面板维护），仍然单事务生效
    response = client.post(f"{API}/setup-jobs/{job_id}/enable", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"

    # 对照组：默认开启时草稿测试组非空
    monkeypatch.setattr(setup, "generate_test_groups",
                        lambda client_, snapshot, settings_, **kw: SimpleNamespace(
                            test_groups=list(TEST_GROUPS), warnings=[], generation_id="gen-2"))
    second_job = _create_job(client, headers, world, question_id=world.question_id)["id"]
    _run_job(db_session_factory, test_settings, redis_client, second_job)
    second_detail = client.get(f"{API}/setup-jobs/{second_job}", headers=headers).json()
    assert len(second_detail["draft_test_groups"]) == 2
