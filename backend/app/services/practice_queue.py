"""练习判题队列（批次 5）——独立于正式 ``judge:*`` 队列。

设计契约（照 ``services/judge_queue.py`` / ``services/lab_queue.py`` 范式）：

* **MySQL 是唯一事实源**：任务状态全部落 ``practice_submissions``；
  Redis 只做唤醒（rpush/blpop），Redis 故障时恢复扫描按库内状态重新推送；
* **认领栅栏**：``claim_token`` + ``grading_status='running'`` 条件 UPDATE，
  失去认领的旧运行写不进任何结果（普通判题与 AI 结果都走同一把栅栏）；
* **队列隔离**：本模块只读写 ``practice:judge:queue``，与 ``judge:queue``、
  ``judge:ai:queue`` 无交集——练习积压不占用正式判题容量。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from redis import Redis
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.practice import PracticeSubmission

logger = logging.getLogger("dai.practice_queue")

MAX_ATTEMPTS = 3
# 与正式判题一致：必须覆盖最坏 Docker 判题墙钟，只有 Worker 崩溃才会超过它。
STALE_RUNNING_SECONDS = 1800
STALE_QUEUED_SECONDS = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_redis():
    """获取 Redis 客户端（每次创建，避免连接跨 fork 问题；测试 monkeypatch 本函数）。"""
    import redis as _redis

    settings = get_settings()
    return _redis.Redis.from_url(
        settings.redis_url, decode_responses=False,
        socket_connect_timeout=2, socket_timeout=3,
    )


def judge_queue_name() -> str:
    return get_settings().practice_judge_queue_name


def push_submission(redis_client: Redis | None, submission_id: int, attempt: int = 0) -> bool:
    """DB 已落库后的唤醒推送；推送失败返回 False（由恢复扫描补推）。"""
    message = json.dumps({"type": "practice_judge", "id": submission_id, "attempt": attempt})
    try:
        client = redis_client if redis_client is not None else _get_redis()
        client.rpush(judge_queue_name(), message)
        return True
    except Exception:
        logger.warning("Redis 推送失败，练习提交留在 queued 等待恢复扫描: id=%s", submission_id)
        return False


def claim_submission(db: Session, submission_id: int) -> str | None:
    """条件更新 queued → running 并铸造认领 token；非 queued 返回 None（幂等）。"""
    token = secrets.token_urlsafe(32)
    result = db.execute(
        update(PracticeSubmission)
        .execution_options(synchronize_session=False)
        .where(
            PracticeSubmission.id == submission_id,
            PracticeSubmission.grading_status == "queued",
        )
        .values(
            grading_status="running",
            started_at=_utc_now(),
            claim_token=token,
            attempt_count=PracticeSubmission.attempt_count + 1,
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.info.setdefault("practice_claims", {})[submission_id] = token
    return token


def current_claim_token(db: Session, submission_id: int) -> str | None:
    return (db.info.get("practice_claims") or {}).get(submission_id)


def _forget_claim(db: Session, submission_id: int) -> None:
    claims = db.info.get("practice_claims")
    if claims:
        claims.pop(submission_id, None)


def claim_guard(db: Session, submission_id: int):
    """本会话对该提交的认领栅栏条件；未认领返回 None（写入必须被拒绝）。"""
    token = current_claim_token(db, submission_id)
    if not token:
        return None
    return [
        PracticeSubmission.id == submission_id,
        PracticeSubmission.grading_status == "running",
        PracticeSubmission.claim_token == token,
    ]


def apply_guarded_update(db: Session, submission_id: int, values: dict, *,
                         release_claim: bool = True, stamp_finished: bool = True,
                         bump_revision: bool = True) -> bool:
    """受认领栅栏保护的字段写入（AI 中间态与终态都走这里，绝不裸 ORM 赋值）。"""
    guard = claim_guard(db, submission_id)
    if guard is None:
        logger.warning("拒绝无认领的练习判题写入: id=%s", submission_id)
        db.rollback()
        return False
    payload = dict(values)
    if stamp_finished:
        payload.setdefault("finished_at", _utc_now())
    if release_claim:
        payload["claim_token"] = None
    if bump_revision:
        payload["result_revision"] = PracticeSubmission.result_revision + 1
    result = db.execute(
        update(PracticeSubmission)
        .execution_options(synchronize_session=False)
        .where(*guard)
        .values(**payload)
    )
    if result.rowcount == 0:
        db.rollback()
        logger.warning("练习判题写入被栅栏拒绝（已失去认领）: id=%s", submission_id)
        return False
    db.commit()
    db.expire_all()
    if release_claim:
        _forget_claim(db, submission_id)
    return True


def finish_submission(db: Session, submission_id: int, values: dict) -> bool:
    """终态写入：只有仍持有认领的运行才能落地；结果修订号原子 +1。"""
    return apply_guarded_update(db, submission_id, values)


def heartbeat_submission(db: Session, submission_id: int, *, lease_seconds: int = 120) -> bool:
    """续租：正在判题的长任务不得被恢复扫描当成僵死任务。"""
    guard = claim_guard(db, submission_id)
    if guard is None:
        return False
    result = db.execute(
        update(PracticeSubmission)
        .execution_options(synchronize_session=False)
        .where(*guard)
        .values(lease_until=_utc_now() + timedelta(seconds=lease_seconds))
    )
    db.commit()
    return result.rowcount == 1


def fail_submission(
    db: Session, redis_client: Redis | None, submission_id: int, error: str,
    *, retryable: bool, max_attempts: int = MAX_ATTEMPTS,
) -> None:
    """失败处理：可重试→queued 并重推；否则终态 review_required 且**不写 0 分**。"""
    row = db.get(PracticeSubmission, submission_id)
    if row is None:
        return
    safe = _sanitize(error)
    now = _utc_now()
    if retryable and row.attempt_count < max_attempts:
        guard = claim_guard(db, submission_id)
        if guard is None:
            return
        result = db.execute(
            update(PracticeSubmission)
            .execution_options(synchronize_session=False)
            .where(*guard)
            .values(grading_status="queued", last_error=safe, queued_at=now, claim_token=None)
        )
        if result.rowcount == 0:
            db.rollback()
            return
        db.commit()
        db.expire_all()
        _forget_claim(db, submission_id)
        push_submission(redis_client, submission_id, attempt=row.attempt_count + 1)
        return

    guard = claim_guard(db, submission_id)
    if guard is None:
        return
    result = db.execute(
        update(PracticeSubmission)
        .execution_options(synchronize_session=False)
        .where(*guard)
        .values(
            grading_status="review_required",
            # 平台/基础设施问题绝不折算成学生 0 分：保留已完成的确定性分（可能为空）
            score=PracticeSubmission.deterministic_score,
            last_error=safe,
            claim_token=None,
            finished_at=now,
            result_revision=PracticeSubmission.result_revision + 1,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        return
    db.commit()
    db.expire_all()
    _forget_claim(db, submission_id)


def recover_stale_submissions(db: Session, redis_client: Redis | None) -> dict[str, int]:
    """恢复扫描：queued 超时补推、running 超时重置为 queued（多实例 CAS 安全）。"""
    stats = {"queued_repushed": 0, "running_reset": 0}
    now = _utc_now()

    queued_deadline = now - timedelta(seconds=STALE_QUEUED_SECONDS)
    stale_queued = db.scalars(
        select(PracticeSubmission)
        .execution_options(populate_existing=True)
        .where(
            PracticeSubmission.grading_status == "queued",
            PracticeSubmission.queued_at < queued_deadline,
        )
        .limit(200)
    ).all()
    for row in stale_queued:
        result = db.execute(
            update(PracticeSubmission)
            .execution_options(synchronize_session=False)
            .where(
                PracticeSubmission.id == row.id,
                PracticeSubmission.grading_status == "queued",
                PracticeSubmission.queued_at == row.queued_at,
            )
            .values(queued_at=now)
        )
        if result.rowcount == 0:
            db.rollback()
            continue
        db.commit()
        push_submission(redis_client, row.id, attempt=row.attempt_count)
        stats["queued_repushed"] += 1

    running_deadline = now - timedelta(seconds=STALE_RUNNING_SECONDS)
    stale_running = db.scalars(
        select(PracticeSubmission)
        .execution_options(populate_existing=True)
        .where(
            PracticeSubmission.grading_status == "running",
            PracticeSubmission.started_at < running_deadline,
        )
        .limit(200)
    ).all()
    for row in stale_running:
        result = db.execute(
            update(PracticeSubmission)
            .execution_options(synchronize_session=False)
            .where(
                PracticeSubmission.id == row.id,
                PracticeSubmission.grading_status == "running",
                # 以认领 token 作栅栏：token 不受 MySQL DATETIME(0) 截断影响
                PracticeSubmission.claim_token.is_not_distinct_from(row.claim_token),
                PracticeSubmission.started_at < running_deadline,
            )
            .values(grading_status="queued", queued_at=now, claim_token=None,
                    last_error="Worker 超时未响应")
        )
        if result.rowcount == 0:
            db.rollback()
            continue
        db.commit()
        push_submission(redis_client, row.id, attempt=row.attempt_count)
        stats["running_reset"] += 1

    if any(stats.values()):
        logger.info("practice_recovery", extra=stats)
    return stats


def queue_depth(redis_client: Redis | None) -> int:
    """练习队列当前深度；异常返回 -1（调用方显式报告不可用）。"""
    try:
        client = redis_client if redis_client is not None else _get_redis()
        return int(client.llen(judge_queue_name()))
    except Exception:
        return -1


def running_count(db: Session) -> int:
    return int(db.scalar(
        select(func.count()).select_from(PracticeSubmission)
        .where(PracticeSubmission.grading_status == "running")
    ) or 0)


def queued_count(db: Session) -> int:
    return int(db.scalar(
        select(func.count()).select_from(PracticeSubmission)
        .where(PracticeSubmission.grading_status == "queued")
    ) or 0)


def _sanitize(error: str) -> str:
    import re

    error = re.sub(r"Bearer\s+\S+", "Bearer ***", error)
    error = re.sub(r"sk-[a-zA-Z0-9]+", "sk-***", error)
    return error[:1000]
