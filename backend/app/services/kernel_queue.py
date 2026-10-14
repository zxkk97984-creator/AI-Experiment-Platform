"""Durable kernel cell executions: database-authoritative claims, leases, recovery.

Redis is only a wake-up hint.  Every state transition (claim, heartbeat, finish,
cancel, recover) is a fenced CAS on ``kernel_executions`` so a crashed worker can
never overwrite the result of the worker that recovered its work.
"""
from __future__ import annotations

import logging
import socket
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import ExperimentRecord
from app.models.kernel_exec import KernelExecution

logger = logging.getLogger("dai.kernel_queue")

TERMINAL_STATUSES = {"succeeded", "timeout", "failed", "cancelled"}
ACTIVE_STATUSES = ("queued", "running", "recovering")
# 基础设施常量（非教师可配项）：租约 TTL 必须大于心跳间隔的 3 倍，且覆盖一次
# 无响应探测（约 8s）；崩溃后的恢复上限避免死循环重跑学生代码。
KERNEL_LEASE_SECONDS = 120
KERNEL_HEARTBEAT_SECONDS = 20
KERNEL_MAX_ATTEMPTS = 2
KERNEL_RECOVERY_LEASE_SECONDS = 60
KERNEL_RECOVERY_RETRY_SECONDS = 15


class LeaseLost(RuntimeError):
    pass


class ExecutionCancelled(RuntimeError):
    pass


def _owned(execution_id, token, now):
    return (KernelExecution.id == execution_id, KernelExecution.lease_token == token,
            KernelExecution.status == "running", KernelExecution.lease_until > now)


def execution_node_id(settings=None) -> str:
    node = (getattr(settings, "lab_worker_node_id", "") or "").strip() if settings else ""
    return node or socket.gethostname()[:80]


def publish_wakeup(settings, execution_id=None) -> None:
    """Redis 只做唤醒：发布失败绝不影响已落库的任务（worker 5s 轮询兜底）。"""
    try:
        import redis
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2,
                                      socket_timeout=2)
        client.publish("kernel-executions", str(execution_id or ""))
    except Exception:
        logger.debug("Kernel 执行唤醒发布失败", exc_info=True)


def assert_execution_owned(db, execution_id, token):
    now = db.scalar(select(func.now()))
    row = db.execute(select(KernelExecution.cancel_requested).where(
        *_owned(execution_id, token, now))).first()
    if row is None:
        raise LeaseLost("Kernel execution lease was lost")
    if row[0]:
        raise ExecutionCancelled("Kernel execution was cancelled")


def active_execution(db, record_id):
    """最新一条在途执行（刷新恢复状态查询用）。"""
    return db.scalar(
        select(KernelExecution)
        .where(KernelExecution.record_id == record_id,
               KernelExecution.status.in_(ACTIVE_STATUSES))
        .order_by(KernelExecution.created_at.desc(), KernelExecution.id.desc())
    )


def has_in_flight_execution(db, record_id) -> bool:
    """是否有在途执行——空闲回收前必须检查，正在执行的 kernel 不算空闲。"""
    return db.scalar(
        select(func.count()).select_from(KernelExecution).where(
            KernelExecution.record_id == record_id,
            KernelExecution.status.in_(ACTIVE_STATUSES),
        )
    ) > 0


def execution_read(db, execution: KernelExecution) -> dict:
    return {
        "id": execution.id,
        "record_id": execution.record_id,
        "cell_id": execution.cell_id,
        "status": execution.status,
        "outputs": execution.outputs or [],
        "execution_time_ms": execution.execution_time_ms,
        "execution_count": execution.execution_count,
        "error_code": execution.error_code,
        "error_message": execution.error_message,
        "state_lost": bool(execution.state_lost),
        "timed_out": execution.status == "timeout",
        "timeout_seconds": execution.timeout_seconds,
        "attempts": execution.attempts or 0,
        "cancel_requested": bool(execution.cancel_requested),
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
    }


def enqueue_execution(db, *, record_id, cell_id, code, timeout_seconds,
                      client_request_id=None, context=None, node_id=None):
    """写入一条 queued 执行；同 (record_id, client_request_id) 重复点击返回原行。

    返回 (execution, created)；仅当 created 为 True 时调用方才需要唤醒 worker。
    """
    if client_request_id:
        existing = db.scalar(select(KernelExecution).where(
            KernelExecution.record_id == record_id,
            KernelExecution.client_request_id == client_request_id,
        ))
        if existing is not None:
            return existing, False
    execution = KernelExecution(
        record_id=record_id,
        cell_id=cell_id,
        code=code,
        status="queued",
        client_request_id=client_request_id,
        timeout_seconds=int(timeout_seconds),
        context=context or {},
        outputs=[],
        attempts=0,
        cancel_requested=False,
        state_lost=False,
        node_id=node_id,
        started_at=None,
        finished_at=None,
    )
    db.add(execution)
    try:
        db.commit()
    except IntegrityError:
        # 并发双击：唯一约束挡下第二条，返回已存在的那条
        db.rollback()
        existing = db.scalar(select(KernelExecution).where(
            KernelExecution.record_id == record_id,
            KernelExecution.client_request_id == client_request_id,
        ))
        if existing is None:
            raise
        return existing, False
    db.refresh(execution)
    return execution, True


