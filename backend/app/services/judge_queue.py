"""统一判题入队服务——所有判题任务的唯一入队入口。

保证：
- 数据库是判题状态的唯一事实源，Redis 只负责唤醒 Worker
- 条件 UPDATE 抢占状态，杜绝先读后写
- 重复入队只产生一条有效任务
- Redis 不可用时任务保留在 DB queued，由恢复扫描重新推送
"""

import json as _json
import logging
from datetime import datetime, timezone

import redis as _redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ExamAnswer, ExamSubmission, Submission

logger = logging.getLogger("dai.judge_queue")

# Redis 消息格式（v2 统一协议）
# {"type": "assignment" | "exam", "id": 123, "attempt": 1}

JUDGE_QUEUE = "judge:queue"
EXAM_JUDGE_QUEUE = "judge:exam:queue"

# 自动入队只接受 pending——system_error 是终态，不得自动复活；
# 显式受控重试（如考试答案修复后）由调用方先重置为 pending 再入队。
RETRYABLE_STATUSES = ["pending"]

# 最大重试次数
MAX_ATTEMPTS = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_redis():
    """获取 Redis 客户端（每次调用创建，避免连接跨 fork 问题）"""
    settings = get_settings()
    return _redis.Redis.from_url(
        settings.redis_url, decode_responses=False,
        socket_connect_timeout=2, socket_timeout=3,
    )


def enqueue_job(db: Session, *, job_type: str, object_id: int) -> bool:
    """唯一入队入口：条件 UPDATE 将 pending 转为 queued，然后推送 Redis。

    system_error 是终态，不在此自动入队（避免配置错误无限重试）；需要重试时
    由显式受控入口（如 retry_exam_submission）先把任务重置为 pending 再调用本函数。

    参数：
        db: 数据库会话
        job_type: "assignment" 或 "exam"
        object_id: Submission.id 或 ExamAnswer.id

    返回：
        True  — 入队成功（DB 状态已更新 + Redis 消息已发送）
        False — 条件更新未命中（已是 queued/running/completed/system_error，重复入队被忽略）

    竞争安全：
        使用 UPDATE ... WHERE grading_status = 'pending' 实现原子抢占。
        多个 Worker/API 并发调用时，只有一个能成功更新行。
    """
    if job_type == "assignment":
        model = Submission
        queue_key = JUDGE_QUEUE
    elif job_type == "exam":
        model = ExamAnswer
        queue_key = EXAM_JUDGE_QUEUE
    else:
        raise ValueError(f"不支持的 job_type: {job_type}")

    target_statuses = RETRYABLE_STATUSES
    now = _utc_now()

    # 1. 条件 UPDATE 抢占状态：原子操作，杜绝竞态
    result = db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(
            model.id == object_id,
            model.grading_status.in_(target_statuses),
        )
        .values(
            grading_status="queued",
            attempt_count=model.attempt_count + 1,
            queued_at=now,
            last_error=None,
        )
    )

    if result.rowcount == 0:
        # 未命中：状态不是 pending（可能已 queued/running/completed/system_error；
        # system_error 是终态，不得自动复活）
        db.commit()  # 确保事务结束
        return False

    db.commit()

    db.expire_all()  # core UPDATE 不同步会话，刷新对象

    # 2. DB 状态已持久化，推送 Redis 消息
    attempt = _get_current_attempt(db, job_type, object_id)
    message = _json.dumps({"type": job_type, "id": object_id, "attempt": attempt})

    try:
        r = _get_redis()
        r.rpush(queue_key, message)
    except Exception as exc:
        # Redis 不可用：任务已在 DB 中标记为 queued，恢复扫描会重新入队
        logger.warning("Redis 不可用，任务 %s:%s 留在 queued 状态等待恢复扫描: %s",
                       job_type, object_id, exc)
        return True  # DB 状态已正确更新，视为成功

    return True


def _get_current_attempt(db: Session, job_type: str, object_id: int) -> int:
    """读取当前 attempt_count（在 commit 后调用）"""
    if job_type == "assignment":
        row = db.get(Submission, object_id)
    else:
        row = db.get(ExamAnswer, object_id)
    return row.attempt_count if row else 1


def claim_job(db: Session, *, job_type: str, object_id: int) -> bool:
    """Worker 领取任务：条件 UPDATE queued → running。

    抢占失败（rowcount=0）说明消息重复或已被其他 Worker 领取，应确认消息并跳过。

    返回 True 表示抢占成功。
    """
    if job_type == "assignment":
        model = Submission
    elif job_type == "exam":
        model = ExamAnswer
    else:
        raise ValueError(f"不支持的 job_type: {job_type}")

    now = _utc_now()
    result = db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(
            model.id == object_id,
            model.grading_status == "queued",
        )
        .values(
            grading_status="running",
            started_at=now,
        )
    )

    db.commit()

    db.expire_all()  # core UPDATE 不同步会话，刷新对象
    return result.rowcount > 0


