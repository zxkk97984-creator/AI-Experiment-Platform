"""Durable lab claims, fencing and recovery; Redis is only a wake-up hint."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.models.lab import LabResourceLease, LabRun
from app.services.lab_resources import lock_resources, release_resources, reserve_resources, touch_resources

logger = logging.getLogger("dai.lab_queue")
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "needs_review"}
VALIDATION_KINDS = {"dataset_validate", "artifact_validate"}


class LeaseLost(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


def _owned(run_id, token, now):
    return (LabRun.id == run_id, LabRun.lease_token == token,
            LabRun.status == "running", LabRun.lease_until > now)


def assert_run_owned(db, run_id, token):
    now = db.scalar(select(func.now()))
    row = db.execute(select(LabRun.cancel_requested).where(*_owned(run_id, token, now))).first()
    if row is None:
        raise LeaseLost("Lab execution lease was lost")
    if row[0]:
        raise RunCancelled("Lab execution was cancelled")


def request_cancel(db, run_id):
    """Cancel queued work atomically; running work retains its cleanup lease."""
    changed = db.execute(update(LabRun).where(
        LabRun.id == run_id, LabRun.status == "queued",
    ).values(cancel_requested=True, status="cancelled", active_student_id=None,
             finished_at=func.now()).execution_options(synchronize_session=False)).rowcount
    if not changed:
        db.execute(update(LabRun).where(
            LabRun.id == run_id, LabRun.status.in_(("running", "recovering")),
        ).values(cancel_requested=True).execution_options(synchronize_session=False))


def claim_run(db, settings):
    try:
        return _claim_run(db, settings)
    except OperationalError as exc:
        # Concurrent first insertion of the global guard can deadlock on
        # InnoDB gap locks. This is a lost claim, not lost durable work.
        code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
        if code not in (1205, 1213):
            raise
        db.rollback()
        return None


def _claim_run(db, settings):
    """Claim oldest admissible work and commit its resource reservation atomically."""
    if not settings.lab_enabled:
        return None
    from app.services.lab_node import benchmark_daemon_ready, is_benchmark, matches_worker
    if settings.lab_worker_mode == 'benchmark' and not benchmark_daemon_ready(settings):
        return None
    now = lock_resources(db)
    query = select(LabRun).where(LabRun.status == 'queued')
    node = LabRun.snapshot['benchmark_node_id'].as_string()
    if settings.lab_worker_mode == 'benchmark':
        query = query.where(node == settings.lab_benchmark_node_id,
            LabRun.kind.in_(['baseline', 'evaluate']))
    else:
        query = query.where(func.coalesce(node, '') == '')
    candidates = list(db.scalars(query.order_by(LabRun.created_at, LabRun.id).limit(100).with_for_update()))
    for run in candidates:
        if not matches_worker(run.kind, run.snapshot, settings):
            if settings.lab_worker_mode == 'general' and is_benchmark(run.kind, run.snapshot) and not run.snapshot.get('benchmark_node_id'):
                run.status, run.error_code = 'needs_review', 'LAB_BENCHMARK_NODE_REQUIRED'
                run.error_message = '历史效率任务未冻结基准节点，需管理员核对，未发布成绩。'
                run.finished_at = now
            continue
        if run.cancel_requested:
            run.status = "cancelled"
            run.finished_at = now
            run.active_student_id = None
            continue
        token = str(uuid.uuid4())
        resources = run.resources or {}
        if run.kind in VALIDATION_KINDS:
            if db.scalar(select(func.count()).select_from(LabResourceLease).where(
                LabResourceLease.kind == "validation",
            )):
                continue
            db.add(LabResourceLease(key=f"run:{run.id}", token=token, kind="validation",
                cpus=0, memory_mb=0, expires_at=now + timedelta(seconds=settings.lab_lease_seconds)))
        else:
            try:
                config = run.snapshot.get('assessment_config', {})
                benchmark = run.kind in ('baseline', 'evaluate') and any(c.get('source') == 'efficiency' for c in config.get('criteria', []))
                reservation = reserve_resources(db, key=f"run:{run.id}", token=token,
                    kind="benchmark" if benchmark else "job", cpus=resources.get("cpus", 1),
                    memory_mb=resources.get("memory_mb", 1024), settings=settings,
                    device=run.device or "cpu")
            except ValueError:
                run.status = "failed"
                run.error_code = "RESOURCE_CONFIGURATION"
                run.error_message = "请求的运行资源超出管理员配置，请联系教师或管理员。"
                run.active_student_id = None
                run.finished_at = now
                if run.submission_id:
                    from app.models.lab import LabSubmission
                    db.execute(update(LabSubmission).where(
                        LabSubmission.submission_id == run.submission_id,
                        LabSubmission.current_run_id == run.id,
                    ).values(quota_charged=False))
                continue
            if reservation is None:
                continue
        run.status = "running"
        run.lease_token = token
        run.lease_until = now + timedelta(seconds=settings.lab_lease_seconds)
        run.attempts = (run.attempts or 0) + 1
        run.started_at = now
        run.finished_at = None
        run.error_code = None
        run.error_message = None
        db.commit()
        return run.id, token
    db.commit()
    return None


def heartbeat_run(db, run_id, token, settings):
    now = db.scalar(select(func.now()))
    changed = db.execute(update(LabRun).where(*_owned(run_id, token, now)).values(
        lease_until=now + timedelta(seconds=settings.lab_lease_seconds),
    )).rowcount
    if changed and not touch_resources(db, f"run:{run_id}", token, settings):
        db.rollback()
        return False
    db.commit()
    return changed == 1


def append_logs(db, run_id, token, text, settings):
    now = db.scalar(select(func.now()))
    current = db.scalar(select(LabRun.logs).where(*_owned(run_id, token, now)))
    if current is None:
        raise LeaseLost("Lab execution lease was lost")
    bounded = ((current or "") + text).encode("utf-8")[-settings.lab_log_max_bytes:].decode("utf-8", "ignore")
    changed = db.execute(update(LabRun).where(*_owned(run_id, token, now)).values(logs=bounded)).rowcount
    if changed != 1:
        raise LeaseLost("Lab execution lease was lost")
    db.commit()


def finish_run(db, run_id, token, status, result=None, error_code=None, error_message=None):
    """Fenced terminal transition; caller commits alongside grades/artifacts.

    The caller must have stopped every sandbox before invoking this function.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError("Invalid terminal lab status")
    run = db.get(LabRun, run_id)
    if run and run.submission_id:
        from app.services.lab_grading import lock_evaluation_records
        lock_evaluation_records(db, run)
    now = db.scalar(select(func.now()))
    clauses = list(_owned(run_id, token, now))
    if status != "cancelled":
        clauses.append(LabRun.cancel_requested.is_(False))
    values = dict(status=status, lease_until=None, active_student_id=None,
                  finished_at=now, error_code=error_code, error_message=error_message)
    if result is not None:
        values["result"] = result
    changed = db.execute(update(LabRun).where(*clauses).values(**values)).rowcount
    if changed == 1:
        release_resources(db, f"run:{run_id}", token)
    return changed == 1