def queue_position(db, execution) -> int:
    """该执行在所属记录排队队列中的位置（1 = 下一个被执行）。"""
    if execution.status not in ACTIVE_STATUSES:
        return 0
    ahead = db.scalar(
        select(func.count()).select_from(KernelExecution).where(
            KernelExecution.record_id == execution.record_id,
            KernelExecution.status == "queued",
            KernelExecution.id < execution.id,
        )
    )
    return int(ahead or 0) + 1


def request_cancel(db, execution_id):
    """排队中直接取消；运行中只置 cancel_requested，由 worker 中断当前执行。"""
    changed = db.execute(update(KernelExecution).where(
        KernelExecution.id == execution_id,
        KernelExecution.status == "queued",
    ).values(cancel_requested=True, status="cancelled",
             finished_at=func.now()).execution_options(synchronize_session=False)).rowcount
    if not changed:
        db.execute(update(KernelExecution).where(
            KernelExecution.id == execution_id,
            KernelExecution.status == "running",
        ).values(cancel_requested=True).execution_options(synchronize_session=False))
    db.commit()
    return db.get(KernelExecution, execution_id, populate_existing=True)


def cancel_record_executions(db, record_id) -> int:
    """重启/重置内核前取消该记录的全部在途执行，返回受影响行数。"""
    now = db.scalar(select(func.now()))
    changed = db.execute(update(KernelExecution).where(
        KernelExecution.record_id == record_id,
        KernelExecution.status == "queued",
    ).values(cancel_requested=True, status="cancelled", finished_at=now)
    .execution_options(synchronize_session=False)).rowcount
    changed += db.execute(update(KernelExecution).where(
        KernelExecution.record_id == record_id,
        KernelExecution.status == "running",
    ).values(cancel_requested=True).execution_options(synchronize_session=False)).rowcount
    db.commit()
    return int(changed or 0)


@dataclass
class ClaimScanState:
    """Worker-local keyset progress. A fixed upper bound makes every pass finite."""
    after: tuple[datetime, int] | None = None
    high_water: tuple[datetime, int] | None = None

    def reset(self):
        self.after = self.high_water = None


def claim_execution(db, settings=None, scan: ClaimScanState | None = None):
    progress = replace(scan) if scan is not None else ClaimScanState()
    try:
        result = _claim_execution(db, settings, progress)
        if scan is not None:
            scan.after, scan.high_water = progress.after, progress.high_water
        return result
    except OperationalError as exc:
        # 并发首插/锁竞争在 InnoDB 上是「被别人抢赢」，不是工作丢失
        code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
        if code not in (1205, 1213):
            raise
        db.rollback()
        return None


def _claim_execution(db, settings, scan):
    """认领最旧任务；父记录锁串行化执行与 worker-loss 恢复。"""
    now = db.scalar(select(func.now()))
    query = select(KernelExecution).where(KernelExecution.status == "queued")
    if scan.high_water is None:
        last = db.execute(query.with_only_columns(KernelExecution.created_at, KernelExecution.id)
                          .order_by(KernelExecution.created_at.desc(), KernelExecution.id.desc())
                          .limit(1)).first()
        if last is None:
            scan.reset()
            db.commit()
            return None
        scan.high_water = tuple(last)
    key = tuple_(KernelExecution.created_at, KernelExecution.id)
    query = query.where(key <= scan.high_water)
    if scan.after is not None:
        query = query.where(key > scan.after)
    candidates = list(db.scalars(query.order_by(KernelExecution.created_at, KernelExecution.id)
                                 .limit(100)))
    if not candidates:
        scan.reset()
    claimed_records: set[int] = set()
    for execution in candidates:
        scan.after = (execution.created_at, execution.id)
        if scan.after >= scan.high_water:
            scan.reset()
        if execution.record_id in claimed_records:
            continue
        # All claim/recovery transitions lock parent then execution row. A
        # recovering cell therefore blocks its siblings until stop is confirmed.
        record_exists = db.scalar(select(ExperimentRecord.id).where(
            ExperimentRecord.id == execution.record_id).with_for_update())
        if record_exists is None:
            continue
        active_id = db.scalar(select(KernelExecution.id).where(
            KernelExecution.record_id == execution.record_id,
            KernelExecution.status.in_(("running", "recovering")),
        ).order_by(KernelExecution.id).limit(1).with_for_update())
        if active_id is not None:
            continue
        row = db.get(KernelExecution, execution.id, populate_existing=True)
        if row is None or row.status != "queued":
            continue
        if row.cancel_requested:
            changed = db.execute(update(KernelExecution).where(
                KernelExecution.id == row.id,
                KernelExecution.status == "queued",
                KernelExecution.cancel_requested.is_(True),
            ).values(status="cancelled", finished_at=now)
            .execution_options(synchronize_session=False)).rowcount
            if changed:
                db.commit()
            else:
                db.rollback()
            continue
        token = str(uuid.uuid4())
        changed = db.execute(update(KernelExecution).where(
            KernelExecution.id == row.id,
            KernelExecution.status == "queued",
            KernelExecution.cancel_requested.is_(False),
        ).values(
            status="running", lease_token=token,
            lease_until=now + timedelta(seconds=KERNEL_LEASE_SECONDS),
            attempts=KernelExecution.attempts + 1,
            started_at=now, finished_at=None,
            error_code=None, error_message=None, state_lost=False,
            node_id=execution_node_id(settings),
        ).execution_options(synchronize_session=False)).rowcount
        if not changed:
            db.rollback()
            continue
        claimed_records.add(execution.record_id)
        db.commit()
        return row.id, token
    db.commit()
    return None