def complete_job(db: Session, *, job_type: str, object_id: int,
                 score: float | None = None, result_details: dict | None = None) -> None:
    """Worker 成功完成判题：running → completed，写入分数和结果。"""
    if job_type == "assignment":
        model = Submission
    elif job_type == "exam":
        model = ExamAnswer
    else:
        raise ValueError(f"不支持的 job_type: {job_type}")

    now = _utc_now()
    values = {
        "grading_status": "completed",
        "finished_at": now,
    }
    if score is not None:
        values["score"] = score
    if result_details is not None:
        values["result_details"] = result_details

    db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(model.id == object_id, model.grading_status == "running")
        .values(**values)
    )
    db.commit()
    db.expire_all()  # core UPDATE 不同步会话，刷新对象


def fail_job(db: Session, *, job_type: str, object_id: int,
             error: str, retryable: bool = False) -> None:
    """判题失败处理。

    retryable=True: 退回 pending 状态（Worker 崩溃、临时错误），等待恢复扫描重试
    retryable=False: 写入 system_error 终态

    状态 CAS：只有仍为 running（当前 Worker 认领）的任务才能 fail——
    避免旧 Worker 的失败覆盖新 Worker 已完成的判题结果。
    """
    if job_type == "assignment":
        model = Submission
    elif job_type == "exam":
        model = ExamAnswer
    else:
        raise ValueError(f"不支持的 job_type: {job_type}")

    now = _utc_now()

    if retryable:
        # 退回 pending，由恢复扫描重新入队
        result = db.execute(
            update(model).execution_options(synchronize_session=False)
            .where(model.id == object_id, model.grading_status == "running")
            .values(
                grading_status="pending",
                last_error=error,
                finished_at=now,
            )
        )
    else:
        # 终态 system_error
        result = db.execute(
            update(model).execution_options(synchronize_session=False)
            .where(model.id == object_id, model.grading_status == "running")
            .values(
                grading_status="system_error",
                last_error=error,
                finished_at=now,
            )
        )
    if result.rowcount == 0:
        db.rollback()
        return  # 已被并发 Worker 处理，不覆盖
    db.commit()
    db.expire_all()  # core UPDATE 不同步会话，刷新对象


