"""AI 评分队列——DB 驱动、Redis 唤醒、幂等、恢复。所有 producer/retry/recovery/consumer 统一使用配置的 AI queue 名称。"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from redis import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CodeGrade

logger = logging.getLogger("dai.ai_queue")

# 10 分钟→30 分钟：AI 客户端最多重试 3 次 × 60–180s，合法运行会超过 600s 并在仍在运行时
# 被重新入队（白跑一遍）。栅栏落地后旧运行的写入会被丢弃，这里抬高阈值避免无谓重跑。
STALE_RUNNING_SECONDS = 1800


def _ai_queue_name() -> str:
    return get_settings().ai_queue_name


def enqueue_ai_grade(db: Session, redis_client: Redis, code_grade_id: int) -> bool:
    """将 pending CodeGrade 条件更新为 queued 并推送 Redis。幂等：非 pending 返回 False。

    先 commit 再 rpush——若 Redis 推送失败，stale recovery 会定期把孤立 queued 记录重新推送。
    """
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(CodeGrade)
        .where(CodeGrade.id == code_grade_id, CodeGrade.status == "pending")
        .values(status="queued", queued_at=now)
    )
    if result.rowcount == 0:
        return False
    msg = json.dumps({"type": "ai_grade", "id": code_grade_id, "attempt": 0})
    db.commit()
    try:
        redis_client.rpush(_ai_queue_name(), msg)
    except Exception:
        logger.warning("Redis 推送失败，queued 记录等待 stale recovery: grade=%s", code_grade_id)
    return True


def claim_ai_grade(db: Session, code_grade_id: int) -> str | None:
    """条件更新 queued → running，并铸造本次认领的 token。非 queued 返回 None。

    返回 token 表示认领成功；同一会话随后的评分写入必须带它（见 finish_ai_grade /
    fail_ai_grade 的栅栏条件），使失去认领的旧运行无法覆盖新结果。
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(CodeGrade)
        .where(CodeGrade.id == code_grade_id, CodeGrade.status == "queued")
        .values(status="running", started_at=now, attempt_count=CodeGrade.attempt_count + 1,
                claim_token=token)
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.info.setdefault("ai_claims", {})[code_grade_id] = token
    return token


def current_ai_claim_token(db: Session, code_grade_id: int) -> str | None:
    return (db.info.get("ai_claims") or {}).get(code_grade_id)


def _forget_ai_claim(db: Session, code_grade_id: int) -> None:
    claims = db.info.get("ai_claims")
    if claims:
        claims.pop(code_grade_id, None)


def ai_claim_guard(db: Session, code_grade_id: int):
    """返回本会话对该 CodeGrade 的认领栅栏条件列表；未认领时返回 None。

    调用方在自己的条件 UPDATE 里带上它，即可让"评分写入"与"仍持有认领"原子绑定。
    """
    token = current_ai_claim_token(db, code_grade_id)
    if not token:
        return None
    return [CodeGrade.id == code_grade_id, CodeGrade.status == "running",
            CodeGrade.claim_token == token]


