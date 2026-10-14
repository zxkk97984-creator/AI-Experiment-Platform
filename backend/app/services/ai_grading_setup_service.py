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
import hashlib
import logging
import secrets
import threading
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from redis import Redis
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.models import (
    Assignment,
    AuditEvent,
    ExamQuestion,
    JudgeGradingVersion,
    JudgeQuestion,
    QuestionRubric,
)
from app.models.ai_setup import AiGradingSetupJob, RubricTrialRun
from app.schemas.ai_grading import AIGradeResponse, RubricDocument, ScoreCapRule, TestGroup
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


class DraftRevisionConflict(SetupJobError):
    """The local draft changed since the editor loaded it."""


def active_key_for(question_kind: str, question_id: int) -> str:
    return f"{question_kind}:{question_id}"


def _question(db: Session, question_kind: str, question_id: int):
    if question_kind == "practice_problem":
        # 自主练习（批次 5）：平台级公共题库，管理员复用本流水线配置 AI 评分
        from app.models.practice import PracticeProblem

        return db.get(PracticeProblem, question_id)
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
    job = db.get(AiGradingSetupJob, job_id)
    if job is not None and job.stage == "queued":
        job.wake_published_at = datetime.now(timezone.utc)
        db.commit()
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
    job.stage = "queued"
    job.resume_stage = resume
    job.failed_stage = None
    job.progress = JOB_STAGE_PROGRESS["queued"]
    job.error_code = None
    job.error_message = None
    job.lease_token = None
    job.lease_until = None
    job.wake_published_at = None
    job.finished_at = None
    db.commit()
    db.refresh(job)
    enqueue_setup_job(db, redis_client, job.id)
    return job


def recover_durable_setup_work(
    db: Session, redis_client: Redis, *, deterministic_kinds: set[str] | None = None,
    include_ai: bool = True, limit: int = 100,
) -> dict:
    """Fail expired leases and re-wake queued DB work without auto-retrying AI calls."""
    now = datetime.now(timezone.utc)
    stale_jobs = list(db.scalars(select(AiGradingSetupJob).where(
        AiGradingSetupJob.stage.in_(GENERATING_STAGES),
        or_(AiGradingSetupJob.lease_until.is_(None), AiGradingSetupJob.lease_until < now),
    ).order_by(AiGradingSetupJob.id).limit(limit)).all())
    for job in stale_jobs:
        failed_stage = job.stage
        db.execute(update(AiGradingSetupJob).where(
            AiGradingSetupJob.id == job.id,
            AiGradingSetupJob.stage == failed_stage,
            AiGradingSetupJob.lease_token == job.lease_token,
            or_(AiGradingSetupJob.lease_until.is_(None), AiGradingSetupJob.lease_until < now),
        ).execution_options(synchronize_session=False).values(stage="failed", failed_stage=failed_stage,
                 progress=JOB_STAGE_PROGRESS["failed"], error_code="SETUP_JOB_INTERRUPTED",
                 error_message="生成任务中断（worker 租约过期），草稿已保留，请手动重试",
                 lease_token=None, lease_until=None, finished_at=now))
    stale_trials = list(db.scalars(select(RubricTrialRun).where(
        RubricTrialRun.stage.in_(("deterministic_running", "ai_running")),
        or_(RubricTrialRun.lease_until.is_(None), RubricTrialRun.lease_until < now),
    ).order_by(RubricTrialRun.id).limit(limit)).all())
    for run in stale_trials:
        db.execute(update(RubricTrialRun).where(
            RubricTrialRun.id == run.id, RubricTrialRun.stage == run.stage,
            RubricTrialRun.lease_token == run.lease_token,
            or_(RubricTrialRun.lease_until.is_(None), RubricTrialRun.lease_until < now),
        ).execution_options(synchronize_session=False).values(
                 status="failed", stage="failed", error_code="TRIAL_RUN_INTERRUPTED",
                 error_message="试判 Worker 中断，本次阶段未完成，请重新发起试判",
                 lease_token=None, lease_until=None, total_score=None, complete=False,
                 finished_at=now))
    db.commit()

    wake_before = now - timedelta(seconds=60)
    stats = {"setup_jobs": 0, "deterministic_trials": 0, "ai_trials": 0,
             "expired_setup_jobs": len(stale_jobs), "expired_trials": len(stale_trials)}
    if include_ai:
        job_ids = list(db.scalars(select(AiGradingSetupJob.id).where(
            AiGradingSetupJob.stage == "queued",
            or_(AiGradingSetupJob.wake_published_at.is_(None),
                AiGradingSetupJob.wake_published_at < wake_before),
        ).order_by(AiGradingSetupJob.id).limit(limit)).all())
        for ident in job_ids:
            if enqueue_setup_job(db, redis_client, ident):
                stats["setup_jobs"] += 1
        ai_ids = list(db.scalars(select(RubricTrialRun.id).where(
            RubricTrialRun.stage == "ai_queued",
            or_(RubricTrialRun.wake_published_at.is_(None),
                RubricTrialRun.wake_published_at < wake_before),
        ).order_by(RubricTrialRun.id).limit(limit)).all())
        for ident in ai_ids:
            if enqueue_trial_ai_run(db, redis_client, ident):
                stats["ai_trials"] += 1
    if deterministic_kinds:
        deterministic_runs = db.execute(select(
            RubricTrialRun.id, RubricTrialRun.question_kind
        ).where(
            RubricTrialRun.stage == "deterministic_queued",
            RubricTrialRun.question_kind.in_(sorted(deterministic_kinds)),
            or_(RubricTrialRun.wake_published_at.is_(None),
                RubricTrialRun.wake_published_at < wake_before),
        ).order_by(RubricTrialRun.id).limit(limit)).all()
        for ident, _kind in deterministic_runs:
            if enqueue_trial_run(db, redis_client, ident):
                stats["deterministic_trials"] += 1
    return stats


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
    expected_stage = values.pop("_expected_stage", None)
    renew_lease = "lease_token" not in values
    lease_conditions = [AiGradingSetupJob.id == job_id, AiGradingSetupJob.lease_token == token]
    conditions = list(lease_conditions)
    if expected_stage is not None:
        conditions.append(AiGradingSetupJob.stage == expected_stage)
    result = db.execute(
        update(AiGradingSetupJob)
        .execution_options(synchronize_session=False)
        .where(*conditions)
        .values(**values)
    )
    matched = result.rowcount == 1
    if matched and renew_lease:
        db.execute(
            update(AiGradingSetupJob)
            .execution_options(synchronize_session=False)
            .where(*lease_conditions)
            .values(lease_until=datetime.now(timezone.utc)
                    + timedelta(seconds=SETUP_LEASE_SECONDS))
        )
    db.commit()
    db.expire_all()
    return matched


