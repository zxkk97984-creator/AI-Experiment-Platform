"""统一判题入队服务——所有判题任务的唯一入队入口。

保证：
- 数据库是判题状态的唯一事实源，Redis 只负责唤醒 Worker
- 条件 UPDATE 抢占状态，杜绝先读后写
- 重复入队只产生一条有效任务
- Redis 不可用时任务保留在 DB queued，由恢复扫描重新推送
"""

import json as _json
import logging
import secrets as _secrets
from datetime import datetime, timezone

import redis as _redis
from sqlalchemy import func, select, update
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
        queue_key = get_settings().judge_queue_name
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


def _model_for(job_type: str):
    if job_type == "assignment":
        return Submission
    if job_type == "exam":
        return ExamAnswer
    raise ValueError(f"不支持的 job_type: {job_type}")


def _claim_key(job_type: str, object_id: int) -> tuple[str, int]:
    return (job_type, object_id)


def _remember_claim(db: Session, job_type: str, object_id: int, token: str) -> None:
    """把本次认领的 token 记在会话上，供同一会话内的结果写入做栅栏条件。

    使用 db.info 而非逐层传参：判题失败分支有 20+ 个调用点，全部改签名既啰嗦又容易漏改，
    而"这次会话认领的是哪个 token"本就是会话级上下文（仓库既有 teaching_token_* 同样用 db.info）。
    """
    claims = db.info.setdefault("judge_claims", {})
    claims[_claim_key(job_type, object_id)] = token


def current_claim_token(db: Session, job_type: str, object_id: int) -> str | None:
    """取本会话对某任务的认领 token；没有则说明本会话未认领（写入必须被拒绝）。"""
    return (db.info.get("judge_claims") or {}).get(_claim_key(job_type, object_id))


def _forget_claim(db: Session, job_type: str, object_id: int) -> None:
    claims = db.info.get("judge_claims")
    if claims:
        claims.pop(_claim_key(job_type, object_id), None)


def claim_job(db: Session, *, job_type: str, object_id: int,
              extra_values: dict | None = None) -> str | None:
    """Worker 领取任务：条件 UPDATE queued → running，并铸造本次认领的 token。

    返回 token 表示认领成功（同一会话随后的结果写入必须带它）；
    返回 None 表示抢占失败（消息重复、已被其他 Worker 领取、或已被恢复机制重新排队），
    调用方应确认消息并跳过。

    extra_values 用于与认领原子落地的展示字段（如 Submission.status="running"），
    避免出现"无栅栏的状态写入"。

    过期消息不会抢走属于更新代次的任务：抢占条件含 queued 状态，
    因此处于 running 的行不会被第二次认领。
    """
    model = _model_for(job_type)
    token = _secrets.token_urlsafe(32)
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
            heartbeat_at=now,
            claim_token=token,
            **(extra_values or {}),
        )
    )

    db.commit()

    db.expire_all()  # core UPDATE 不同步会话，刷新对象
    if result.rowcount == 0:
        return None
    _remember_claim(db, job_type, object_id, token)
    return token


def apply_guarded_result(db: Session, *, job_type: str, object_id: int, values: dict) -> bool:
    """底层事务内路径：执行受认领保护的终态 UPDATE，**不 commit、不 rollback**。

    供评分服务把"主结果 + 后续评分记录 + 来源证据"合成同一个事务时使用；调用方
    必须自己决定提交或回滚。若返回 False（已失去认领），调用方应立即 rollback 并停止，
    不得继续创建评分记录或推送队列通知。
    """
    model = _model_for(job_type)
    token = current_claim_token(db, job_type, object_id)
    if not token:
        logger.warning("拒绝无认领的结果写入: %s:%s", job_type, object_id)
        return False

    now = _utc_now()
    payload = dict(values)
    payload.setdefault("finished_at", now)
    payload["claim_token"] = None
    payload["heartbeat_at"] = None
    # 结果修订号与结果本身在同一事务内原子推进（人工改分/客户端据此判断过时写入）
    payload["result_revision"] = model.result_revision + 1

    result = db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(
            model.id == object_id,
            model.grading_status == "running",
            model.claim_token == token,
        )
        .values(**payload)
    )
    if result.rowcount == 0:
        logger.warning(
            "判题结果被栅栏拒绝（已失去认领，结果丢弃）: %s:%s", job_type, object_id
        )
        return False
    return True


def finish_job(db: Session, *, job_type: str, object_id: int, values: dict) -> bool:
    """**唯一**的判题终态写入入口：确认仍持有认领 + 写入全部结果 + 置终态，一次条件 UPDATE。

    栅栏：`WHERE id = ? AND grading_status = 'running' AND claim_token = <本次认领的 token>`。
    失去认领（恢复机制已重新排队并由别的 Worker 接管）的旧 Worker 因此无法覆盖新结果——
    这正是"旧任务迟到写回"的根因修复：原先 Worker 先用普通 ORM 赋值提交结果，
    再调用只以 running 为条件的 complete_job，两者之间没有任何代次校验。

    values 必须包含目标 grading_status 以及所有要落库的结果字段
    （score/status/result_details/tests_passed/tests_total/stdout/stderr/execution_time_ms 等），
    使"确认写入资格"与"写结果"在同一事务内原子完成。

    rowcount=0（已失去认领）→ 回滚、WARNING、返回 False；**不重试**，由恢复机制兜底重排。
    需要把 CodeGrade 等后续记录并入同一事务时，改用评分服务的收尾操作
    （它复用 ``apply_guarded_result``）。
    """
    if not apply_guarded_result(db, job_type=job_type, object_id=object_id, values=values):
        db.rollback()
        return False
    db.commit()
    db.expire_all()  # core UPDATE 不同步会话，刷新对象
    _forget_claim(db, job_type, object_id)
    return True


