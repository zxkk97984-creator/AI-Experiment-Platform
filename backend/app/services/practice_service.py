"""自主练习域服务（批次 5 Q8/Q9）：题库版本、额度台账、提交、榜单与用量。

三条硬约束（写错会在真机出事）：

1. **额度以库为准**：Redis 只做快速计数，``practice_usage_logs`` 是唯一台账；
   Redis 异常时降级为纯库校验，绝不因为"计数不可用"就放行。
2. **幂等 + 并发**：提交幂等键 ``(problem_id, student_id, client_request_id)``；
   额度扣减先锁 ``practice_settings`` 单行再写台账（MySQL 行锁串行化，
   并发 50 个提交、日预算只够 10 个时恰好 10 个受理）；排名只累计
   **当前评分版本**下的历史最高分。
3. **失败不写 0 分**：AI 未完成时展示「AI 评分未完成」+ 已完成的确定性分。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import api_error
from app.models import JudgeGradingVersion, QuestionRubric, User
from app.models.practice import (
    PracticeProblem, PracticeSettings, PracticeStudentStat, PracticeSubmission,
    PracticeUsageLog,
)
from app.schemas.practice import PracticeCase
from app.services import grading_version_service as versions

logger = logging.getLogger("dai.practice")

# 通过判定阈值：题目得分 ≥ 60 记为「通过」（普通判题按用例通过率、AI 按合分）。
# 用于通过题数、distinct_solvers 与 accuracy；不是计分口径，只影响榜单展示。
PRACTICE_SOLVED_SCORE = 60.0
# 全平台榜名次上限（学生只能看到聚合结果，不暴露他人明细）
LEADERBOARD_LIMIT = 100

# 题目创作口径 ↔ 评分版本口径
MODE_TO_VERSION_MODE = {"deterministic": "legacy", "ai": "active"}
VERSION_MODE_TO_MODE = {"legacy": "deterministic", "active": "ai", "shadow": "ai"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 返回 naive datetime，MySQL 返回 aware：排序前统一成 aware。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ─────────────────────────────────────────────────────────────
# 平台设置（单行 id=1）
# ─────────────────────────────────────────────────────────────

def settings_row(db: Session, *, for_update: bool = False) -> PracticeSettings:
    """读取（必要时创建）平台设置单行；``for_update`` 用于额度扣减的串行化。"""
    query = select(PracticeSettings).where(PracticeSettings.id == 1)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is not None:
        return row
    # SQLite 下 with_for_update 是空操作；并发创建由唯一主键兜底
    row = PracticeSettings(id=1)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        row = db.scalar(select(PracticeSettings).where(PracticeSettings.id == 1)
                        .execution_options(populate_existing=True))
        if row is None:
            raise
    return row


def update_settings(db: Session, *, actor: User, values: dict) -> PracticeSettings:
    row = settings_row(db, for_update=True)
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by_id = actor.id
    db.commit()
    db.refresh(row)
    return row


# ─────────────────────────────────────────────────────────────
# 评分版本：按口径取最新版本；改题生成新版本，旧成绩留历史
# ─────────────────────────────────────────────────────────────

def version_mode_for(problem_mode: str) -> str:
    try:
        return MODE_TO_VERSION_MODE[problem_mode]
    except KeyError as exc:
        raise ValueError(f"未知练习判题方式: {problem_mode}") from exc


def problem_mode_for(version_mode: str) -> str:
    return VERSION_MODE_TO_MODE.get(version_mode, "deterministic")


def effective_grading_mode(problem: PracticeProblem, settings: PracticeSettings) -> str:
    """全局开关关着、或题目还没配好 AI 评分标准时，一律走普通判题。"""
    if settings.grading_mode != "ai" or problem.grading_mode != "ai":
        return "deterministic"
    return "ai"


def latest_version_for_mode(db: Session, *, problem_id: int, mode: str,
                            statuses: tuple[str, ...] | None = None) -> JudgeGradingVersion | None:
    """取该题该口径下版本号最大的评分版本（不按 status 过滤，保证改题后旧版本退役仍可比对）。"""
    query = select(JudgeGradingVersion).where(
        JudgeGradingVersion.practice_problem_id == problem_id,
        JudgeGradingVersion.grading_mode_snapshot == version_mode_for(mode),
    )
    if statuses:
        query = query.where(JudgeGradingVersion.status.in_(statuses))
    return db.scalars(query.order_by(JudgeGradingVersion.version.desc()).limit(1)).first()


def eligible_version_map(db: Session, *, board: str) -> dict[int, int]:
    """榜单口径下每题当前评分版本：``{problem_id: version_id}``。

    每个榜只看自己口径的最新版本——普通测试榜不会被 AI 版本覆盖，
    AI 综合榜也不会把普通判题的历史分算进来（两榜不互相污染）。
    """
    mode = version_mode_for(board)
    rows = db.execute(
        select(
            JudgeGradingVersion.practice_problem_id,
            JudgeGradingVersion.id,
            JudgeGradingVersion.version,
        )
        .where(
            JudgeGradingVersion.practice_problem_id.is_not(None),
            JudgeGradingVersion.grading_mode_snapshot == mode,
        )
        .order_by(JudgeGradingVersion.practice_problem_id, JudgeGradingVersion.version.desc())
    ).all()
    eligible: dict[int, int] = {}
    for problem_id, version_id, _version in rows:
        eligible.setdefault(int(problem_id), int(version_id))
    return eligible


def version_for_new_submission(db: Session, *, problem: PracticeProblem,
                               mode: str) -> JudgeGradingVersion | None:
    """新提交绑定该口径下**最新且可评分**的版本（active/confirmed）。"""
    return latest_version_for_mode(
        db, problem_id=problem.id, mode=mode, statuses=("active", "confirmed"),
    )


def grading_standard_updated(db: Session, submission: PracticeSubmission) -> bool:
    """提交绑定的版本是否已不是该口径的最新版本（UI 提示「本题评分标准已更新」）。"""
    if submission.grading_version_id is None:
        return False
    latest = latest_version_for_mode(db, problem_id=submission.problem_id,
                                     mode=submission.grading_mode)
    return bool(latest and latest.id != submission.grading_version_id)


# ─────────────────────────────────────────────────────────────
# 题目 -> 评分版本（发布/改题）
# ─────────────────────────────────────────────────────────────

def problem_cases(problem: PracticeProblem) -> list[dict]:
    """判题用例：只认 hidden_tests（示例用例不参与计分）。"""
    cases = problem.hidden_tests or []
    if not isinstance(cases, list):
        return []
    return [PracticeCase.model_validate(item).model_dump() for item in cases]


def build_deterministic_payload(problem: PracticeProblem) -> dict:
    """普通判题的评分版本 payload：逐例 binary_cases（容器只拿到输入）。"""
    cases = problem_cases(problem)
    if not cases:
        raise api_error(422, "PRACTICE_CASES_REQUIRED", "请先为题目配置隐藏测试用例")
    function_name = problem.function_name or "solution"
    if not function_name.isidentifier():
        raise api_error(422, "PRACTICE_ENTRY_FUNCTION_INVALID", "入口函数名不合法")
    return {
        "schema_version": 2,
        "entry_function": function_name,
        "execution_model": "isolated_case_v2",
        "binary_cases": [
            {"id": case["id"], "input": case["input"], "expected": case["expected"],
             **({"compare": case["compare"]} if case.get("compare") else {})}
            for case in cases
        ],
        "groups": [],
    }


def publish_problem(db: Session, *, problem: PracticeProblem, actor: User) -> JudgeGradingVersion:
    """发布：按当前用例生成/复用评分版本并启用，然后置为 published。

    改题（用例或入口函数变化）会生成**新版本**：旧提交仍绑定旧版本，
    从当前榜消失但历史可查（``source_digest`` 内容寻址决定是否新建）。
    """
    if problem.grading_mode == "ai":
        version = latest_version_for_mode(db, problem_id=problem.id, mode="ai")
        if version is None or version.status != "active":
            raise api_error(409, "PRACTICE_AI_NOT_READY",
                            "AI 综合判题尚未配置完成，请先完成一键配置并启用")
    else:
        payload = build_deterministic_payload(problem)
        function_name = problem.function_name or "solution"
        # 内容寻址：与 create_draft 存库时的摘要口径完全一致（归一化后再算）
        digest = versions.compute_source_digest(versions.validate_payload(payload),
                                                function_name)
        current = (db.get(JudgeGradingVersion, problem.current_grading_version_id)
                   if problem.current_grading_version_id else None)
        reusable = (current is not None
                    and current.practice_problem_id == problem.id
                    and current.grading_mode_snapshot == "legacy"
                    and current.status in ("active", "confirmed")
                    and current.source_digest == digest)
        if reusable:
            version = current
        else:
            version = versions.create_draft(
                db, question_kind="practice_problem", question_id=problem.id,
                payload=payload, entry_function=function_name,
                context_snapshot={"title": problem.title, "source": "practice_publish"},
                source_kind="manual", grading_mode_snapshot="legacy",
                actor_id=actor.id, commit=False,
            )
            versions.confirm_version(db, version, actor_id=actor.id,
                                     confirm_isolated_case_v2=True, commit=False)
            versions.activate_version(db, version, actor_id=actor.id, commit=False)
    versions.set_question_current_version(
        db, question_kind="practice_problem", question_id=problem.id, version_id=version.id,
    )
    problem.status = "published"
    problem.published_at = problem.published_at or utcnow()
    problem.archived_at = None
    db.commit()
    db.refresh(problem)
    return version


def archive_problem(db: Session, *, problem: PracticeProblem) -> PracticeProblem:
    problem.status = "archived"
    problem.archived_at = utcnow()
    db.commit()
    db.refresh(problem)
    return problem


# ─────────────────────────────────────────────────────────────
# 额度（Redis 计数 + 台账落库；Redis 故障以库为准，不得放行）
# ─────────────────────────────────────────────────────────────

def _quota_error(metric: str, limit: float, used: float) -> Exception:
    return api_error(
        429, "QUOTA_EXHAUSTED", "今日额度已用完",
        fields={"metric": metric, "limit": limit, "used": used},
    )


def _redis_precheck(redis_client, *, today: date_cls, metric: str, scope_id: int,
                    limit: float, amount: float) -> None:
    """Redis 快速计数：命中上限立即拒绝；Redis 异常时保持静默（库仍是权威）。"""
    if redis_client is None or limit <= 0:
        return
    key = f"practice:quota:{today.isoformat()}:{metric}:{scope_id}"
    try:
        current = redis_client.incrbyfloat(key, amount)
        if float(current) == float(amount):
            redis_client.expire(key, 48 * 3600)
    except Exception as exc:  # noqa: BLE001 - Redis 只做加速，故障必须降级到库
        logger.warning("练习额度 Redis 计数不可用，降级为以库为准: %s", exc)
        return
    if float(current) > float(limit):
        try:
            redis_client.incrbyfloat(key, -amount)
        except Exception:  # noqa: BLE001 - 回滚失败不影响拒绝结果
            pass
        raise _quota_error(metric, limit, float(current) - amount)


def _ledger_consume(db: Session, *, today: date_cls, metric: str, scope: str,
                    student_id: int, limit: float, amount: float) -> None:
    """台账扣减：唯一 (date, scope, student_id, metric) 行上的原子累加。"""
    row = db.scalar(
        select(PracticeUsageLog)
        .where(
            PracticeUsageLog.date == today,
            PracticeUsageLog.scope == scope,
            PracticeUsageLog.student_id == student_id,
            PracticeUsageLog.metric == metric,
        )
        .with_for_update()
    )
    if row is None:
        row = PracticeUsageLog(date=today, scope=scope, student_id=student_id,
                               metric=metric, amount=0.0)
        db.add(row)
        db.flush()
    used = float(row.amount or 0.0)
    if limit > 0 and used + amount > limit:
        raise _quota_error(metric, limit, used)
    row.amount = used + amount
    db.flush()


def consume_quota(db: Session, redis_client, *, student_id: int,
                  settings: PracticeSettings, want_ai: bool, token_estimate: int,
                  amount_price_per_1k: float | None = None) -> dict:
    """提交额度扣减（幂等调用点：只有"新建提交"这一条路径）。

    先锁 ``practice_settings`` 单行：这是所有额度决策的唯一串行化点，
    50 个并发提交在这里排队，因此恰好只有预算内的那些能受理。
    """
    settings = settings_row(db, for_update=True)
    today = utcnow().date()
    price = (settings.ai_amount_per_1k_tokens if amount_price_per_1k is None
             else amount_price_per_1k)
    consumed = {"submissions": 0.0, "ai_tokens": 0.0, "ai_amount": 0.0}

    cap = int(settings.daily_submission_cap_per_student or 0)
    _redis_precheck(redis_client, today=today, metric="submissions", scope_id=student_id,
                    limit=cap, amount=1)
    _ledger_consume(db, today=today, metric="submissions", scope="student",
                    student_id=student_id, limit=cap, amount=1)
    consumed["submissions"] = 1.0

    if want_ai:
        tokens = int(token_estimate)
        amount = round(tokens / 1000.0 * float(price or 0.0), 6)
        token_budget = int(settings.daily_ai_budget_tokens or 0)
        amount_budget = float(settings.daily_ai_budget_amount or 0.0)
        _redis_precheck(redis_client, today=today, metric="ai_tokens", scope_id=0,
                        limit=token_budget, amount=tokens)
        _ledger_consume(db, today=today, metric="ai_tokens", scope="platform",
                        student_id=0, limit=token_budget, amount=tokens)
        consumed["ai_tokens"] = float(tokens)
        if amount > 0:
            _redis_precheck(redis_client, today=today, metric="ai_amount", scope_id=0,
                            limit=amount_budget, amount=amount)
            _ledger_consume(db, today=today, metric="ai_amount", scope="platform",
                            student_id=0, limit=amount_budget, amount=amount)
            consumed["ai_amount"] = amount
    return consumed


def settle_ai_usage(db: Session, *, reserved_tokens: int, actual_tokens: int,
                    price_per_1k: float | None = None) -> None:
    """AI 结束后按**实际** token 用量结算：多退少补，日台账反映真实用量。"""
    if actual_tokens <= 0 or actual_tokens == reserved_tokens:
        return
    settings = settings_row(db, for_update=True)
    today = utcnow().date()
    price = float(settings.ai_amount_per_1k_tokens if price_per_1k is None
                  else price_per_1k)
    delta_tokens = float(actual_tokens - reserved_tokens)
    row = db.scalar(
        select(PracticeUsageLog)
        .where(PracticeUsageLog.date == today, PracticeUsageLog.scope == "platform",
               PracticeUsageLog.student_id == 0, PracticeUsageLog.metric == "ai_tokens")
        .with_for_update()
    )
    if row is not None:
        row.amount = max(0.0, float(row.amount or 0.0) + delta_tokens)
    delta_amount = round(delta_tokens / 1000.0 * price, 6)
    amount_row = db.scalar(
        select(PracticeUsageLog)
        .where(PracticeUsageLog.date == today, PracticeUsageLog.scope == "platform",
               PracticeUsageLog.student_id == 0, PracticeUsageLog.metric == "ai_amount")
        .with_for_update()
    )
    if amount_row is not None:
        amount_row.amount = max(0.0, float(amount_row.amount or 0.0) + delta_amount)
    db.commit()


def usage_summary(db: Session, *, on_date: date_cls | None = None) -> dict:
    settings = settings_row(db)
    today = on_date or utcnow().date()

    def _sum(metric: str, day: date_cls) -> float:
        return float(db.scalar(
            select(func.coalesce(func.sum(PracticeUsageLog.amount), 0.0))
            .where(PracticeUsageLog.date == day, PracticeUsageLog.metric == metric)
        ) or 0.0)

    from app.services import practice_queue

    daily = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        daily.append({
            "date": day.isoformat(),
            "submissions": _sum("submissions", day),
            "ai_tokens": _sum("ai_tokens", day),
            "ai_amount": _sum("ai_amount", day),
        })
    return {
        "date": today.isoformat(),
        "submissions_today": _sum("submissions", today),
        "ai_tokens_used": _sum("ai_tokens", today),
        "ai_budget_tokens": int(settings.daily_ai_budget_tokens or 0),
        "ai_amount_used": _sum("ai_amount", today),
        "ai_budget_amount": float(settings.daily_ai_budget_amount or 0.0),
        "queue": {
            "queued": practice_queue.queued_count(db),
            "running": practice_queue.running_count(db),
            "max_concurrent": int(settings.max_concurrent_practice_jobs or 1),
        },
        "daily": daily,
    }


# ─────────────────────────────────────────────────────────────
# 提交（幂等 + 快照 + 额度）
# ─────────────────────────────────────────────────────────────

def code_digest(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def find_idempotent_submission(db: Session, *, problem_id: int, student_id: int,
                               client_request_id: str) -> PracticeSubmission | None:
    return db.scalar(
        select(PracticeSubmission).where(
            PracticeSubmission.problem_id == problem_id,
            PracticeSubmission.student_id == student_id,
            PracticeSubmission.client_request_id == client_request_id,
        )
    )


def create_submission(db: Session, redis_client, *, student: User, problem: PracticeProblem,
                      code: str, client_request_id: str,
                      token_estimate: int) -> tuple[PracticeSubmission, bool]:
    """学生提交：幂等、提交时刻快照判题方式与评分版本、额度不足直接拒绝。

    ``token_estimate`` 是 AI 模式下预扣的 token 估算值（按实际用量结算）。
    """
    digest = code_digest(code)
    existing = find_idempotent_submission(
        db, problem_id=problem.id, student_id=student.id,
        client_request_id=client_request_id)
    if existing is not None:
        if existing.request_digest != digest:
            raise api_error(409, "PRACTICE_IDEMPOTENCY_CONFLICT",
                            "该提交请求标识已用于另一份代码，请刷新后重试")
        return existing, False

    settings = settings_row(db, for_update=True)
    mode = effective_grading_mode(problem, settings)
    version = version_for_new_submission(db, problem=problem, mode=mode)
    if version is None:
        raise api_error(409, "PRACTICE_NOT_READY", "本题评分标准尚未就绪，暂时不能提交")

    consume_quota(db, redis_client, student_id=student.id, settings=settings,
                  want_ai=(mode == "ai"), token_estimate=token_estimate)

    submission = PracticeSubmission(
        problem_id=problem.id,
        student_id=student.id,
        code=code,
        client_request_id=client_request_id,
        request_digest=digest,
        grading_mode=mode,
        grading_version_id=version.id,
        grading_status="queued",
        max_score=100.0,
        queued_at=utcnow(),
    )
    db.add(submission)
    problem.total_attempts = int(problem.total_attempts or 0) + 1
    db.commit()
    db.refresh(submission)
    return submission, True


def queue_position(db: Session, submission: PracticeSubmission) -> int | None:
    """排队位次（1 = 队首）；已开始判题/终态返回 None。"""
    if submission.grading_status != "queued":
        return None
    ahead = db.scalar(
        select(func.count()).select_from(PracticeSubmission).where(
            PracticeSubmission.grading_status == "queued",
            PracticeSubmission.id < submission.id,
        )
    ) or 0
    return int(ahead) + 1


def mark_practice_ai_incomplete(db: Session, submission_id: int) -> None:
    """AI 终态为 review_required 时收口：保留确定性分，明确标记 AI 未完成。"""
    from sqlalchemy import update

    result = db.execute(
        update(PracticeSubmission)
        .execution_options(synchronize_session=False)
        .where(
            PracticeSubmission.id == submission_id,
            PracticeSubmission.grading_status.in_(("queued", "running")),
        )
        .values(
            grading_status="review_required",
            score=PracticeSubmission.deterministic_score,
            last_error="AI 评分未完成：仅保留已完成的确定性分",
            claim_token=None,
            finished_at=utcnow(),
            result_revision=PracticeSubmission.result_revision + 1,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        return
    db.commit()
    db.expire_all()


def reconcile_after_ai(db: Session, submission_id: int) -> None:
    """AI 运行结束后的兜底收口：成功由评分事务写入，这里只处理未完成终态。"""
    from app.models import CodeGrade

    db.expire_all()
    submission = db.get(PracticeSubmission, submission_id)
    if submission is None or submission.grading_status not in ("queued", "running"):
        return
    grade = (db.get(CodeGrade, submission.code_grade_id)
             if submission.code_grade_id else None)
    if grade is None:
        return
    if grade.status == "review_required":
        mark_practice_ai_incomplete(db, submission_id)


def completed_ai_grade(db: Session, submission: PracticeSubmission):
    from app.models import CodeGrade

    if submission.code_grade_id is None:
        return None
    return db.get(CodeGrade, submission.code_grade_id)


# ─────────────────────────────────────────────────────────────
# AI 预检（开启 AI 前列出未就绪题目）
# ─────────────────────────────────────────────────────────────

def missing_ai_requirements(db: Session, problem: PracticeProblem) -> list[str]:
    """AI 未就绪的判据（与规格一致）：没有已启用版本，或版本缺少 locked Rubric。"""
    missing: list[str] = []
    version = latest_version_for_mode(db, problem_id=problem.id, mode="ai")
    if version is None or version.status != "active":
        missing.append("active_version")
    if version is not None:
        if version.rubric_id is None:
            missing.append("locked_rubric")
        else:
            rubric = db.get(QuestionRubric, version.rubric_id)
            if rubric is None or rubric.status != "locked":
                missing.append("locked_rubric")
            elif not versions.rubric_binding_ok(db, version):
                missing.append("frozen_rubric")
    return sorted(set(missing))


def ai_preflight(db: Session) -> dict:
    problems = db.scalars(
        select(PracticeProblem).where(PracticeProblem.status == "published")
        .order_by(PracticeProblem.id)
    ).all()
    pending = []
    for problem in problems:
        missing = missing_ai_requirements(db, problem)
        if missing:
            pending.append({
                "id": problem.id, "title": problem.title,
                "status": problem.status, "missing": missing,
            })
    return {"ready": not pending, "problems": pending, "checked_at": utcnow().isoformat()}


# ─────────────────────────────────────────────────────────────
# 榜单与个人统计
# ─────────────────────────────────────────────────────────────

def _alias(settings, *, salt: str, student_id: int) -> str:
    digest = hmac.new(settings.secret_key.encode(), f"{salt}:{student_id}".encode(),
                      hashlib.sha256).hexdigest()
    return f"同学 {digest[:6].upper()}"


def leaderboard(db: Session, *, board: str, scope_kind: str, scope_id: int | None,
                settings, viewer: User, student_ids: set[int] | None = None) -> dict:
    """按当前评分版本聚合的只读榜单：总分 → 通过题数 → 达成时间 → 稳定标识。

    只返回聚合结果与化名，绝不包含学号、源码或他人评分明细。
    """
    eligible = eligible_version_map(db, board=board)
    items: list[dict] = []
    if eligible:
        rows = db.execute(
            select(
                PracticeSubmission.student_id,
                PracticeSubmission.problem_id,
                func.max(PracticeSubmission.score).label("best"),
                func.min(PracticeSubmission.finished_at).label("first_finished"),
            )
            .where(
                PracticeSubmission.grading_version_id.in_(list(eligible.values())),
                PracticeSubmission.grading_mode == board,
                PracticeSubmission.counted_for_rank.is_(True),
                PracticeSubmission.score.is_not(None),
            )
            .group_by(PracticeSubmission.student_id, PracticeSubmission.problem_id)
        ).all()
        per_student: dict[int, dict] = defaultdict(
            lambda: {"total_score": 0.0, "solved_count": 0, "attempted": 0,
                     "achieved_at": None})
        for student_id, _problem_id, best, finished in rows:
            bucket = per_student[int(student_id)]
            bucket["attempted"] += 1
            bucket["total_score"] = round(bucket["total_score"] + float(best or 0.0), 4)
            if float(best or 0.0) >= PRACTICE_SOLVED_SCORE:
                bucket["solved_count"] += 1
            if finished is not None and (bucket["achieved_at"] is None
                                         or finished < bucket["achieved_at"]):
                bucket["achieved_at"] = finished
        if student_ids is not None:
            per_student = {sid: data for sid, data in per_student.items()
                           if sid in student_ids}
        ordered = sorted(
            per_student.items(),
            key=lambda item: (
                -item[1]["total_score"],
                -item[1]["solved_count"],
                _as_utc(item[1]["achieved_at"]) or datetime.max.replace(tzinfo=timezone.utc),
                item[0],
            ),
        )[:LEADERBOARD_LIMIT]
        names: dict[int, str] = {}
        if scope_kind == "offering":
            names = dict(db.execute(
                select(User.id, User.real_name).where(User.id.in_(list(per_student.keys())))
            ).all())
        rank = 0
        previous_total = None
        for index, (student_id, data) in enumerate(ordered, 1):
            if previous_total is None or abs(previous_total - data["total_score"]) > 1e-9:
                rank = index
            previous_total = data["total_score"]
            alias = _alias(settings, salt=f"practice:{scope_kind}:{scope_id or 0}",
                           student_id=student_id)
            display = names.get(student_id, alias) if scope_kind == "offering" else alias
            items.append({
                "rank": rank,
                # 全平台榜一律化名；班内榜也只在所属班内显示姓名
                "display_name": display,
                "solved_count": data["solved_count"],
                "total_score": data["total_score"],
                "avg_score": (round(data["total_score"] / data["attempted"], 4)
                              if data["attempted"] else None),
                "achieved_at": (data["achieved_at"].isoformat()
                                if data["achieved_at"] else None),
                "is_me": viewer.role == "student" and student_id == viewer.id,
            })
    return {
        "items": items, "total": len(items), "board": board,
        "scope": scope_kind, "generated_at": utcnow().isoformat(),
    }


def student_summary(db: Session, *, student_id: int, settings: PracticeSettings) -> dict:
    """个人统计：已解决/总分/平均分/提交数/打卡天数/今日额度。"""
    eligible = {
        board: eligible_version_map(db, board=board)
        for board in ("deterministic", "ai")
    }
    rows = db.scalars(
        select(PracticeSubmission).where(PracticeSubmission.student_id == student_id)
    ).all()
    best: dict[tuple[int, str], float] = {}
    submission_count = 0
    per_day: set[date_cls] = set()
    for row in rows:
        submission_count += 1
        if row.created_at is not None:
            per_day.add(row.created_at.date() if hasattr(row.created_at, "date") else row.created_at)
        if row.score is None or row.grading_version_id is None:
            continue
        board = row.grading_mode
        eligible_map = eligible.get(board) or {}
        if eligible_map.get(row.problem_id) != row.grading_version_id:
            continue
        key = (row.problem_id, board)
        best[key] = max(best.get(key, 0.0), float(row.score))
    solved = sum(1 for value in best.values() if value >= PRACTICE_SOLVED_SCORE)
    total_score = round(sum(best.values()), 4)
    attempted = len(best)
    today = utcnow().date()
    used_today = db.scalar(
        select(func.coalesce(func.sum(PracticeUsageLog.amount), 0.0)).where(
            PracticeUsageLog.date == today, PracticeUsageLog.metric == "submissions",
            PracticeUsageLog.scope == "student", PracticeUsageLog.student_id == student_id,
        )
    ) or 0.0
    stat = db.scalar(
        select(PracticeStudentStat).where(
            PracticeStudentStat.student_id == student_id,
            PracticeStudentStat.scope_kind == "platform",
            PracticeStudentStat.board_kind == "deterministic",
        )
    )
    return {
        "solved_count": solved,
        "total_score": total_score,
        "avg_score": round(total_score / attempted, 4) if attempted else None,
        "accuracy": round(solved / attempted, 4) if attempted else None,
        "submission_count": submission_count,
        "streak_days": int(stat.streak_days if stat is not None else len(per_day)),
        "daily_submissions_used": float(used_today),
        "daily_submission_cap": int(settings.daily_submission_cap_per_student or 0),
    }


def recompute_student_stat(db: Session, *, student_id: int) -> None:
    """幂等重算个人统计行（批次 6 的定时任务复用；请求路径不依赖它）。"""
    eligible = {
        board: eligible_version_map(db, board=board)
        for board in ("deterministic", "ai")
    }
    rows = db.scalars(
        select(PracticeSubmission).where(
            PracticeSubmission.student_id == student_id,
            PracticeSubmission.score.is_not(None),
        )
    ).all()
    for board in ("deterministic", "ai"):
        eligible_map = eligible[board] or {}
        best: dict[int, float] = {}
        achieved: datetime | None = None
        for row in rows:
            if row.grading_mode != board or row.grading_version_id is None:
                continue
            if eligible_map.get(row.problem_id) != row.grading_version_id:
                continue
            best[row.problem_id] = max(best.get(row.problem_id, 0.0), float(row.score or 0.0))
            if row.finished_at is not None and (achieved is None or row.finished_at < achieved):
                achieved = row.finished_at
        solved = sum(1 for value in best.values() if value >= PRACTICE_SOLVED_SCORE)
        total = round(sum(best.values()), 4)
        stat = db.scalar(
            select(PracticeStudentStat).where(
                PracticeStudentStat.student_id == student_id,
                PracticeStudentStat.scope_kind == "platform",
                PracticeStudentStat.scope_id == 0,
                PracticeStudentStat.board_kind == board,
            )
        )
        if stat is None:
            stat = PracticeStudentStat(student_id=student_id, scope_kind="platform",
                                       scope_id=0, board_kind=board)
            db.add(stat)
        stat.solved_count = solved
        stat.total_score = total
        stat.avg_score = round(total / len(best), 4) if best else None
        stat.accuracy = round(solved / len(best), 4) if best else None
        stat.achieved_at = achieved
    db.commit()


def ai_token_sink():
    """收集本次 AI 调用的实际 token 用量（同时保留既有指标聚合）。"""
    from app.services.op_metrics import ai_metrics_sink

    upstream = ai_metrics_sink()
    totals = {"prompt_tokens": 0, "completion_tokens": 0}

    def _sink(data: dict) -> None:
        upstream(data)
        totals["prompt_tokens"] += int(data.get("prompt_tokens") or 0)
        totals["completion_tokens"] += int(data.get("completion_tokens") or 0)

    return totals, _sink


def problem_read(db: Session, problem: PracticeProblem, *, student_id: int | None = None,
                 detail: bool = False) -> dict:
    """题目序列化：学生视角不含隐藏用例与参考解法。"""
    payload = {
        "id": problem.id,
        "title": problem.title,
        "difficulty": problem.difficulty,
        "tags": list(problem.tags or []),
        "prompt": problem.prompt,
        "status": problem.status,
        "grading_mode": problem.grading_mode,
        "time_limit_ms": problem.time_limit_ms,
        "memory_limit_kb": problem.memory_limit_kb,
        "total_attempts": int(problem.total_attempts or 0),
        "distinct_solvers": int(problem.distinct_solvers or 0),
        "current_grading_version_id": problem.current_grading_version_id,
        "created_at": problem.created_at.isoformat() if problem.created_at else None,
    }
    if detail:
        payload.update({
            "starter_code": problem.starter_code,
            "public_cases": list(problem.public_cases or []),
        })
        if student_id is not None:
            payload.update(_student_problem_progress(db, problem_id=problem.id,
                                                     student_id=student_id))
    return payload


def _student_problem_progress(db: Session, *, problem_id: int, student_id: int) -> dict:
    latest = db.scalars(
        select(PracticeSubmission).where(
            PracticeSubmission.problem_id == problem_id,
            PracticeSubmission.student_id == student_id,
        ).order_by(PracticeSubmission.id.desc()).limit(1)
    ).first()
    count = db.scalar(
        select(func.count()).select_from(PracticeSubmission).where(
            PracticeSubmission.problem_id == problem_id,
            PracticeSubmission.student_id == student_id,
        )
    ) or 0
    best = db.scalar(
        select(func.max(PracticeSubmission.score)).where(
            PracticeSubmission.problem_id == problem_id,
            PracticeSubmission.student_id == student_id,
            PracticeSubmission.grading_mode
            == (latest.grading_mode if latest is not None else "deterministic"),
        )
    )
    if latest is None:
        status = "not_started"
    elif best is not None and float(best) >= PRACTICE_SOLVED_SCORE:
        status = "solved"
    else:
        status = "attempted"
    return {
        "my_status": status,
        "my_best_score": float(best) if best is not None else None,
        "attempt_count": int(count),
        "latest_submission_id": latest.id if latest is not None else None,
        "grading_standard_updated": (grading_standard_updated(db, latest)
                                     if latest is not None else False),
    }


def submission_read(db: Session, submission: PracticeSubmission, *, settings) -> dict:
    """提交序列化（学生本人可见）：AI 未完成时 ai_result 为空、分数只放确定性分。"""
    problem = db.get(PracticeProblem, submission.problem_id)
    grade = completed_ai_grade(db, submission)
    ai_result = None
    ai_status = "skipped"
    if submission.grading_mode == "ai":
        if grade is not None and grade.status == "completed":
            ai_status = "completed"
        elif grade is not None and grade.status == "review_required":
            ai_status = "failed"
        else:
            ai_status = "pending"
    if grade is not None and grade.status == "completed":
        from app.services.student_ai_results import build_student_grading_breakdown

        breakdown = build_student_grading_breakdown(grade)
        breakdown["test_groups"] = breakdown.get("test_groups") or []
        ai_result = breakdown
        ai_status = "completed"
    deterministic = submission.result_details or {}
    groups = [
        {
            "name": group.get("name") or group.get("id"),
            "status": ("passed" if not group.get("failed") and not group.get("platform_errors")
                       and group.get("passed") else
                       "error" if group.get("platform_errors") else "failed"),
            "passed": group.get("passed", 0),
            "total": group.get("case_total") or (group.get("passed", 0) + group.get("failed", 0)),
            "detail": (group.get("platform_errors") or [None])[0],
        }
        for group in (deterministic.get("groups") or [])
    ]
    appeal_row = _grade_appeal_row(db, submission.id)
    appeal = None
    if appeal_row is not None:
        appeal = {
            "id": appeal_row.id, "status": appeal_row.status, "reason": appeal_row.reason,
            "handler_comment": appeal_row.handler_comment,
            "score_before": appeal_row.score_before, "score_after": appeal_row.score_after,
            "created_at": (appeal_row.created_at.isoformat()
                           if appeal_row.created_at else None),
        }
    can_appeal = bool(
        submission.grading_status in ("completed", "review_required")
        and submission.score is not None
        and (appeal_row is None or appeal_row.status in ("withdrawn", "rejected"))
    )
    return {
        "id": submission.id,
        "problem": ({
            "id": problem.id, "title": problem.title, "difficulty": problem.difficulty,
            "tags": list(problem.tags or []), "grading_mode": problem.grading_mode,
            "current_grading_version_id": problem.current_grading_version_id,
        } if problem is not None else None),
        "code": submission.code,
        "grading_status": submission.grading_status,
        "queue_position": queue_position(db, submission),
        "score": submission.score,
        "deterministic_score": submission.deterministic_score,
        "max_score": submission.max_score,
        "grading_mode": submission.grading_mode,
        "grading_version_id": submission.grading_version_id,
        "grading_version_stale": grading_standard_updated(db, submission),
        "created_at": submission.created_at.isoformat() if submission.created_at else None,
        "finished_at": submission.finished_at.isoformat() if submission.finished_at else None,
        "deterministic": {
            "groups": groups,
            "passed_count": submission.tests_passed,
            "total_count": submission.tests_total,
        },
        "ai_result": ai_result,
        "ai_status": ai_status,
        "appeal": appeal,
        "can_appeal": can_appeal,
    }


def _grade_appeal_row(db: Session, submission_id: int):
    from app.models import GradeAppeal

    return db.scalars(
        select(GradeAppeal).where(
            GradeAppeal.target_kind == "practice_submission",
            GradeAppeal.target_id == submission_id,
        ).order_by(GradeAppeal.id.desc()).limit(1)
    ).first()