def finish_ai_grade(db: Session, code_grade_id: int, values: dict,
                    related_updates: list[tuple] | None = None,
                    target_revision_guard: tuple | None = None) -> bool:
    """AI 评分终态写入：确认仍持有认领 + 写入全部字段，一次事务提交。

    related_updates 为 [(Model, pk, values_dict), ...]，用于在同一事务内写入
    派生结果（如 active 模式下 Submission.score / ExamAnswer.score）——
    它们与 CodeGrade 的栅栏写入同生共死，避免"CodeGrade 被拒但正式分已改"。
    """
    if target_revision_guard is not None:
        model, target_id, expected_revision = target_revision_guard
        target = db.scalar(
            select(model).where(model.id == target_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if target is None or (expected_revision is not None
                              and (target.result_revision or 0) != expected_revision):
            logger.warning("拒绝过期 AI 正式结果: grade=%s target=%s#%s",
                           code_grade_id, model.__tablename__, target_id)
            db.rollback()
            return False
        if related_updates and expected_revision is None:
            logger.warning("拒绝缺少来源修订号的 AI 正式结果: grade=%s", code_grade_id)
            db.rollback()
            return False

    guard = ai_claim_guard(db, code_grade_id)
    if guard is None:
        logger.warning("拒绝无认领的 AI 评分写入: grade=%s", code_grade_id)
        db.rollback()
        return False
    payload = dict(values)
    payload.setdefault("finished_at", datetime.now(timezone.utc))
    payload["claim_token"] = None
    # 结果修订号与结果本身在同一事务内原子推进（人工改分/客户端据此判断过时写入）
    payload["result_revision"] = CodeGrade.result_revision + 1
    result = db.execute(
        update(CodeGrade).execution_options(synchronize_session=False)
        .where(*guard).values(**payload)
    )
    if result.rowcount == 0:
        db.rollback()
        logger.warning("AI 评分写入被栅栏拒绝（已失去认领）: grade=%s", code_grade_id)
        return False
    for model, pk, related_values in (related_updates or []):
        conditions = [model.id == pk]
        if target_revision_guard is not None and model is target_revision_guard[0] \
                and pk == target_revision_guard[1]:
            conditions.append(model.result_revision == target_revision_guard[2])
        related = db.execute(
            update(model).execution_options(synchronize_session=False)
            .where(*conditions).values(**related_values)
        )
        if related.rowcount != 1:
            # 关联目标不存在（或被并发删除）：整个事务回滚，主结果不得落库，
            # 否则会出现"CodeGrade 已终态、正式分未写入"的分裂状态。
            db.rollback()
            from app.services.grading_errors import RelatedWriteError
            raise RelatedWriteError(
                f"AI 评分关联写入失败（{model.__tablename__}#{pk} 未命中），已回滚: grade={code_grade_id}"
            )
    db.commit()
    db.expire_all()
    _forget_ai_claim(db, code_grade_id)
    # 重评异议收口：只有成功写入终态分数才按分数变化转 adjusted/upheld；
    # 失败（review_required / 无分数）时保持 regrading，绝不提前关闭处理流程。
    if payload.get("status") == "completed":
        try:
            from app.services.appeal_service import resolve_regrading_appeals
            resolve_regrading_appeals(db, code_grade_id)
        except Exception:  # noqa: BLE001 - 异议收口失败不得影响评分终态
            logger.exception("重评异议收口失败: grade=%s", code_grade_id)
    return True


def complete_ai_grade(db: Session, code_grade_id: int, values: dict | None = None) -> bool:
    """标记完成（仅仍持有认领的 running → completed，不覆盖 review_required）"""
    payload = dict(values or {})
    payload["status"] = "completed"
    return finish_ai_grade(db, code_grade_id, payload)


def fail_ai_grade(
    db: Session, redis_client: Redis, code_grade_id: int,
    error: str, *, retryable: bool, max_attempts: int = 3,
) -> None:
    """处理失败：可重试→queued(重新入队)，否则→review_required。

    状态 CAS：只有仍为 running（当前 worker 认领）的 CodeGrade 才能被 fail——
    避免旧 Worker 的失败覆盖新 Worker 已完成的评分。
    """
    grade = db.get(CodeGrade, code_grade_id)
    if grade is None:
        return
    safe = _sanitize(error)
    current = grade.attempt_count
    now = datetime.now(timezone.utc)

    if retryable and current < max_attempts:
        guard = ai_claim_guard(db, code_grade_id)
        if guard is None:
            logger.warning("拒绝无认领的 AI 失败写入: grade=%s", code_grade_id)
            return
        result = db.execute(
            update(CodeGrade)
            .execution_options(synchronize_session=False)
            .where(*guard)
            .values(status="queued", last_error=safe, queued_at=now, claim_token=None)
        )
        if result.rowcount == 0:
            db.rollback()
            return  # 已被并发 Worker 处理或已失去认领，不覆盖
        db.commit()
        db.expire_all()
        _forget_ai_claim(db, code_grade_id)
        from app.services.op_metrics import record as record_metric

        record_metric(redis_client, "judge_failures_total", label="retryable")
        msg = json.dumps({"type": "ai_grade", "id": code_grade_id, "attempt": current + 1})
        redis_client.rpush(_ai_queue_name(), msg)
    else:
        guard = ai_claim_guard(db, code_grade_id)
        if guard is None:
            logger.warning("拒绝无认领的 AI 失败写入: grade=%s", code_grade_id)
            return
        result = db.execute(
            update(CodeGrade)
            .execution_options(synchronize_session=False)
            .where(*guard)
            .values(
                status="review_required", needs_teacher_review=True,
                review_reason=f"AI 评分失败（尝试 {current} 次）: {safe}",
                last_error=safe, claim_token=None,
            )
        )
        if result.rowcount == 0:
            db.rollback()
            return  # 已被并发 Worker 处理或已失去认领，不覆盖
        db.commit()
        db.expire_all()
        _forget_ai_claim(db, code_grade_id)
        from app.services.op_metrics import record as record_metric

        record_metric(redis_client, "judge_failures_total", label="permanent")
        # 考试 active CodeGrade：父级当场转 review_required（finalize 自身幂等/CAS）
        if grade.exam_answer_id:
            from app.models import ExamAnswer
            ans = db.get(ExamAnswer, grade.exam_answer_id)
            if ans:
                from app.services.exam_grading import finalize_if_ready
                finalize_if_ready(ans.submission_id, db)


def recover_stale_ai_grades(db: Session, redis_client: Redis) -> dict[str, int]:
    """恢复僵死任务：running/pending/queued 超时→重置并重新推送。

    关键：先 commit 状态变更，再 rpush Redis。避免消费者先收到消息却发现 DB 状态未更新导致 claim 失败。

    - running 超过 10 分钟→重置为 queued（保留原 attempt，不重复计数）
    - queued 超过 5 分钟→重新推送 Redis（DB 已 queued 但消息丢失）
    - pending 超过 2 分钟→直接转为 queued 并入队
    """
    recovered = {"running": 0, "queued": 0, "pending": 0}
    now = datetime.now(timezone.utc)
    qname = _ai_queue_name()

    # running 超时 → queued（CAS：仍 running 且 started_at 未变；先 commit 再 rpush）
    running_threshold = now - timedelta(seconds=STALE_RUNNING_SECONDS)
    stale_running = db.scalars(
        select(CodeGrade).where(
            CodeGrade.status == "running",
            CodeGrade.started_at < running_threshold,
        )
    ).all()
    for grade in stale_running:
        result = db.execute(
            update(CodeGrade)
            .execution_options(synchronize_session=False)
            .where(
                CodeGrade.id == grade.id,
                CodeGrade.status == "running",
                # 栅栏用认领 token 而非 started_at：token 不受 MySQL DATETIME(0) 截断影响。
                # NULL 安全比较：迁移前/旧代码认领的行 claim_token 为 NULL，`==` 会得到 NULL。
                CodeGrade.claim_token.is_not_distinct_from(grade.claim_token),
                # 代次之外复核"实际仍然过期"：SELECT 与 UPDATE 之间若已被重新认领/刷新，
                # 旧扫描结果不得把它重置（重新认领会换 token，这里再兜一层）。
                CodeGrade.started_at < running_threshold,
            )
            .values(status="queued", last_error="Worker 超时未响应", queued_at=now,
                    # 释放代次：下一次认领铸造新 token，旧运行的评分写入随即失效
                    claim_token=None)
        )
        if result.rowcount == 0:
            db.rollback()
            continue  # 已被并发实例恢复
        db.commit()  # 先持久化状态变更
        try:
            msg = json.dumps({"type": "ai_grade", "id": grade.id, "attempt": grade.attempt_count})
            redis_client.rpush(qname, msg)
        except Exception:
            logger.warning("Redis 推送失败（running→queued），等待下次恢复: grade=%s", grade.id)
        recovered["running"] += 1

    # queued 超时 → 重新推送（CAS：仍 queued 且 queued_at 未变，防重复推送）
    queued_threshold = now - timedelta(seconds=300)
    stale_queued = db.scalars(
        select(CodeGrade).where(
            CodeGrade.status == "queued",
            CodeGrade.queued_at < queued_threshold,
        )
    ).all()
    for grade in stale_queued:
        result = db.execute(
            update(CodeGrade)
            .execution_options(synchronize_session=False)
            .where(
                CodeGrade.id == grade.id,
                CodeGrade.status == "queued",
                CodeGrade.queued_at == grade.queued_at,
            )
            .values(queued_at=now)
        )
        if result.rowcount == 0:
            db.rollback()
            continue
        db.commit()  # 先持久化
        try:
            msg = json.dumps({"type": "ai_grade", "id": grade.id, "attempt": grade.attempt_count})
            redis_client.rpush(qname, msg)
        except Exception:
            logger.warning("Redis 推送失败（queued 重推），等待下次恢复: grade=%s", grade.id)
        recovered["queued"] += 1

    # pending 超时 → 转为 queued（CAS：仍 pending 且超时阈值）
    pending_threshold = now - timedelta(seconds=120)
    stale_pending = db.scalars(
        select(CodeGrade).where(
            CodeGrade.status == "pending",
            CodeGrade.created_at < pending_threshold,
        )
    ).all()
    for grade in stale_pending:
        result = db.execute(
            update(CodeGrade)
            .execution_options(synchronize_session=False)
            .where(
                CodeGrade.id == grade.id,
                CodeGrade.status == "pending",
                CodeGrade.created_at < pending_threshold,
            )
            .values(status="queued", queued_at=now)
        )
        if result.rowcount == 0:
            db.rollback()
            continue  # 已被并发实例恢复
        db.commit()  # 先持久化状态变更
        try:
            msg = json.dumps({"type": "ai_grade", "id": grade.id, "attempt": grade.attempt_count})
            redis_client.rpush(qname, msg)
        except Exception:
            logger.warning("Redis 推送失败（pending→queued），等待下次恢复: grade=%s", grade.id)
        recovered["pending"] += 1

    if any(recovered.values()):
        logger.info("stale_ai_recovery", extra=recovered)

    return recovered


def _sanitize(error: str) -> str:
    import re
    error = re.sub(r"Bearer\s+\S+", "Bearer ***", error)
    error = re.sub(r"sk-[a-zA-Z0-9]+", "sk-***", error)
    if len(error) > 1000:
        error = error[:1000]
    return error
