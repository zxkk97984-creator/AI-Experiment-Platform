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

from app.models.kernel_exec import KernelExecution

logger = logging.getLogger("dai.kernel_queue")

TERMINAL_STATUSES = {"succeeded", "timeout", "failed", "cancelled"}
ACTIVE_STATUSES = ("queued", "running", "recovering")
# 基础设施常量（非教师可配项）：租约 TTL 必须大于心跳间隔的 3 倍，且覆盖一次
# 无响应探测（约 8s）；崩溃后的恢复上限避免死循环重跑学生代码。
KERNEL_LEASE_SECONDS = 120
KERNEL_HEARTBEAT_SECONDS = 20
KERNEL_MAX_ATTEMPTS = 2


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
    """认领最旧的可执行任务；同一记录同时只允许一个执行在跑。"""
    now = db.scalar(select(func.now()))
    running_records = select(KernelExecution.record_id).where(
        KernelExecution.status == "running")
    query = select(KernelExecution).where(
        KernelExecution.status == "queued",
        KernelExecution.record_id.not_in(running_records),
    )
    if scan.high_water is None:
        last = db.execute(query.with_only_columns(KernelExecution.created_at, KernelExecution.id)
                          .order_by(KernelExecution.created_at.desc(), KernelExecution.id.desc())
                          .limit(1).with_for_update()).first()
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
                                 .limit(100).with_for_update()
                                 .execution_options(populate_existing=True)))
    if not candidates:
        scan.reset()
    claimed_records: set[int] = set()
    for execution in candidates:
        scan.after = (execution.created_at, execution.id)
        if scan.after >= scan.high_water:
            scan.reset()
        if execution.record_id in claimed_records:
            continue
        if execution.cancel_requested:
            execution.status = "cancelled"
            execution.finished_at = now
            continue
        token = str(uuid.uuid4())
        execution.status = "running"
        execution.lease_token = token
        execution.lease_until = now + timedelta(seconds=KERNEL_LEASE_SECONDS)
        execution.attempts = (execution.attempts or 0) + 1
        execution.started_at = now
        execution.finished_at = None
        execution.error_code = None
        execution.error_message = None
        execution.state_lost = False
        execution.node_id = execution_node_id(settings)
        claimed_records.add(execution.record_id)
        db.commit()
        return execution.id, token
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
    """租约过期的执行：先中断可能的残留执行，再重排队或（超过上限）落 failed。

    与 lab 不同，这里绝不销毁学生内核——内存变量是学习状态；恢复只保证旧 worker
    永远无法再写入结果（finish 被 token 栅栏挡住），重试由新 worker 完成。
    """
    now = db.scalar(select(func.now()))
    stale = list(db.execute(select(
        KernelExecution.id, KernelExecution.lease_token, KernelExecution.record_id,
        KernelExecution.attempts, KernelExecution.cancel_requested,
    ).where(
        ((KernelExecution.status == "running") & (KernelExecution.lease_until <= now)) |
        (KernelExecution.status == "recovering"),
    ).limit(100)))
    db.rollback()
    recovered = 0
    for execution_id, token, record_id, attempts, cancel_requested in stale:
        if not token:
            continue
        now = db.scalar(select(func.now()))
        fenced = db.execute(update(KernelExecution).where(
            KernelExecution.id == execution_id,
            KernelExecution.lease_token == token,
            ((KernelExecution.status == "running") & (KernelExecution.lease_until <= now)) |
            (KernelExecution.status == "recovering"),
        ).values(status="recovering").execution_options(synchronize_session=False)).rowcount
        db.commit()
        if not fenced:
            continue
        _interrupt_orphan(record_id)
        terminal = bool(cancel_requested) or (attempts or 0) >= KERNEL_MAX_ATTEMPTS
        status = "cancelled" if cancel_requested else "failed" if terminal else "queued"
        values = dict(status=status, lease_until=None, lease_token=None,
                      error_code="WORKER_LOST" if terminal and not cancel_requested else None,
                      error_message=("执行服务中断，本次运行未产生结果，请重新运行。"
                                     if terminal and not cancel_requested else None))
        if terminal:
            values["finished_at"] = now
        changed = db.execute(update(KernelExecution).where(
            KernelExecution.id == execution_id,
            KernelExecution.lease_token == token,
            KernelExecution.status == "recovering",
        ).values(**values).execution_options(synchronize_session=False)).rowcount
        db.commit()
        if changed:
            recovered += 1
    return recovered


def _interrupt_orphan(record_id):
    """尽力中断可能仍在旧 worker 下运行的当前执行（不销毁内核、不丢变量）。"""
    try:
        from app.services.kernel_manager import get_kernel_manager
        get_kernel_manager().interrupt(record_id)
    except Exception:
        logger.warning("恢复执行时无法中断 kernel record=%s 的残留执行", record_id, exc_info=True)


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
