"""一键 AI 配置编排（批次 2）：异步生成草稿 → 试判 → 保存并启用。

只做编排，不重写底层：Rubric 生成复用 ``rubric_service.generate_rubric``，
用例转换复用 ``grading_version_service.build_data_payload_from_generated_groups``，
启用复用 ``update_draft_rubric → lock_rubric → create_draft → confirm_version →
activate_version``，全部 ``commit=False`` 由本模块单事务收口。

安全边界：

* 生成阶段只产出草稿（QuestionRubric(status='draft') + 任务行上的 draft_test_groups），
  不锁标准、不建评分版本、不改题目配置；
* 试判（``RubricTrialRun``）只用草稿 Rubric 调 AI，**绝不**创建
  Submission/ExamAnswer/CodeGrade，也**绝不**消耗提交次数；
* 启用任一步骤异常 → 全事务回滚，不留半提交。
"""
from __future__ import annotations

import json
import logging
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from redis import Redis
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditEvent,
    ExamQuestion,
    JudgeGradingVersion,
    JudgeQuestion,
    QuestionRubric,
)
from app.models.ai_setup import AiGradingSetupJob, RubricTrialRun
from app.schemas.ai_grading import AIGradeResponse, RubricDocument, TestGroup
from app.services import grading_version_service as versions
from app.services import rubric_service
from app.services.ai_client import AIServiceError, DeepSeekClient
from app.services.ai_prompts import (
    build_grading_messages,
    build_test_group_snapshot,
)
from app.services.ai_score_validation import validate_ai_output
from app.services.grading_version_service import (
    MAX_AI_ALGORITHM_SCORE,
    MAX_AI_QUALITY_SCORE,
    MAX_FUNCTIONAL_SCORE,
    MAX_ROBUSTNESS_SCORE,
    GradingVersionError,
)
from app.services.op_metrics import ai_metrics_sink
from app.services.rubric_service import RubricGenerationError
from app.services.score_merger import merge_scores
from app.services.static_analysis import analyze_python
from app.services.test_group_generator import PreflightUnavailableError, TestGroupValidationError, generate_test_groups

logger = logging.getLogger("dai.ai_setup")

SETUP_LEASE_SECONDS = 900
JOB_STAGE_PROGRESS = {
    "queued": 5,
    "generating_rubric": 30,
    "generating_test_groups": 70,
    "draft_ready": 100,
    "enabled": 100,
    "failed": 0,
}
GENERATING_STAGES = ("generating_rubric", "generating_test_groups")


class SetupJobError(ValueError):
    """一键配置任务状态不合法（调用方转 4xx）。"""


def active_key_for(question_kind: str, question_id: int) -> str:
    return f"{question_kind}:{question_id}"


def _question(db: Session, question_kind: str, question_id: int):
    model = JudgeQuestion if question_kind == "assignment" else ExamQuestion
    return db.get(model, question_id)


def _snapshot_for(db: Session, job: AiGradingSetupJob) -> dict:
    question = _question(db, job.question_kind, job.question_id)
    if question is None:
        raise SetupJobError("题目不存在")
    return rubric_service.build_question_snapshot(
        title=getattr(question, "title", None) or getattr(question, "prompt", ""),
        description=getattr(question, "description", None) if hasattr(question, "description")
        else getattr(question, "prompt", None),
        function_name=getattr(question, "function_name", None) if hasattr(question, "function_name")
        else getattr(question, "prompt", None),
        teacher_constraints=job.requested_teacher_constraints or question.teacher_constraints,
        test_groups=question.test_groups,
        reference_solution=job.requested_reference_solution
        if job.requested_reference_solution is not None else question.reference_solution,
        is_exam=(job.question_kind == "exam"),
    )


# ── 任务生命周期 ────────────────────────────────────────────────