def recover_runs(db, settings):
    """Fence expired owners, stop their exact containers, then requeue or fail.

    Unreachable Docker means no capacity is released. Retrying recovery remains
    safe because the recovering record keeps its original container token.
    """
    from app.services.lab_runtime import stop_run_containers
    from app.services.lab_node import benchmark_daemon_ready
    if settings.lab_worker_mode == 'benchmark' and not benchmark_daemon_ready(settings):
        return 0

    now = db.scalar(select(func.now()))
    stale = list(db.execute(select(LabRun.id, LabRun.lease_token, LabRun.kind, LabRun.snapshot).where(
        ((LabRun.status == "running") & (LabRun.lease_until <= now)) |
        (LabRun.status == "recovering"),
    ).limit(100)))
    db.rollback()
    recovered = 0
    from app.services.lab_node import is_benchmark, matches_worker
    for run_id, token, kind, snapshot in stale:
        unbound_legacy = (settings.lab_worker_mode == 'general' and is_benchmark(kind, snapshot)
            and not (snapshot or {}).get('benchmark_node_id'))
        if not unbound_legacy and not matches_worker(kind, snapshot, settings):
            continue
        if not token:
            continue
        now = db.scalar(select(func.now()))
        fenced = db.execute(update(LabRun).where(
            LabRun.id == run_id, LabRun.lease_token == token,
            ((LabRun.status == "running") & (LabRun.lease_until <= now)) |
            (LabRun.status == "recovering"),
        ).values(status="recovering")).rowcount
        db.commit()
        if not fenced:
            continue
        try:
            if kind not in VALIDATION_KINDS and not stop_run_containers(run_id, token):
                continue
        except OSError:
            continue
        run = db.get(LabRun, run_id, populate_existing=True)
        if not run or run.status != "recovering" or run.lease_token != token:
            db.rollback()
            continue
        terminal = run.cancel_requested or run.attempts >= settings.lab_max_attempts
        if terminal and run.submission_id:
            from app.services.lab_grading import lock_evaluation_records
            lock_evaluation_records(db, run)
        status = "cancelled" if run.cancel_requested else "failed" if terminal else "queued"
        values = dict(status=status, lease_until=None, lease_token=None,
            error_code="WORKER_LOST" if terminal and not run.cancel_requested else None,
            error_message="运行服务中断，提交未计为零分，可重新提交。" if terminal and not run.cancel_requested else None)
        if terminal:
            values.update(active_student_id=None, finished_at=now)
        changed = db.execute(update(LabRun).where(
            LabRun.id == run_id, LabRun.lease_token == token,
            LabRun.status == "recovering",
        ).values(**values)).rowcount
        if changed:
            release_resources(db, f"run:{run_id}", token)
            # A failed infrastructure attempt never consumes the student's quota.
            if terminal and run.submission_id:
                from app.models.lab import LabSubmission
                db.execute(update(LabSubmission).where(
                    LabSubmission.submission_id == run.submission_id,
                    LabSubmission.current_run_id == run_id,
                ).values(quota_charged=False))
            if terminal and (run.snapshot or {}).get("batch_id"):
                from app.services.lab_grading import refresh_regrade_batch
                # Make the terminal transition visible before inspecting the batch.
                db.expire(run)
                refresh_regrade_batch(db, run.snapshot["batch_id"])
            recovered += 1
        db.commit()
    return recovered