def commit_guarded_result(db: Session, *, job_type: str, object_id: int) -> None:
    """提交已由 ``apply_guarded_result`` 写入的结果并释放本会话的认领记录。"""
    db.commit()
    db.expire_all()
    _forget_claim(db, job_type, object_id)


def complete_job(db: Session, *, job_type: str, object_id: int,
                 score: float | None = None, result_details: dict | None = None,
                 values: dict | None = None) -> bool:
    """成功完成判题：running → completed，并写入分数与结果（受认领栅栏保护）。"""
    payload = dict(values or {})
    payload["grading_status"] = "completed"
    if score is not None:
        payload["score"] = score
    if result_details is not None:
        payload["result_details"] = result_details
    return finish_job(db, job_type=job_type, object_id=object_id, values=payload)


def heartbeat_job(db: Session, *, job_type: str, object_id: int) -> bool:
    """续期存活标记（长判题在测试组之间调用）。

    以认领 token 为条件：失去认领的续期与失去认领的结果一样被静默忽略。
    """
    model = _model_for(job_type)
    token = current_claim_token(db, job_type, object_id)
    if not token:
        return False
    result = db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(
            model.id == object_id,
            model.grading_status == "running",
            model.claim_token == token,
        )
        .values(heartbeat_at=_utc_now())
    )
    if result.rowcount == 0:
        db.rollback()
        return False
    db.commit()
    return True


def fail_job(db: Session, *, job_type: str, object_id: int,
             error: str, retryable: bool = False,
             values: dict | None = None) -> bool:
    """判题失败处理（受认领栅栏保护）。

    retryable=True → 退回 pending，等待恢复扫描重试；
    retryable=False → 写入 system_error 终态。

    只有仍持有认领（claim_token 匹配）的 Worker 才能失败当前任务——
    避免旧 Worker 的失败覆盖新 Worker 已完成的判题结果。
    """
    model = _model_for(job_type)
    payload = dict(values or {})
    payload["grading_status"] = "pending" if retryable else "system_error"
    payload["last_error"] = error
    # retryable 失败会重新排队，必须释放代次：下一次认领会铸造新 token。
    # 这里不能走 finish_job（它会把 claim_token 置 NULL 并写 finished_at），
    # 因为"退回 pending"不是终态，但栅栏条件完全一致。
    token = current_claim_token(db, job_type, object_id)
    if not token:
        logger.warning("拒绝无认领的失败写入: %s:%s", job_type, object_id)
        db.rollback()
        return False

    now = _utc_now()
    payload.setdefault("finished_at", now)
    payload["claim_token"] = None
    payload["heartbeat_at"] = None
    if not retryable:
        # 终态失败（system_error / 学生错误）也是结果状态变化，原子推进修订号；
        # 可重试失败只是退回 pending，不产生结果，因此不推进。
        payload["result_revision"] = model.result_revision + 1
    result = db.execute(
        update(model).execution_options(synchronize_session=False)
        .where(
            model.id == object_id,
            model.grading_status == "running",
            model.claim_token == token,
        )
        .values(**payload)
    )
    if result.rowcount == 0:
        db.rollback()
        logger.warning("判题失败写入被栅栏拒绝（已失去认领）: %s:%s", job_type, object_id)
        return False
    db.commit()
    db.expire_all()  # core UPDATE 不同步会话，刷新对象
    _forget_claim(db, job_type, object_id)
    return True


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
        models_to_scan.append(("assignment", Submission, get_settings().judge_queue_name))
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

        # running 超时 → 重置为 pending（Worker 崩溃或已失去认领）
        # 存活判定用 heartbeat_at（判题在测试组之间续期），未续期则回退 started_at。
        # 栅栏用 claim_token 而非 started_at：token 是本次认领的随机值，
        # 不受 MySQL DATETIME(0) 截断影响（后者是 running_reset 长期为 0 的根因）。
        running_deadline = now - _td(seconds=stale_running_seconds)
        running_jobs = db.scalars(
            _recovery_query()
            .execution_options(populate_existing=True)
            .where(
                model.grading_status == "running",
                func.coalesce(model.heartbeat_at, model.started_at) < running_deadline,
            )
        ).all()
        for job in running_jobs:
            result = db.execute(
                update(model).execution_options(synchronize_session=False)
                .where(
                    model.id == job.id,
                    model.grading_status == "running",
                    # NULL 安全比较：旧代码认领或迁移前的行 claim_token 为 NULL，
                    # 用 `==` 会得到 NULL 而非 TRUE，导致这些行永远无法被恢复。
                    model.claim_token.is_not_distinct_from(job.claim_token),
                    # 代次之外还要复核"实际仍然过期"：SELECT 与 UPDATE 之间任务可能已续
                    # 心跳（多测试组的长判题持续 heartbeat），只比 token 会把活着的任务重置。
                    func.coalesce(model.heartbeat_at, model.started_at) < running_deadline,
                )
                .values(
                    grading_status="pending",
                    last_error="Worker 超时未响应（stale running）",
                    finished_at=now,
                    # 释放代次：下一次认领铸造新 token，旧 Worker 的结果写入随即失效
                    claim_token=None,
                    heartbeat_at=None,
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