def create_or_get_job(
    db: Session,
    *,
    question_kind: str,
    question_id: int,
    teacher_id: int,
    reference_solution: str | None = None,
    teacher_constraints: dict | None = None,
    grading_mode: str | None = None,
    generate_test_groups: bool = True,
) -> tuple[AiGradingSetupJob, bool]:
    """幂等创建：同一 (question_kind, question_id) 的活跃任务唯一，重复点击返回既有任务。"""
    key = active_key_for(question_kind, question_id)
    existing = db.scalar(select(AiGradingSetupJob).where(AiGradingSetupJob.active_key == key))
    if existing is not None:
        expire_stale_job(db, existing)
        return existing, False

    job = AiGradingSetupJob(
        question_kind=question_kind,
        question_id=question_id,
        teacher_id=teacher_id,
        stage="queued",
        progress=JOB_STAGE_PROGRESS["queued"],
        active_key=key,
        requested_reference_solution=reference_solution,
        requested_teacher_constraints=dict(teacher_constraints or {}),
        requested_grading_mode=grading_mode,
        generate_test_groups=generate_test_groups,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # 并发重复点击：唯一键保证只有一个任务，另一个返回既有任务
        db.rollback()
        existing = db.scalar(select(AiGradingSetupJob).where(AiGradingSetupJob.active_key == key))
        if existing is None:
            raise
        return existing, False
    db.refresh(job)
    return job, True


def expire_stale_job(db: Session, job: AiGradingSetupJob) -> AiGradingSetupJob:
    """租约过期的生成中任务标记为失败，保留已生成草稿，由教师点重试续跑。"""
    if job.stage not in GENERATING_STAGES:
        return job
    now = datetime.now(timezone.utc)
    lease = job.lease_until
    if lease is not None and lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    if lease is not None and lease >= now:
        return job
    job.stage = "failed"
    job.failed_stage = job.failed_stage or ("generating_rubric" if job.draft_rubric_id is None
                                            else "generating_test_groups")
    job.error_code = "SETUP_JOB_INTERRUPTED"
    job.error_message = "生成任务中断（worker 未在租约内完成），已保留草稿，请重试"
    job.lease_token = None
    job.lease_until = None
    db.commit()
    db.refresh(job)
    return job


def enqueue_setup_job(db: Session, redis_client: Redis, job_id: int) -> bool:
    """推送唤醒消息；任务状态已在库中，Redis 只做唤醒（先持久化再推送）。"""
    db.commit()
    try:
        redis_client.rpush(
            _setup_queue_name(),
            json.dumps({"type": "ai_setup", "id": job_id, "attempt": 0}),
        )
    except Exception:
        logger.warning("Redis 推送失败，任务等待教师重试: setup_job=%s", job_id)
        return False
    return True


def retry_setup_job(db: Session, redis_client: Redis, job: AiGradingSetupJob) -> AiGradingSetupJob:
    """从失败阶段续跑：已生成的草稿一律保留（draft_rubric_id / draft_test_groups）。"""
    if job.stage == "enabled":
        raise SetupJobError("任务已启用，无需重试")
    resume = job.failed_stage or (job.stage if job.stage in GENERATING_STAGES else None)
    if resume not in GENERATING_STAGES:
        resume = "generating_rubric" if job.draft_rubric_id is None else "generating_test_groups"
    if resume == "generating_test_groups" and job.draft_rubric_id is None:
        resume = "generating_rubric"
    job.stage = resume
    job.failed_stage = None
    job.progress = JOB_STAGE_PROGRESS[resume]
    job.error_code = None
    job.error_message = None
    job.lease_token = None
    job.lease_until = None
    job.finished_at = None
    db.commit()
    db.refresh(job)
    enqueue_setup_job(db, redis_client, job.id)
    return job


def claim_setup_job(db: Session, job_id: int) -> str | None:
    """条件认领：仅 queued/生成中且租约空闲的任务可被认领，铸造新的租约令牌。"""
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    result = db.execute(
        update(AiGradingSetupJob)
        .execution_options(synchronize_session=False)
        .where(
            AiGradingSetupJob.id == job_id,
            AiGradingSetupJob.stage.in_(("queued",) + GENERATING_STAGES),
            or_(AiGradingSetupJob.lease_until.is_(None), AiGradingSetupJob.lease_until < now),
        )
        .values(
            lease_token=token,
            lease_until=now + timedelta(seconds=SETUP_LEASE_SECONDS),
            attempts=AiGradingSetupJob.attempts + 1,
            started_at=func.coalesce(AiGradingSetupJob.started_at, now),
            error_code=None,
            error_message=None,
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.expire_all()
    return token


def _update_job(db: Session, job_id: int, token: str, **values) -> bool:
    """条件更新（仍持有租约才生效）并提交；返回是否命中。

    执行的是 Core UPDATE（绕过 identity map），因此提交后必须 ``expire_all``，
    否则同一会话后续读到的仍是过期对象（sessionmaker 未开启 expire_on_commit）。
    """
    values = dict(values)
    renew_lease = "lease_token" not in values
    result = db.execute(
        update(AiGradingSetupJob)
        .execution_options(synchronize_session=False)
        .where(AiGradingSetupJob.id == job_id, AiGradingSetupJob.lease_token == token)
        .values(**values)
    )
    matched = result.rowcount == 1
    if matched and renew_lease:
        db.execute(
            update(AiGradingSetupJob)
            .execution_options(synchronize_session=False)
            .where(AiGradingSetupJob.id == job_id, AiGradingSetupJob.lease_token == token)
            .values(lease_until=datetime.now(timezone.utc)
                    + timedelta(seconds=SETUP_LEASE_SECONDS))
        )
    db.commit()
    db.expire_all()
    return matched


def _fresh_job(db: Session, job_id: int) -> AiGradingSetupJob | None:
    db.expire_all()
    return db.get(AiGradingSetupJob, job_id)


def _fail_job(db: Session, job_id: int, token: str, *, stage: str, code: str, message: str) -> None:
    _update_job(
        db, job_id, token,
        stage="failed", failed_stage=stage, progress=JOB_STAGE_PROGRESS["failed"],
        error_code=code, error_message=message[:1000],
        lease_token=None, lease_until=None, finished_at=datetime.now(timezone.utc),
    )


def run_setup_job(db: Session, redis_client: Redis, settings: Settings, job_id: int) -> None:
    """worker 入口：按阶段推进，任一步失败保留草稿并停在 failed。"""
    token = claim_setup_job(db, job_id)
    if token is None:
        return
    job = _fresh_job(db, job_id)
    if job is None:
        return

    if job.stage == "queued":
        if not _update_job(db, job_id, token, stage="generating_rubric",
                           progress=JOB_STAGE_PROGRESS["generating_rubric"]):
            return
        job = _fresh_job(db, job_id)

    if job.stage == "generating_rubric" and job.draft_rubric_id is None:
        try:
            client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
            rubric = rubric_service.generate_rubric(
                db, client, kind=job.question_kind, question_id=job.question_id,
                snapshot=_snapshot_for(db, job),
            )
        except RubricGenerationError as exc:
            db.rollback()
            _fail_job(db, job_id, token, stage="generating_rubric",
                      code="AI_GENERATION_INVALID", message=str(exc))
            return
        except AIServiceError as exc:
            db.rollback()
            _fail_job(db, job_id, token, stage="generating_rubric",
                      code=_ai_error_code(exc), message=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 任务失败必须落库并保留可重试状态
            db.rollback()
            logger.exception("一键配置 Rubric 生成失败: job=%s", job_id)
            _fail_job(db, job_id, token, stage="generating_rubric",
                      code="SETUP_JOB_FAILED", message=str(exc))
            return
        if not _update_job(db, job_id, token, draft_rubric_id=rubric.id,
                           stage="generating_test_groups",
                           progress=JOB_STAGE_PROGRESS["generating_test_groups"]):
            return
        job = _fresh_job(db, job_id)

    if not job.generate_test_groups and job.stage in GENERATING_STAGES:
        # 教师选择只生成评分标准：Rubric 草稿已完成，不再调用测试组生成，直接进入待确认启用
        _update_job(db, job_id, token, stage="draft_ready",
                    progress=JOB_STAGE_PROGRESS["draft_ready"],
                    lease_token=None, lease_until=None)
        return

    if job.stage == "generating_test_groups" and not (job.draft_test_groups or []):
        try:
            groups = _generate_test_groups(db, job, settings)
        except (TestGroupValidationError, PreflightUnavailableError) as exc:
            db.rollback()
            _fail_job(db, job_id, token, stage="generating_test_groups",
                      code="AI_TEST_GROUPS_INVALID", message=str(exc))
            return
        except AIServiceError as exc:
            db.rollback()
            _fail_job(db, job_id, token, stage="generating_test_groups",
                      code=_ai_error_code(exc), message=str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("一键配置测试组生成失败: job=%s", job_id)
            _fail_job(db, job_id, token, stage="generating_test_groups",
                      code="SETUP_JOB_FAILED", message=str(exc))
            return
        _update_job(db, job_id, token, draft_test_groups=groups,
                    stage="draft_ready", progress=JOB_STAGE_PROGRESS["draft_ready"],
                    lease_token=None, lease_until=None)
        return

    if job.stage in GENERATING_STAGES and (job.draft_test_groups or job.draft_rubric_id):
        # 续跑时前一阶段已完成：直接推进到 draft_ready，不重复生成
        _update_job(db, job_id, token, stage="draft_ready",
                    progress=JOB_STAGE_PROGRESS["draft_ready"],
                    lease_token=None, lease_until=None)


def _generate_test_groups(db: Session, job: AiGradingSetupJob, settings: Settings) -> list[dict]:
    question = _question(db, job.question_kind, job.question_id)
    if question is None:
        raise SetupJobError("题目不存在")
    snapshot = build_test_group_snapshot(
        title=getattr(question, "title", None) or getattr(question, "prompt", ""),
        description=getattr(question, "description", None) if hasattr(question, "description")
        else getattr(question, "prompt", None),
        function_name=getattr(question, "function_name", None),
        signature=getattr(question, "signature", None),
        starter_code=getattr(question, "starter_code", None),
        hidden_tests=getattr(question, "hidden_tests", None),
        reference_solution=job.requested_reference_solution
        if job.requested_reference_solution is not None else getattr(question, "reference_solution", None),
        teacher_constraints=job.requested_teacher_constraints or question.teacher_constraints,
    )
    client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
    with tempfile.TemporaryDirectory(prefix="dai-setup-gen-") as tmp:
        result = generate_test_groups(
            client, snapshot, settings, workdir=tmp,
            host_workdir=settings.judge_host_work_dir or None,
        )
    return [group.model_dump() if hasattr(group, "model_dump") else dict(group)
            for group in result.test_groups]


def _ai_error_code(exc: AIServiceError) -> str:
    if exc.code == "timeout":
        return "AI_GENERATION_TIMEOUT"
    if exc.code == "http_429":
        return "AI_RATE_LIMITED"
    return "AI_GENERATION_INVALID"


def _setup_queue_name() -> str:
    from app.config import get_settings

    return get_settings().ai_setup_queue_name


# ── 草稿试判 ────────────────────────────────────────────────────


def create_trial_run(
    db: Session, *, job: AiGradingSetupJob, teacher_id: int, code: str,
    client_request_id: str | None = None,
) -> tuple[RubricTrialRun, bool]:
    """同 client_request_id 重复提交返回同一次试判（幂等），不重复消耗 AI 调用。"""
    if job.draft_rubric_id is None:
        raise SetupJobError("尚未生成草稿评分标准，不能试判")
    if client_request_id:
        existing = db.scalar(
            select(RubricTrialRun).where(
                RubricTrialRun.job_id == job.id,
                RubricTrialRun.client_request_id == client_request_id,
            )
        )
        if existing is not None:
            return existing, False
    run = RubricTrialRun(
        job_id=job.id,
        question_kind=job.question_kind,
        question_id=job.question_id,
        teacher_id=teacher_id,
        draft_rubric_id=job.draft_rubric_id,
        code=code,
        client_request_id=client_request_id,
        status="queued",
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(RubricTrialRun).where(
                RubricTrialRun.job_id == job.id,
                RubricTrialRun.client_request_id == client_request_id,
            )
        )
        if existing is None:
            raise
        return existing, False
    db.refresh(run)
    return run, True


def enqueue_trial_run(db: Session, redis_client: Redis, run_id: int) -> bool:
    db.commit()
    try:
        redis_client.rpush(
            _setup_queue_name(),
            json.dumps({"type": "ai_trial", "id": run_id, "attempt": 0}),
        )
    except Exception:
        logger.warning("Redis 推送失败，试判等待重新发起: trial_run=%s", run_id)
        return False
    return True


def claim_trial_run(db: Session, run_id: int) -> str | None:
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    result = db.execute(
        update(RubricTrialRun)
        .execution_options(synchronize_session=False)
        .where(
            RubricTrialRun.id == run_id,
            RubricTrialRun.status.in_(("queued", "running")),
            or_(RubricTrialRun.lease_until.is_(None), RubricTrialRun.lease_until < now),
        )
        .values(
            status="running", lease_token=token, attempts=RubricTrialRun.attempts + 1,
            lease_until=now + timedelta(seconds=SETUP_LEASE_SECONDS),
            started_at=func.coalesce(RubricTrialRun.started_at, now),
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.expire_all()
    return token


def run_trial_run(db: Session, redis_client: Redis, settings: Settings, run_id: int) -> None:
    """用当前草稿规则试判一段样例代码：只写 rubric_trial_runs 一行。"""
    token = claim_trial_run(db, run_id)
    if token is None:
        return
    run = db.get(RubricTrialRun, run_id)
    if run is None:
        return
    rubric = db.get(QuestionRubric, run.draft_rubric_id) if run.draft_rubric_id else None
    question = _question(db, run.question_kind, run.question_id)
    if rubric is None or question is None:
        _fail_trial_run(db, run_id, token, "TRIAL_TARGET_MISSING", "草稿评分标准或题目不存在")
        return

    try:
        static = analyze_python(run.code)
        question_snapshot = {
            "title": getattr(question, "title", None) or getattr(question, "prompt", ""),
            "description": getattr(question, "description", None) if hasattr(question, "description")
            else getattr(question, "prompt", None),
            "function_name": getattr(question, "function_name", None),
            "teacher_constraints": dict(getattr(question, "teacher_constraints", {}) or {}),
        }
        # 试判不执行判题容器：确定性 F/R 结果不存在，绝不冒充完整成绩
        deterministic: dict[str, Any] = {}
        messages = build_grading_messages(
            dict(rubric.rubric_json or {}), question_snapshot, run.code,
            deterministic, static, rubric_version=rubric.version,
        )
        client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
        raw = client.chat_json(messages, operation="ai_grading")
        parsed = AIGradeResponse.model_validate(raw)
        errors = validate_ai_output(
            dict(rubric.rubric_json or {}), parsed.model_dump(),
            list(range(1, len(run.code.splitlines()) + 1)),
        )
        if errors:
            _fail_trial_run(db, run_id, token, "AI_TRIAL_INVALID", "；".join(errors[:5]))
            return
        algorithm = float(parsed.algorithm.dimension_score)
        quality = float(parsed.code_quality.dimension_score)
        preview = merge_scores(f=0.0, a=algorithm, r=0.0, q=quality, cap=None, exam_points=None)
        db.execute(
            update(RubricTrialRun)
            .execution_options(synchronize_session=False)
            .where(RubricTrialRun.id == run_id, RubricTrialRun.lease_token == token)
            .values(
                status="completed",
                algorithm_score=algorithm,
                quality_score=quality,
                preview_score_100=preview.final_score_100,
                ai_result=parsed.model_dump(),
                raw_response=json.dumps(raw, ensure_ascii=False),
                lease_token=None, lease_until=None,
                error_code=None, error_message=None,
                finished_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    except AIServiceError as exc:
        db.rollback()
        _fail_trial_run(db, run_id, token, _ai_error_code(exc), str(exc))
    except Exception as exc:  # noqa: BLE001 - 试判失败不影响草稿与正式评分
        db.rollback()
        logger.exception("草稿试判失败: trial_run=%s", run_id)
        _fail_trial_run(db, run_id, token, "TRIAL_RUN_FAILED", str(exc))


def _fail_trial_run(db: Session, run_id: int, token: str, code: str, message: str) -> None:
    db.execute(
        update(RubricTrialRun)
        .execution_options(synchronize_session=False)
        .where(RubricTrialRun.id == run_id, RubricTrialRun.lease_token == token)
        .values(status="failed", error_code=code, error_message=message[:1000],
                lease_token=None, lease_until=None, finished_at=datetime.now(timezone.utc))
    )
    db.commit()


# ── 保存并启用（单事务） ─────────────────────────────────────────


def validate_enable_scores(rubric_document: dict, payload: dict) -> list[dict]:
    """启用前强校验 F≤60 / A≤20 / R≤10 / Q≤10（常量全部来自 grading_version_service）。"""
    violations: list[dict] = []
    groups = list((payload or {}).get("groups") or [])
    f_total = sum(float(g.get("max_score", 0) or 0) for g in groups if g.get("dimension") == "F")
    r_total = sum(float(g.get("max_score", 0) or 0) for g in groups if g.get("dimension") == "R")
    a_total = sum(float(c.get("points", 0) or 0)
                  for c in (rubric_document or {}).get("algorithm_criteria") or [])
    q_total = sum(float(c.get("points", 0) or 0)
                  for c in (rubric_document or {}).get("quality_criteria") or [])

    def check(dimension: str, value: float, limit: float, label: str) -> None:
        if value > limit + 1e-6:
            violations.append({
                "dimension": dimension, "value": round(value, 4), "max": limit,
                "message": f"{label}合计 {round(value, 4)} 超过上限 {limit}",
            })

    check("F", f_total, MAX_FUNCTIONAL_SCORE, "功能分 F")
    check("R", r_total, MAX_ROBUSTNESS_SCORE, "边界分 R")
    check("A", a_total, MAX_AI_ALGORITHM_SCORE, "AI 算法分 A")
    check("Q", q_total, MAX_AI_QUALITY_SCORE, "AI 代码质量分 Q")
    return violations


def _resolve_rubric_document(
    db: Session, job: AiGradingSetupJob, document: RubricDocument | dict | None,
) -> RubricDocument:
    if document is None:
        rubric = db.get(QuestionRubric, job.draft_rubric_id) if job.draft_rubric_id else None
        if rubric is None:
            raise SetupJobError("草稿评分标准不存在，请先完成生成")
        document = rubric.rubric_json or {}
    if isinstance(document, RubricDocument):
        return document
    return RubricDocument.model_validate(document)


def _resolve_groups(
    db: Session, job: AiGradingSetupJob, groups: list | None,
) -> list[dict]:
    raw_groups = groups if groups is not None else (job.draft_test_groups or [])
    if not raw_groups:
        # 关闭了自动生成测试组的任务：回落到题目上已有的测试组（高级面板维护）
        question = _question(db, job.question_kind, job.question_id)
        raw_groups = list(getattr(question, "test_groups", []) or []) if question else []
    if not raw_groups:
        raise SetupJobError("没有可用的测试组，请先生成测试组或手工配置")
    resolved = []
    for group in raw_groups:
        if isinstance(group, TestGroup):
            resolved.append(group.model_dump())
        elif isinstance(group, dict):
            resolved.append(TestGroup.model_validate(group).model_dump())
        else:
            raise SetupJobError("测试组格式不合法")
    return resolved


def enable_setup_job(
    db: Session,
    *,
    job: AiGradingSetupJob,
    actor,
    rubric: RubricDocument | dict | None = None,
    test_groups: list | None = None,
    teacher_constraints: dict | None = None,
    score_cap_rules: list | None = None,
    compare_rules: dict | None = None,
    entry_function: str | None = None,
    acknowledge_in_flight: bool = False,
) -> dict:
    """保存并启用：patch_rubric → lock_rubric → create_draft → confirm → activate 单事务。

    任一步骤抛错都不提交，API 会话关闭时整体回滚——不会出现「rubric 已锁但版本没启用」
    或「版本已启用但题目仍指向旧版本」的半提交状态。
    """
    if job.stage == "enabled":
        raise SetupJobError("任务已启用")
    if job.stage != "draft_ready":
        raise SetupJobError("草稿尚未生成完成，不能启用")

    question = _question(db, job.question_kind, job.question_id)
    if question is None:
        raise SetupJobError("题目不存在")

    document = _resolve_rubric_document(db, job, rubric)
    group_dicts = _resolve_groups(db, job, test_groups)
    entry = entry_function or getattr(question, "function_name", None)
    if not isinstance(entry, str) or not entry.isidentifier():
        raise SetupJobError("入口函数名不合法，无法生成评分版本")

    grading_mode = job.requested_grading_mode or getattr(question, "grading_mode", "legacy") or "legacy"
    if job.question_kind == "exam" and getattr(question, "question_type", "code") != "code" \
            and grading_mode != "legacy":
        raise SetupJobError("选择题只支持 legacy 模式")

    # 分值与在途门禁：先配置错误（422），再状态冲突（409）。
    # 分值上限必须用原始测试组直接判，转换器对 F/R 不等 60/10 的报错是 409 语义，
    # 而 F>60 属配置越界，必须给出 422 与逐维度明细。
    violations = validate_enable_scores(document.model_dump(), {"groups": group_dicts})
    if violations:
        raise EnableScoreError(violations)
    try:
        payload, _report = versions.build_data_payload_from_generated_groups(
            group_dicts, entry_function=entry,
        )
    except GradingVersionError as exc:
        raise SetupJobError(f"测试组无法转换为可信用例：{exc}") from exc

    versions.assert_enablement_allowed(
        db, question_kind=job.question_kind, question_id=job.question_id,
        acknowledge_in_flight=acknowledge_in_flight,
    )

    rubric_row = db.get(QuestionRubric, job.draft_rubric_id)
    if rubric_row is None:
        raise SetupJobError("草稿评分标准不存在，请先完成生成")

    # 教师填写的参考解法与评分侧重属于本次保存内容，与启用在同一事务落库
    if teacher_constraints is not None:
        question.teacher_constraints = dict(teacher_constraints)
    if job.requested_reference_solution is not None:
        question.reference_solution = job.requested_reference_solution
    question.test_groups = group_dicts
    if score_cap_rules is not None:
        question.score_cap_rules = list(score_cap_rules)
    if job.requested_grading_mode is not None:
        question.grading_mode = grading_mode
    db.flush()

    rubric_service.update_draft_rubric(db, rubric_row.id, document, commit=False)
    rubric_service.lock_rubric(db, rubric_row.id, commit=False)
    version = versions.create_draft(
        db,
        question_kind=job.question_kind,
        question_id=job.question_id,
        payload=payload,
        entry_function=entry,
        compare_rules=compare_rules,
        score_cap_rules=list(score_cap_rules if score_cap_rules is not None
                             else (question.score_cap_rules or [])),
        context_snapshot={"title": getattr(question, "title", None) or getattr(question, "prompt", ""),
                          "source": "ai_setup"},
        rubric_id=rubric_row.id,
        source_kind="ai_draft",
        grading_mode_snapshot=grading_mode,
        actor_id=actor.id,
        commit=False,
    )
    versions.confirm_version(db, version, actor_id=actor.id, confirm_isolated_case_v2=True)
    versions.activate_version(db, version, actor_id=actor.id)
    versions.set_question_current_version(
        db, question_kind=job.question_kind, question_id=job.question_id,
        version_id=version.id,
    )

    job.stage = "enabled"
    job.progress = JOB_STAGE_PROGRESS["enabled"]
    job.active_key = None
    job.lease_token = None
    job.lease_until = None
    job.error_code = None
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)

    db.add(AuditEvent(
        actor_id=actor.id,
        action="ai_setup.enable",
        target_type=f"{job.question_kind}_question",
        target_id=str(job.question_id),
        reason="一键配置保存并启用",
        changes={"job_id": job.id, "rubric_id": rubric_row.id, "version_id": version.id,
                 "version": version.version, "grading_mode": grading_mode},
    ))
    db.commit()
    db.refresh(job)
    db.refresh(version)
    return {
        "ok": True,
        "job_id": job.id,
        "rubric_id": rubric_row.id,
        "version_id": version.id,
        "version": version.version,
        "status": version.status,
        "grading_mode": grading_mode,
        "warnings": [],
    }


class EnableScoreError(SetupJobError):
    """启用前分值校验未通过：携带逐维度违规明细（API 转 422）。"""

    def __init__(self, violations: list[dict]):
        super().__init__("分值超过上限，不能启用")
        self.violations = violations