_guards = {}
_guard_lock = threading.Lock()


def get_run_guard(run_id, token):
    with _guard_lock:
        return _guards.get((run_id, token))


class LeaseGuard:
    """Heartbeat independent of model execution and long storage operations."""
    def __init__(self, session_factory, run_id, token, settings):
        self.factory, self.run_id, self.token, self.settings = session_factory, run_id, token, settings
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True,
            name=f"lab-lease-{run_id}")

    def __enter__(self):
        with _guard_lock:
            _guards[(self.run_id, self.token)] = self
        self.thread.start()
        return self

    def assert_owned(self):
        if self.lost_event.is_set():
            raise LeaseLost("Lab execution lease was lost")
        with self.factory() as db:
            assert_run_owned(db, self.run_id, self.token)

    def _heartbeat(self):
        interval = max(0.5, min(10, self.settings.lab_lease_seconds / 3))
        while not self.stop_event.wait(interval):
            try:
                with self.factory() as db:
                    if heartbeat_run(db, self.run_id, self.token, self.settings):
                        continue
            except Exception:
                logger.exception("lab heartbeat failed for run %s", self.run_id)
            self.lost_event.set()
            # Fence fail-closed even when the processor isn't currently polling.
            try:
                from app.services.lab_runtime import stop_run_containers
                stop_run_containers(self.run_id, self.token)
            except Exception:
                logger.warning("Unable to stop lease-lost lab run %s", self.run_id)
            return

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=15)
        with _guard_lock:
            _guards.pop((self.run_id, self.token), None)