def _fresh_job(db: Session, job_id: int) -> AiGradingSetupJob | None:
    db.expire_all()
    return db.get(AiGradingSetupJob, job_id)


@contextmanager
def _setup_lease_heartbeat(db: Session, job_id: int, token: str):
    """Keep long model/preflight calls leased without sharing a SQLAlchemy session."""
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    stop = threading.Event()
    lost = threading.Event()

    def pulse() -> None:
        while not stop.wait(30):
            try:
                with factory() as session:
                    now = datetime.now(timezone.utc)
                    result = session.execute(update(AiGradingSetupJob).where(
                        AiGradingSetupJob.id == job_id,
                        AiGradingSetupJob.lease_token == token,
                        AiGradingSetupJob.stage.in_(GENERATING_STAGES),
                    ).values(lease_until=now + timedelta(seconds=SETUP_LEASE_SECONDS)))
                    session.commit()
                    if result.rowcount != 1:
                        lost.set()
                        return
            except Exception:
                logger.exception("AI 草稿任务租约心跳失败: setup_job=%s", job_id)
                lost.set()
                return

    thread = threading.Thread(target=pulse, name=f"setup-lease-{job_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
    if lost.is_set():
        raise SetupJobError("配置任务租约已失效，旧 Worker 结果已丢弃")


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
        resume = job.resume_stage or ("generating_rubric" if job.draft_rubric_id is None
                                      else "generating_test_groups")
        if resume not in GENERATING_STAGES:
            resume = "generating_rubric" if job.draft_rubric_id is None else "generating_test_groups"
        if not _update_job(db, job_id, token, _expected_stage="queued", stage=resume,
                           resume_stage=None, progress=JOB_STAGE_PROGRESS[resume]):
            return
        job = _fresh_job(db, job_id)

    if job.stage == "generating_rubric" and job.draft_rubric_id is None:
        try:
            with _setup_lease_heartbeat(db, job_id, token):
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
        if not _update_job(db, job_id, token, _expected_stage="generating_rubric",
                           draft_rubric_id=rubric.id,
                           stage="generating_test_groups",
                           progress=JOB_STAGE_PROGRESS["generating_test_groups"]):
            return
        job = _fresh_job(db, job_id)

    if not job.generate_test_groups and job.stage in GENERATING_STAGES:
        # 教师选择只生成评分标准：Rubric 草稿已完成，不再调用测试组生成，直接进入待确认启用
        defaults = _draft_default_values(db, job)
        _update_job(db, job_id, token, _expected_stage=job.stage,
                    **defaults, draft_revision=job.draft_revision or 1, stage="draft_ready",
                    progress=JOB_STAGE_PROGRESS["draft_ready"],
                    lease_token=None, lease_until=None)
        return

    if job.stage == "generating_test_groups" and not (job.draft_test_groups or []):
        try:
            with _setup_lease_heartbeat(db, job_id, token):
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
        defaults = _draft_default_values(db, job)
        _update_job(db, job_id, token, _expected_stage="generating_test_groups",
                    draft_test_groups=groups, **defaults,
                    draft_revision=job.draft_revision or 1,
                    stage="draft_ready", progress=JOB_STAGE_PROGRESS["draft_ready"],
                    lease_token=None, lease_until=None)
        return

    if job.stage in GENERATING_STAGES and (job.draft_test_groups or job.draft_rubric_id):
        # 续跑时前一阶段已完成：直接推进到 draft_ready，不重复生成
        defaults = _draft_default_values(db, job)
        _update_job(db, job_id, token, _expected_stage=job.stage,
                    **defaults, draft_revision=job.draft_revision or 1, stage="draft_ready",
                    progress=JOB_STAGE_PROGRESS["draft_ready"],
                    lease_token=None, lease_until=None)


def _draft_default_values(db: Session, job: AiGradingSetupJob) -> dict:
    """Freeze current editable defaults when the first generated draft becomes ready."""
    question = _question(db, job.question_kind, job.question_id)
    if question is None:
        return {}
    active = versions.active_version_for(
        db, question_kind=job.question_kind, question_id=job.question_id)
    return {
        "draft_entry_function": (job.draft_entry_function or getattr(question, "function_name", None)),
        "draft_teacher_constraints": (job.draft_teacher_constraints
                                      if job.draft_teacher_constraints is not None
                                      else dict(job.requested_teacher_constraints
                                                or getattr(question, "teacher_constraints", {}) or {})),
        "draft_compare_rules": (job.draft_compare_rules if job.draft_compare_rules is not None
                                else dict(active.compare_rules or {}) if active is not None else {}),
        "draft_score_cap_rules": (job.draft_score_cap_rules if job.draft_score_cap_rules is not None
                                  else list(active.score_cap_rules or []) if active is not None
                                  else list(getattr(question, "score_cap_rules", []) or [])),
    }


def save_setup_draft(
    db: Session, *, job_id: int, expected_revision: int, rubric: dict,
    test_groups: list, entry_function: str, teacher_constraints: dict,
    compare_rules: dict, score_cap_rules: list,
) -> AiGradingSetupJob:
    """CAS-save an editable draft without changing any active scoring version."""
    job = db.scalar(select(AiGradingSetupJob).where(AiGradingSetupJob.id == job_id)
                    .with_for_update().execution_options(populate_existing=True))
    if job is None:
        raise SetupJobError("配置任务不存在")
    if job.stage != "draft_ready":
        raise SetupJobError("只有待确认的草稿可以修改")
    if int(job.draft_revision or 0) != int(expected_revision):
        raise DraftRevisionConflict(
            f"草稿已更新（当前修订号 {job.draft_revision}），请刷新后合并本地修改")
    rubric_row = db.get(QuestionRubric, job.draft_rubric_id) if job.draft_rubric_id else None
    if rubric_row is None or rubric_row.status != "draft":
        raise SetupJobError("草稿评分标准不存在或已锁定")
    document = RubricDocument.model_validate(rubric)
    groups = [TestGroup.model_validate(group).model_dump() for group in test_groups]
    if len({group["id"] for group in groups}) != len(groups):
        raise SetupJobError("F/R 测试组标识不能重复")
    f_total = sum(group["max_score"] for group in groups if group["dimension"] == "F")
    r_total = sum(group["max_score"] for group in groups if group["dimension"] == "R")
    if f_total > MAX_FUNCTIONAL_SCORE + 1e-6 or r_total > MAX_ROBUSTNESS_SCORE + 1e-6:
        raise SetupJobError("F/R 测试组满分分别不能超过 60/10")
    if not isinstance(entry_function, str) or not entry_function.isidentifier():
        raise SetupJobError("入口函数名必须是有效 Python 标识符")
    if not isinstance(teacher_constraints, dict) or not isinstance(compare_rules, dict):
        raise SetupJobError("教师约束和比较规则必须是对象")
    for name, rule in compare_rules.items():
        if name not in ("default", "float") or not isinstance(rule, dict) \
                or rule.get("kind") not in ("literal", "identity", "float", "array", "exception"):
            raise SetupJobError("比较规则不符合现有评分版本格式")
    normalized_caps = [ScoreCapRule.model_validate(item).model_dump() for item in score_cap_rules]
    if len({item["id"] for item in normalized_caps}) != len(normalized_caps):
        raise SetupJobError("封顶规则标识不能重复")
    try:
        serialized = json.dumps({
            "rubric": document.model_dump(), "test_groups": groups,
            "entry_function": entry_function, "teacher_constraints": teacher_constraints,
            "compare_rules": compare_rules, "score_cap_rules": normalized_caps,
        }, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise SetupJobError("草稿配置必须是有限、可序列化的 JSON") from exc
    if len(serialized.encode("utf-8")) > 200_000:
        raise SetupJobError("草稿配置超过 200KB")

    rubric_service.update_draft_rubric(db, rubric_row.id, document, commit=False)
    job.draft_test_groups = groups
    job.draft_entry_function = entry_function
    job.draft_teacher_constraints = dict(teacher_constraints)
    job.draft_compare_rules = dict(compare_rules)
    job.draft_score_cap_rules = normalized_caps
    job.draft_revision = int(job.draft_revision or 0) + 1
    db.commit()
    db.refresh(job)
    return job


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
    client_request_id: str | None = None, expected_revision: int | None = None,
    settings: Settings | None = None,
) -> tuple[RubricTrialRun, bool]:
    """Create a full trial with immutable candidate snapshot and revision-aware idempotency."""
    if job.draft_rubric_id is None:
        raise SetupJobError("尚未生成草稿评分标准，不能试判")
    if job.stage != "draft_ready":
        raise SetupJobError("配置草稿尚未就绪")
    revision = int(job.draft_revision or 0)
    if expected_revision is not None and int(expected_revision) != revision:
        raise DraftRevisionConflict(f"草稿已更新（当前修订号 {revision}），请先保存或刷新")
    settings = settings or get_settings()
    candidate, digest = build_trial_candidate(db, job, settings)
    if client_request_id:
        existing = db.scalar(
            select(RubricTrialRun).where(
                RubricTrialRun.job_id == job.id,
                RubricTrialRun.client_request_id == client_request_id,
            )
        )
        if existing is not None:
            if existing.code != code or existing.candidate_digest != digest:
                raise SetupJobError("该请求标识已用于不同代码或候选配置")
            return existing, False
    run = RubricTrialRun(
        job_id=job.id,
        question_kind=job.question_kind,
        question_id=job.question_id,
        teacher_id=teacher_id,
        draft_rubric_id=job.draft_rubric_id,
        draft_revision=revision,
        candidate_snapshot=candidate,
        candidate_digest=digest,
        code=code,
        client_request_id=client_request_id,
        status="queued",
        trial_type="full",
        stage="deterministic_queued",
        wake_published_at=None,
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
        if existing.code != code or existing.candidate_digest != digest:
            raise SetupJobError("该请求标识已用于不同代码或候选配置")
        return existing, False
    db.refresh(run)
    return run, True


def enqueue_trial_run(db: Session, redis_client: Redis, run_id: int) -> bool:
    run = db.get(RubricTrialRun, run_id)
    if run is None or run.stage != "deterministic_queued":
        return False
    settings = get_settings()
    if run.question_kind == "practice_problem":
        queue_name = settings.practice_judge_queue_name
    elif run.question_kind == "exam":
        queue_name = "judge:exam:queue"
    else:
        queue_name = settings.judge_queue_name
    try:
        redis_client.rpush(queue_name, json.dumps({"type": "trial_deterministic", "id": run_id,
                                                   "attempt": 0}))
    except Exception:
        logger.warning("Redis 推送失败，完整试判等待持久扫描: trial_run=%s", run_id)
        return False
    run.wake_published_at = datetime.now(timezone.utc)
    db.commit()
    return True


def enqueue_trial_ai_run(db: Session, redis_client: Redis, run_id: int) -> bool:
    run = db.get(RubricTrialRun, run_id)
    if run is None or run.stage != "ai_queued":
        return False
    try:
        redis_client.rpush(_setup_queue_name(), json.dumps({
            "type": "ai_trial", "id": run_id, "attempt": 0,
        }))
    except Exception:
        logger.warning("Redis 推送失败，AI 试判阶段等待持久扫描: trial_run=%s", run_id)
        return False
    run.wake_published_at = datetime.now(timezone.utc)
    db.commit()
    return True


def build_trial_candidate(db: Session, job: AiGradingSetupJob, settings: Settings) -> tuple[dict, str]:
    """Freeze the rubric, F/R groups, context, caps and execution environment."""
    question = _question(db, job.question_kind, job.question_id)
    rubric = db.get(QuestionRubric, job.draft_rubric_id) if job.draft_rubric_id else None
    if question is None or rubric is None:
        raise SetupJobError("草稿题目或评分标准不存在")
    active = versions.active_version_for(
        db, question_kind=job.question_kind, question_id=job.question_id)
    groups = _resolve_groups(db, job, None)
    f_total = sum(float(group["max_score"]) for group in groups if group["dimension"] == "F")
    r_total = sum(float(group["max_score"]) for group in groups if group["dimension"] == "R")
    if abs(f_total - MAX_FUNCTIONAL_SCORE) > 1e-6 or abs(r_total - MAX_ROBUSTNESS_SCORE) > 1e-6:
        raise SetupJobError("完整试判需要 F=60、R=10 的有效测试组配置")
    entry = job.draft_entry_function or getattr(question, "function_name", None)
    if not isinstance(entry, str) or not entry.isidentifier():
        raise SetupJobError("入口函数名不合法")
    environment_version_id = getattr(question, "environment_version_id", None)
    if job.question_kind == "assignment" and not environment_version_id:
        assignment = db.get(Assignment, question.assignment_id)
        environment_version_id = assignment.environment_version_id if assignment else None
    image_ref = settings.judge_image
    if environment_version_id:
        from app.services.environment_service import resolve_run_image_ref

        image_ref = resolve_run_image_ref(db, environment_version_id)
    context = {
        "title": getattr(question, "title", None) or getattr(question, "prompt", ""),
        "description": (getattr(question, "description", None)
                        or getattr(question, "prompt", None)
                        or getattr(question, "title", "")),
        "function_name": entry,
        "teacher_constraints": dict(
            job.draft_teacher_constraints if job.draft_teacher_constraints is not None
            else job.requested_teacher_constraints if job.requested_teacher_constraints is not None
            else getattr(question, "teacher_constraints", {}) or {}),
        "reference_solution": (job.requested_reference_solution
                               if job.requested_reference_solution is not None
                               else getattr(question, "reference_solution", None)),
    }
    candidate = {
        "draft_revision": int(job.draft_revision or 0),
        "rubric_id": rubric.id,
        "rubric_version": rubric.version,
        "rubric": dict(rubric.rubric_json or {}),
        "test_groups": groups,
        "context": context,
        "entry_function": entry,
        "teacher_constraints": context["teacher_constraints"],
        "compare_rules": dict(job.draft_compare_rules if job.draft_compare_rules is not None
                               else active.compare_rules or {} if active is not None else {}),
        "score_cap_rules": list(job.draft_score_cap_rules
                                 if job.draft_score_cap_rules is not None
                                 else active.score_cap_rules or [] if active is not None
                                 else getattr(question, "score_cap_rules", []) or []),
        "environment_version_id": environment_version_id,
        "image_ref": image_ref,
        "timeout_seconds": max(1, min(
            int(getattr(question, "time_limit_ms", 10000) or 10000) // 1000 or 1,
            int(settings.judge_timeout_seconds))),
        "memory_limit_mb": max(64, int(getattr(question, "memory_limit_mb", None)
                                       or settings.judge_memory_limit_mb)),
        "exam_points": (float(question.points) if job.question_kind == "exam" else None),
    }
    if candidate["exam_points"] is not None and candidate["exam_points"] <= 0:
        raise SetupJobError("考试题满分必须大于 0 才能进行完整试判")
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SetupJobError("试判候选配置不可序列化") from exc
    if len(encoded.encode("utf-8")) > 300_000:
        raise SetupJobError("试判候选配置超过 300KB")
    return candidate, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def claim_trial_run(db: Session, run_id: int, *, expected_stage: str = "ai_queued",
                    running_stage: str = "ai_running") -> str | None:
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    result = db.execute(
        update(RubricTrialRun)
        .execution_options(synchronize_session=False)
        .where(
            RubricTrialRun.id == run_id,
            RubricTrialRun.stage == expected_stage,
            RubricTrialRun.status == "queued",
            or_(RubricTrialRun.lease_until.is_(None), RubricTrialRun.lease_until < now),
        )
        .values(
            status="running", stage=running_stage, lease_token=token,
            attempts=RubricTrialRun.attempts + 1,
            lease_until=now + timedelta(seconds=SETUP_LEASE_SECONDS),
            started_at=func.coalesce(RubricTrialRun.started_at, now),
            wake_published_at=None,
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.expire_all()
    return token


@contextmanager
def _trial_lease_heartbeat(db: Session, run_id: int, token: str):
    """Renew a running trial lease in a separate session during long Docker/AI work."""
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    stop = threading.Event()
    lost = threading.Event()

    def renew_once(session: Session) -> bool:
        now = datetime.now(timezone.utc)
        result = session.execute(update(RubricTrialRun)
            .execution_options(synchronize_session=False).where(
            RubricTrialRun.id == run_id,
            RubricTrialRun.lease_token == token,
            RubricTrialRun.stage.in_(("deterministic_running", "ai_running")),
            RubricTrialRun.status == "running",
            RubricTrialRun.lease_until > now,
            ).values(lease_until=now + timedelta(seconds=SETUP_LEASE_SECONDS)))
        session.commit()
        return result.rowcount == 1

    def pulse() -> None:
        while not stop.wait(30):
            try:
                with factory() as session:
                    if not renew_once(session):
                        lost.set()
                        return
            except Exception:
                logger.exception("试判租约心跳失败: trial_run=%s", run_id)
                lost.set()
                return

    thread = threading.Thread(target=pulse, name=f"trial-lease-{run_id}", daemon=True)
    thread.start()
    def heartbeat_now() -> None:
        _raise_if_trial_lease_lost(lost)
        if not renew_once(db):
            lost.set()
            raise SetupJobError("试判租约已失效，旧 Worker 结果已丢弃")

    try:
        yield heartbeat_now
    finally:
        stop.set()
        thread.join(timeout=2)
    _raise_if_trial_lease_lost(lost)


def _raise_if_trial_lease_lost(lost: threading.Event) -> None:
    if lost.is_set():
        raise SetupJobError("试判租约已失效，旧 Worker 结果已丢弃")


def run_trial_deterministic(db: Session, redis_client: Redis, settings: Settings,
                            run_id: int) -> None:
    """Execute frozen F/R pytest groups with the same isolated runner as formal judging."""
    token = claim_trial_run(db, run_id, expected_stage="deterministic_queued",
                            running_stage="deterministic_running")
    if token is None:
        return
    run = db.get(RubricTrialRun, run_id)
    if run is None or not isinstance(run.candidate_snapshot, dict):
        if run is not None:
            _fail_trial_run(db, run_id, token, "TRIAL_CANDIDATE_MISSING", "试判缺少冻结候选配置")
        return
    try:
        candidate = dict(run.candidate_snapshot)
        from app.worker.judge_worker import (
            _calc_fr_scores, _make_work_dir, _settings_path_or_none, run_test_groups,
        )

        root = _settings_path_or_none(settings.judge_work_dir)
        host_root = _settings_path_or_none(settings.judge_host_work_dir)
        cleanup = None
        if root is not None:
            workdir, cleanup = _make_work_dir(root, "dai-trial-")
            host_workdir = host_root / workdir.relative_to(root) if host_root else workdir
        else:
            temporary = tempfile.TemporaryDirectory(prefix="dai-trial-")
            workdir = Path(temporary.name)
            host_workdir = workdir
            cleanup = temporary.cleanup
        try:
            with _trial_lease_heartbeat(db, run_id, token) as heartbeat:
                execution = run_test_groups(
                    workdir, host_workdir, run.code, list(candidate["test_groups"]),
                    settings, int(candidate["timeout_seconds"]),
                    int(candidate["memory_limit_mb"]), image_ref=candidate.get("image_ref"),
                    heartbeat=heartbeat,
                )
            system_errors = list(execution["system_errors"])
            functional, robustness, details = _calc_fr_scores(
                list(candidate["test_groups"]), execution["results"], system_errors)
            if system_errors:
                _fail_trial_run(
                    db, run_id, token, "DETERMINISTIC_TRIAL_FAILED",
                    "；".join(system_errors[:5]),
                    values={"functional_score": functional, "robustness_score": robustness,
                            "deterministic_details": {"groups": details,
                                                       "system_errors": system_errors}},
                    expected_stage="deterministic_running",
                )
                return
            changed = db.execute(update(RubricTrialRun).where(
                RubricTrialRun.id == run_id, RubricTrialRun.lease_token == token,
                RubricTrialRun.stage == "deterministic_running",
                RubricTrialRun.status == "running",
            ).values(
                status="queued", stage="ai_queued",
                functional_score=functional, robustness_score=robustness,
                deterministic_details={"groups": details, "system_errors": []},
                lease_token=None, lease_until=None, wake_published_at=None,
                error_code=None, error_message=None,
            ))
            db.commit()
            if changed.rowcount == 1:
                enqueue_trial_ai_run(db, redis_client, run_id)
        finally:
            if cleanup:
                cleanup()
    except Exception as exc:  # noqa: BLE001 - failures retain the last completed stage
        db.rollback()
        logger.exception("草稿确定性试判失败: trial_run=%s", run_id)
        _fail_trial_run(db, run_id, token, "DETERMINISTIC_TRIAL_FAILED", str(exc)[:1000],
                        expected_stage="deterministic_running")


def run_trial_run(db: Session, redis_client: Redis, settings: Settings, run_id: int) -> None:
    """AI stage for a full trial; retain the historical A/Q-only path for old rows."""
    run = db.get(RubricTrialRun, run_id)
    if run is None:
        return
    if run.trial_type == "ai_only":
        return _run_legacy_ai_only_trial(db, redis_client, settings, run_id)
    token = claim_trial_run(db, run_id, expected_stage="ai_queued", running_stage="ai_running")
    if token is None:
        return
    run = db.get(RubricTrialRun, run_id)
    if run is None or not isinstance(run.candidate_snapshot, dict):
        if run is not None:
            _fail_trial_run(db, run_id, token, "TRIAL_CANDIDATE_MISSING", "试判缺少冻结候选配置",
                            expected_stage="ai_running")
        return
    candidate = dict(run.candidate_snapshot)
    rubric = dict(candidate.get("rubric") or {})
    try:
        static = analyze_python(run.code)
        deterministic = {
            "groups": list((run.deterministic_details or {}).get("groups") or []),
            "f_score": run.functional_score,
            "r_score": run.robustness_score,
        }
        messages = build_grading_messages(
            rubric, dict(candidate.get("context") or {}), run.code,
            deterministic, static, rubric_version=int(candidate.get("rubric_version") or 1),
        )
        with _trial_lease_heartbeat(db, run_id, token):
            client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
            raw = client.chat_json(messages, operation="ai_grading")
        parsed = AIGradeResponse.model_validate(raw)
        errors = validate_ai_output(
            rubric, parsed.model_dump(), list(range(1, len(run.code.splitlines()) + 1)))
        from app.services.ai_grading_service import detect_cross_dimension_duplicates

        duplicates = detect_cross_dimension_duplicates(
            [item.model_dump() for item in parsed.algorithm.items],
            [item.model_dump() for item in parsed.code_quality.items])
        if duplicates:
            errors.extend(f"跨维度重复扣分: {item['a_criterion']} / {item['q_criterion']}"
                          for item in duplicates)
        if errors:
            _fail_trial_run(db, run_id, token, "AI_TRIAL_INVALID", "；".join(errors[:5]),
                            expected_stage="ai_running")
            return
        cap = None
        cap_ids = set(parsed.triggered_cap_rule_ids or [])
        for rule in candidate.get("score_cap_rules") or []:
            if rule.get("id") in cap_ids:
                value = float(rule.get("cap", 100))
                cap = min(cap, value) if cap is not None else value
        merged = merge_scores(
            f=float(run.functional_score or 0),
            a=float(parsed.algorithm.dimension_score),
            r=float(run.robustness_score or 0),
            q=float(parsed.code_quality.dimension_score),
            cap=cap,
            exam_points=candidate.get("exam_points"),
        )
        now = datetime.now(timezone.utc)
        changed = db.execute(update(RubricTrialRun).where(
            RubricTrialRun.id == run_id, RubricTrialRun.lease_token == token,
            RubricTrialRun.stage == "ai_running", RubricTrialRun.status == "running",
        ).values(
            status="completed", stage="completed", algorithm_score=parsed.algorithm.dimension_score,
            quality_score=parsed.code_quality.dimension_score,
            preview_score_100=merged.final_score_100,
            total_score=merged.scaled_score,
            score_max=float(candidate.get("exam_points") or 100.0), complete=True,
            ai_result=parsed.model_dump(), raw_response=json.dumps(raw, ensure_ascii=False),
            lease_token=None, lease_until=None, error_code=None, error_message=None,
            finished_at=now,
        ))
        db.commit()
        if changed.rowcount != 1:
            logger.warning("试判 AI 结果被租约栅栏拒绝: trial_run=%s", run_id)
    except AIServiceError as exc:
        db.rollback()
        _fail_trial_run(db, run_id, token, _ai_error_code(exc), str(exc),
                        expected_stage="ai_running")
    except Exception as exc:  # noqa: BLE001 - a failed stage never creates a total score
        db.rollback()
        logger.exception("完整草稿试判 AI 阶段失败: trial_run=%s", run_id)
        _fail_trial_run(db, run_id, token, "TRIAL_RUN_FAILED", str(exc),
                        expected_stage="ai_running")


def _run_legacy_ai_only_trial(db: Session, redis_client: Redis,
                              settings: Settings, run_id: int) -> None:
    """Process pre-migration A/Q-only previews without presenting them as full trials."""
    token = claim_trial_run(db, run_id, expected_stage="ai_queued", running_stage="ai_running")
    if token is None:
        return
    run = db.get(RubricTrialRun, run_id)
    rubric = db.get(QuestionRubric, run.draft_rubric_id) if run and run.draft_rubric_id else None
    question = _question(db, run.question_kind, run.question_id) if run else None
    if rubric is None or question is None:
        _fail_trial_run(db, run_id, token, "TRIAL_TARGET_MISSING", "草稿评分标准或题目不存在",
                        expected_stage="ai_running")
        return
    try:
        static = analyze_python(run.code)
        question_snapshot = {
            "title": getattr(question, "title", None) or getattr(question, "prompt", ""),
            "description": getattr(question, "description", None) or getattr(question, "prompt", None),
            "function_name": getattr(question, "function_name", None),
            "teacher_constraints": dict(getattr(question, "teacher_constraints", {}) or {}),
        }
        messages = build_grading_messages(dict(rubric.rubric_json or {}), question_snapshot,
                                          run.code, {}, static, rubric_version=rubric.version)
        with _trial_lease_heartbeat(db, run_id, token):
            client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
            raw = client.chat_json(messages, operation="ai_grading")
        parsed = AIGradeResponse.model_validate(raw)
        errors = validate_ai_output(dict(rubric.rubric_json or {}), parsed.model_dump(),
                                    list(range(1, len(run.code.splitlines()) + 1)))
        if errors:
            _fail_trial_run(db, run_id, token, "AI_TRIAL_INVALID", "；".join(errors[:5]),
                            expected_stage="ai_running")
            return
        preview = merge_scores(f=0, a=parsed.algorithm.dimension_score, r=0,
                               q=parsed.code_quality.dimension_score, cap=None, exam_points=None)
        changed = db.execute(update(RubricTrialRun).where(
            RubricTrialRun.id == run_id, RubricTrialRun.lease_token == token,
            RubricTrialRun.stage == "ai_running",
        ).values(status="completed", stage="completed", algorithm_score=parsed.algorithm.dimension_score,
                 quality_score=parsed.code_quality.dimension_score,
                 preview_score_100=preview.final_score_100, total_score=None, complete=False,
                 ai_result=parsed.model_dump(), raw_response=json.dumps(raw, ensure_ascii=False),
                 lease_token=None, lease_until=None, error_code=None, error_message=None,
                 finished_at=datetime.now(timezone.utc)))
        db.commit()
        if changed.rowcount != 1:
            logger.warning("旧版 A/Q 试判结果被租约栅栏拒绝: trial_run=%s", run_id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _fail_trial_run(db, run_id, token,
                        _ai_error_code(exc) if isinstance(exc, AIServiceError) else "TRIAL_RUN_FAILED",
                        str(exc), expected_stage="ai_running")


def _fail_trial_run(db: Session, run_id: int, token: str, code: str, message: str,
                    *, expected_stage: str | None = None, values: dict | None = None) -> None:
    conditions = [RubricTrialRun.id == run_id, RubricTrialRun.lease_token == token,
                  RubricTrialRun.status == "running"]
    if expected_stage is not None:
        conditions.append(RubricTrialRun.stage == expected_stage)
    fields = dict(values or {})
    fields.update(status="failed", stage="failed", error_code=code,
                  error_message=message[:1000], lease_token=None, lease_until=None,
                  complete=False, total_score=None, wake_published_at=None,
                  finished_at=datetime.now(timezone.utc))
    db.execute(
        update(RubricTrialRun)
        .execution_options(synchronize_session=False)
        .where(*conditions)
        .values(**fields)
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
    expected_revision: int | None = None,
) -> dict:
    """保存并启用：patch_rubric → lock_rubric → create_draft → confirm → activate 单事务。

    任一步骤抛错都不提交，API 会话关闭时整体回滚——不会出现「rubric 已锁但版本没启用」
    或「版本已启用但题目仍指向旧版本」的半提交状态。
    """
    # Serialize enablement with draft saves, and refresh the ORM snapshot only
    # after acquiring the row lock. A preflight revision check alone races edits.
    job = db.scalar(select(AiGradingSetupJob).where(AiGradingSetupJob.id == job.id)
                    .with_for_update().execution_options(populate_existing=True))
    if job is None:
        raise SetupJobError("配置任务不存在")
    if expected_revision is not None and expected_revision != int(job.draft_revision or 0):
        raise DraftRevisionConflict(
            f"草稿已更新（当前修订号 {job.draft_revision}），请刷新后重试")
    if job.stage == "enabled":
        raise SetupJobError("任务已启用")
    if job.stage != "draft_ready":
        raise SetupJobError("草稿尚未生成完成，不能启用")

    question = _question(db, job.question_kind, job.question_id)
    if question is None:
        raise SetupJobError("题目不存在")

    document = _resolve_rubric_document(db, job, rubric)
    group_dicts = _resolve_groups(db, job, test_groups)
    entry = entry_function or job.draft_entry_function or getattr(question, "function_name", None)
    if not isinstance(entry, str) or not entry.isidentifier():
        raise SetupJobError("入口函数名不合法，无法生成评分版本")

    grading_mode = job.requested_grading_mode or getattr(question, "grading_mode", "legacy") or "legacy"
    if job.question_kind == "exam" and getattr(question, "question_type", "code") != "code" \
            and grading_mode != "legacy":
        raise SetupJobError("选择题只支持 legacy 模式")
    # 练习题目口径是 deterministic|ai，评分版本口径是 legacy|active：
    # 题目上存创作口径、版本上存版本模式，两者显式映射（否则题目 CHECK 约束会拒绝）。
    practice_mode: str | None = None
    version_mode = grading_mode
    if job.question_kind == "practice_problem":
        from app.services.practice_service import MODE_TO_VERSION_MODE

        practice_mode = "ai" if grading_mode in ("active", "shadow") else "deterministic"
        version_mode = MODE_TO_VERSION_MODE[practice_mode]
        grading_mode = practice_mode
    effective_teacher_constraints = dict(
        teacher_constraints if teacher_constraints is not None else
        job.draft_teacher_constraints if job.draft_teacher_constraints is not None else
        job.requested_teacher_constraints or getattr(question, "teacher_constraints", {}) or {})
    effective_compare_rules = dict(
        compare_rules if compare_rules is not None else job.draft_compare_rules or {})
    effective_score_cap_rules = list(
        score_cap_rules if score_cap_rules is not None else
        job.draft_score_cap_rules if job.draft_score_cap_rules is not None else
        getattr(question, "score_cap_rules", []) or [])

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
    question.teacher_constraints = effective_teacher_constraints
    if job.requested_reference_solution is not None:
        question.reference_solution = job.requested_reference_solution
    question.test_groups = group_dicts
    question.score_cap_rules = effective_score_cap_rules
    if job.requested_grading_mode is not None or practice_mode is not None:
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
        compare_rules=effective_compare_rules,
        score_cap_rules=effective_score_cap_rules,
        context_snapshot={"title": getattr(question, "title", None) or getattr(question, "prompt", ""),
                          "source": "ai_setup"},
        rubric_id=rubric_row.id,
        source_kind="ai_draft",
        grading_mode_snapshot=version_mode,
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