def heartbeat_execution(db, execution_id, token, *, lease_seconds=KERNEL_LEASE_SECONDS):
    now = db.scalar(select(func.now()))
    changed = db.execute(update(KernelExecution).where(*_owned(execution_id, token, now)).values(
        lease_until=now + timedelta(seconds=lease_seconds),
    ).execution_options(synchronize_session=False)).rowcount
    db.commit()
    return changed == 1


def finish_execution(db, execution_id, token, status, *, outputs=None,
                     execution_time_ms=None, execution_count=None,
                     error_code=None, error_message=None, state_lost=False):
    """带 token 的终态 CAS；调用方在同一事务里写 record.cells_outputs 后 commit。"""
    if status not in TERMINAL_STATUSES:
        raise ValueError("Invalid terminal kernel execution status")
    now = db.scalar(select(func.now()))
    clauses = list(_owned(execution_id, token, now))
    if status != "cancelled":
        clauses.append(KernelExecution.cancel_requested.is_(False))
    values = dict(status=status, lease_until=None, finished_at=now,
                  error_code=error_code, error_message=error_message,
                  state_lost=bool(state_lost))
    if outputs is not None:
        values["outputs"] = outputs
    if execution_time_ms is not None:
        values["execution_time_ms"] = execution_time_ms
    if execution_count is not None:
        values["execution_count"] = execution_count
    changed = db.execute(update(KernelExecution).where(*clauses).values(
        **values).execution_options(synchronize_session=False)).rowcount
    return changed == 1