def requeue_stale_jobs(db: Session, *, job_type: str | None = None,
                       stale_pending_seconds: int = 60,
                       stale_queued_seconds: int = 120,
                       stale_running_seconds: int = 300) -> dict:
    """恢复扫描：处理卡住的 pending / queued / running 任务（多实例 CAS 安全）。

    每次状态变更都带旧状态/旧时间阈值做条件 UPDATE，rowcount=1 才算真实转换、
    才 rpush Redis 与计数——多实例同一轮扫描最多一个实例成功。

    返回统计字典：{"pending_requeued": N, "queued_repushed": N, "running_reset": N, "max_retries_reached": N}
    """
    from datetime import timedelta as _td
    now = _utc_now()
    stats = {"pending_requeued": 0, "queued_repushed": 0, "running_reset": 0, "max_retries_reached": 0}

    models_to_scan = []
    if job_type in (None, "assignment"):
        models_to_scan.append(("assignment", Submission, JUDGE_QUEUE))
    if job_type in (None, "exam"):
        models_to_scan.append(("exam", ExamAnswer, EXAM_JUDGE_QUEUE))

    for jt, model, queue_key in models_to_scan:
        def _recovery_query():
            query = select(model)
            if jt == "exam":
                # Only answers belonging to a grading submission are executable.
                # Submitted/review_required answers can intentionally remain pending
                # as historical snapshots; recovering them would send non-code
                # answers through the code judge and create false system_error rows.
                query = query.join(
                    ExamSubmission,
                    ExamSubmission.id == ExamAnswer.submission_id,
                ).where(ExamSubmission.status == "grading")
            return query

        # pending 超时 → 重新入队（CAS：仍 pending + 未达上限 + 超时）
        # populate_existing：CAS 令牌（started_at/queued_at）必须读库内实际值——
        # 同会话 identity map 里的对象可能带微秒，而 MySQL DATETIME(0) 入库已截断，
        # 直接绑回会导致条件永不命中（running_reset=0 的根因）。
        pending_deadline = now - _td(seconds=stale_pending_seconds)
        pending_jobs = db.scalars(
            _recovery_query()
            .execution_options(populate_existing=True)
            .where(
                model.grading_status == "pending",
                model.attempt_count < MAX_ATTEMPTS,
                model.created_at < pending_deadline,
            )
        ).all()
        for job in pending_jobs:
            if _do_enqueue(db, model, job.id, queue_key):
                stats["pending_requeued"] += 1

        # 超过最大重试 → system_error（不扣分：基础设施问题不应让学生承担）
        over_max = db.scalars(
            _recovery_query().where(
                model.grading_status == "pending",
                model.attempt_count >= MAX_ATTEMPTS,
            )
        ).all()
        for job in over_max:
            values = {
                "grading_status": "system_error",
                "last_error": f"超过最大重试次数（{MAX_ATTEMPTS}）",
                "finished_at": now,
                "score": None,
            }
            # 普通作业：同步更新 status（前端轮询字段）
            if jt == "assignment":
                values["status"] = "system_error"
            result = db.execute(
                update(model).execution_options(synchronize_session=False)
                .where(
                    model.id == job.id,
                    model.grading_status == "pending",
                )
                .values(**values)
            )
            if result.rowcount == 0:
                continue  # 已被并发实例转换
            db.commit()
            db.expire_all()  # core UPDATE 不同步会话，刷新对象
            stats["max_retries_reached"] += 1
            # 考试答案：父级当场转 review_required，不等 5 分钟 scanner
            if jt == "exam":
                submission_id = job.submission_id
                from app.services.exam_grading import finalize_if_ready
                finalize_if_ready(submission_id, db)

        # queued 超时 → 重新推送 Redis（消息可能丢失；CAS 更新 queued_at 防重复推送）
        queued_deadline = now - _td(seconds=stale_queued_seconds)
        queued_jobs = db.scalars(
            _recovery_query()
            .execution_options(populate_existing=True)
            .where(
                model.grading_status == "queued",
                model.queued_at < queued_deadline,
            )
        ).all()
        for job in queued_jobs:
            # 只有 CAS 成功（仍 queued 且 queued_at 未变）的实例才推送
            result = db.execute(
                update(model).execution_options(synchronize_session=False)
                .where(
                    model.id == job.id,
                    model.grading_status == "queued",
                    model.queued_at == job.queued_at,
                )
                .values(queued_at=now)
            )
            if result.rowcount == 0:
                continue
            db.commit()
            db.expire_all()  # core UPDATE 不同步会话，刷新对象
            try:
                attempt = job.attempt_count
                message = _json.dumps({"type": jt, "id": job.id, "attempt": attempt})
                r = _get_redis()
                r.rpush(queue_key, message)
                stats["queued_repushed"] += 1
            except Exception:
                logger.warning("重新推送 queued 任务失败: %s:%s", jt, job.id)

        # running 超时 → 重置为 pending（Worker 崩溃；CAS：仍 running 且 started_at 未变）
        running_deadline = now - _td(seconds=stale_running_seconds)
        running_jobs = db.scalars(
            _recovery_query()
            .execution_options(populate_existing=True)
            .where(
                model.grading_status == "running",
                model.started_at < running_deadline,
            )
        ).all()
        for job in running_jobs:
            result = db.execute(
                update(model).execution_options(synchronize_session=False)
                .where(
                    model.id == job.id,
                    model.grading_status == "running",
                    model.started_at == job.started_at,
                )
                .values(
                    grading_status="pending",
                    last_error="Worker 超时未响应（stale running）",
                    finished_at=now,
                )
            )
            if result.rowcount == 0:
                continue
            db.commit()
            db.expire_all()  # core UPDATE 不同步会话，刷新对象
            stats["running_reset"] += 1

    return stats


def _do_enqueue(db: Session, model, object_id: int, queue_key: str) -> bool:
    """内部：条件更新 + Redis 推送"""
    now = _utc_now()
    result = db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(
            model.id == object_id,
            model.grading_status == "pending",
        )
        .values(
            grading_status="queued",
            attempt_count=model.attempt_count + 1,
            queued_at=now,
            last_error=None,
        )
    )
    if result.rowcount == 0:
        db.rollback()  # 多实例：未命中必须结束事务，防止脏读残留
        return False
    db.commit()
    db.expire_all()  # core UPDATE 不同步会话，刷新对象

    attempt = db.get(model, object_id).attempt_count
    message = _json.dumps(
        {"type": "assignment" if model is Submission else "exam",
         "id": object_id, "attempt": attempt}
    )
    try:
        r = _get_redis()
        r.rpush(queue_key, message)
    except Exception:
        pass
    return True