def recover_executions(db, settings=None):
    """回收失联 worker，但绝不自动重放学生代码。

    先用新的恢复令牌接管过期任务，再确认旧执行已停止。无法确认时保留
    ``recovering`` 并短暂退避；确认停止后以 WORKER_LOST 终止本次执行。
    """
    now = db.scalar(select(func.now()))
    stale = list(db.execute(select(
        KernelExecution.id, KernelExecution.lease_token, KernelExecution.record_id,
        KernelExecution.status, KernelExecution.cancel_requested,
    ).where(
        ((KernelExecution.status == "running") & (KernelExecution.lease_until <= now)) |
        ((KernelExecution.status == "recovering") &
         (KernelExecution.lease_until.is_(None) | (KernelExecution.lease_until <= now))),
    ).limit(100)))
    db.rollback()
    recovered = 0
    for execution_id, token, record_id, status, _cancel_requested in stale:
        now = db.scalar(select(func.now()))
        record_exists = db.scalar(select(ExperimentRecord.id).where(
            ExperimentRecord.id == record_id).with_for_update())
        if record_exists is None:
            db.rollback()
            continue
        recovery_token = str(uuid.uuid4())
        eligible = (
            ((KernelExecution.status == "running") & (KernelExecution.lease_until <= now))
            if status == "running" else
            (KernelExecution.status == "recovering") &
            (KernelExecution.lease_until.is_(None) | (KernelExecution.lease_until <= now))
        )
        fenced = db.execute(update(KernelExecution).where(
            KernelExecution.id == execution_id,
            KernelExecution.lease_token == token,
            eligible,
        ).values(status="recovering", lease_token=recovery_token,
                 lease_until=now + timedelta(seconds=KERNEL_RECOVERY_LEASE_SECONDS),
                 error_code="RECOVERY_IN_PROGRESS",
                 error_message="正在确认旧内核执行已停止。"
                 ).execution_options(synchronize_session=False)).rowcount
        db.commit()
        if not fenced:
            db.rollback()
            continue
        try:
            stopped, state_lost = _interrupt_orphan(record_id)
        except Exception:
            logger.exception("确认 kernel record=%s 的旧执行停止失败", record_id)
            stopped, state_lost = False, False
        if not stopped:
            retry_at = db.scalar(select(func.now())) + timedelta(seconds=KERNEL_RECOVERY_RETRY_SECONDS)
            db.execute(update(KernelExecution).where(
                KernelExecution.id == execution_id,
                KernelExecution.lease_token == recovery_token,
                KernelExecution.status == "recovering",
            ).values(lease_until=retry_at, error_code="KERNEL_RECOVERY_BLOCKED",
                     error_message="无法确认旧内核已停止，后续单元格暂不执行。"
                     ).execution_options(synchronize_session=False))
            db.commit()
            continue

        row = db.get(KernelExecution, execution_id, populate_existing=True)
        cancel_requested = bool(row.cancel_requested) if row is not None else False
        terminal_status = "cancelled" if cancel_requested else "failed"
        values = dict(status=terminal_status, lease_until=None, lease_token=None,
                      error_code=None if cancel_requested else "WORKER_LOST",
                      error_message=None if cancel_requested else
                      ("执行服务中断，本次运行未完成；变量可能已经部分改变，请检查后重新运行。"
                       if not state_lost else
                       "执行服务中断，内核已重建且变量已清空；请重新运行初始化及相关单元格。"),
                      state_lost=bool(state_lost), finished_at=func.now())
        changed = db.execute(update(KernelExecution).where(
            KernelExecution.id == execution_id,
            KernelExecution.lease_token == recovery_token,
            KernelExecution.status == "recovering",
        ).values(**values).execution_options(synchronize_session=False)).rowcount
        db.commit()
        if changed:
            recovered += 1
    return recovered


def _interrupt_orphan(record_id):
    """中断失联执行并确认内核可用；必要时销毁无响应内核。

    返回 ``(stopped, state_lost)``。返回 stopped=False 时，调用方必须继续保留
    recovering 栅栏及资源租约。
    """
    try:
        from app.services.kernel_manager import get_kernel_manager
        from app.database import SessionLocal
        return get_kernel_manager().stop_execution_after_worker_loss(
            record_id, resource_factory=SessionLocal)
    except Exception:
        logger.warning("恢复执行时无法中断 kernel record=%s 的残留执行", record_id, exc_info=True)
        return False, False


class KernelLeaseGuard:
    """独立线程续租执行租约；取消请求通过 cancel_event 交给执行器。

    与 LabRun 的 LeaseGuard 同构：worker 崩溃时租约自然过期，恢复逻辑接管，
    旧 worker 的任何写入都会被 token 栅栏拒绝。
    """

    def __init__(self, session_factory, execution_id, token, *,
                 lease_seconds=KERNEL_LEASE_SECONDS, interval_seconds=None,
                 on_cancel=None):
        self.factory = session_factory
        self.execution_id = execution_id
        self.token = token
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds or max(
            0.5, min(KERNEL_HEARTBEAT_SECONDS, lease_seconds / 3))
        self.on_cancel = on_cancel
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.cancel_event = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True,
                                       name=f"kernel-lease-{execution_id}")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=15)

    def assert_owned(self):
        if self.lost_event.is_set():
            raise LeaseLost("Kernel execution lease was lost")
        with self.factory() as db:
            assert_execution_owned(db, self.execution_id, self.token)

    def _lost(self):
        self.lost_event.set()
        self.cancel_event.set()
        # fail-closed：尽快中断当前执行，避免孤儿代码继续跑
        try:
            if self.on_cancel is not None:
                self.on_cancel()
        except Exception:
            logger.warning("Kernel execution %s 失去租约后的中断失败", self.execution_id,
                           exc_info=True)

    def _heartbeat(self):
        while not self.stop_event.wait(self.interval_seconds):
            try:
                with self.factory() as db:
                    now = db.scalar(select(func.now()))
                    row = db.execute(select(KernelExecution.cancel_requested).where(
                        *_owned(self.execution_id, self.token, now))).first()
                    if row is None:
                        self._lost()
                        return
                    if row[0]:
                        self.cancel_event.set()
                    if not heartbeat_execution(db, self.execution_id, self.token,
                                               lease_seconds=self.lease_seconds):
                        self._lost()
                        return
            except Exception:
                logger.exception("Kernel 执行 %s 心跳失败", self.execution_id)
                self._lost()
                return
