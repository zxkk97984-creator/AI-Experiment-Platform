"""异步单元格执行的任务状态机：租约、栅栏、恢复、取消与空闲回收保护。

MySQL 是唯一事实源；这里全部用 SQLite + 假 Docker 边界验证：
- (record_id, client_request_id) 幂等
- 同一记录同时只有一个执行在跑（队列串行）
- 心跳续租 / 过期租约不能续租也不能落终态
- worker 崩溃后 recover 重入队（attempts 递增），旧 worker 的写入被栅栏拒绝
- 取消：排队中直接取消，运行中置 cancel_requested
- 在途执行不被 cleanup_idle 回收；完成后同一内核超阈值被回收（对照组）
- Redis 执行锁的租约续期（CAS 语义）
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from app.models import ExperimentRecord, NotebookTemplateVersion, User
from app.models.kernel_exec import KernelExecution
from app.services.kernel_manager import EXEC_LOCK_TTL, KernelManager, KernelSession
from app.services import kernel_queue

from app.services.kernel_queue import (
    KERNEL_MAX_ATTEMPTS,
    claim_execution,
    enqueue_execution,
    finish_execution,
    heartbeat_execution,
    has_in_flight_execution,
    recover_executions,
    request_cancel,
    cancel_record_executions,
)


class LockStubRedis:
    """只实现执行锁用到的 SET/EVAL 语义（Lua 的 CAS 由 Python 复刻）。"""

    def __init__(self):
        self.values = {}
        self.expires = {}
        self.renew_calls = 0

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expires[key] = ex
        return True

    def setex(self, key, seconds, value):
        self.values[key] = value
        self.expires[key] = seconds
        return True

    def ttl(self, key):
        return self.expires.get(key, -2)

    def eval(self, script, numkeys, key, *args):
        if "expire" in script:
            if self.values.get(key) == args[0]:
                self.expires[key] = int(float(args[1]))
                self.renew_calls += 1
                return 1
            return 0
        if self.values.get(key) == args[0]:
            del self.values[key]
            self.expires.pop(key, None)
            return 1
        return 0

    def pipeline(self):
        return _LockStubPipeline(self)


class _LockStubPipeline:
    def __init__(self, client):
        self.client = client
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def watch(self, _key):
        return None

    def get(self, key):
        return self.client.values.get(key)

    def multi(self):
        return None

    def set(self, key, value, ex=None):
        self.pending.append((key, value, ex))

    def execute(self):
        for key, value, expiry in self.pending:
            self.client.set(key, value, ex=expiry)
        self.pending.clear()


class SlowProcess:
    """communicate 耗时 duration 秒后返回（模拟长单元格执行）。"""

    def __init__(self, duration=0.4, stdout='{"outputs":[]}'):
        self.duration = duration
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0
        self.stdin = _RecordingStdin()

    def communicate(self, input=None, timeout=None):
        time.sleep(self.duration)
        return self.stdout, self.stderr

    def kill(self):
        self.returncode = -9


class _RecordingStdin:
    def __init__(self):
        self.data = ""

    def write(self, value):
        self.data += value

    def close(self):
        pass


@pytest.fixture()
def kernel_case(db_session_factory, test_settings):
    """最小实验记录图：环境 + 模板版本 + 两个学生记录。"""
    from conftest import seed_basic_environment
    from lab_test_support import make_case

    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings)
    records = []
    with db_session_factory() as db:
        for student in (case.student, case.other_student):
            record = ExperimentRecord(
                module_id=case.module_id,
                template_version_id=case.version_id,
                student_id=student.id,
                environment_version_id=case.environment_id,
                cells_sources={"code": "print(1)"},
                record_revision=1,
            )
            db.add(record)
            db.flush()
            records.append(record.id)
        db.commit()
    return SimpleNamespace(record_id=records[0], other_record_id=records[1],
                           version_id=case.version_id, student_id=case.student.id)


def _enqueue(db, case, *, client_request_id=None, cell_id="code", code="print(1)",
             timeout_seconds=120):
    return enqueue_execution(db, record_id=case.record_id, cell_id=cell_id, code=code,
                             timeout_seconds=timeout_seconds,
                             client_request_id=client_request_id,
                             context={"record_revision": 1})


def test_worker_prepares_kernel_when_template_has_no_hidden_cells(
        db_session_factory, kernel_case, monkeypatch):
    from app.services import lab_workspace
    from app.worker.kernel_worker import _prepare_kernel

    record = SimpleNamespace(id=kernel_case.record_id,
                             template_version_id=kernel_case.version_id)
    version = SimpleNamespace(cells=[{"id": "code", "type": "code",
                                      "source_hidden": False, "source": "print(1)"}])
    execution = SimpleNamespace(id=88, record_id=record.id, timeout_seconds=120)

    class FakeSession:
        def get(self, model, object_id):
            if model is KernelExecution:
                return execution
            if model is ExperimentRecord:
                return record
            if model is NotebookTemplateVersion:
                return version
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    calls = []
    class FakeManager:
        def get_or_create_session(self, record_id, **kwargs):
            calls.append((record_id, kwargs))
            return SimpleNamespace(record_id=record_id)

    monkeypatch.setattr("app.worker.kernel_worker._resolve_run_environment",
                        lambda db, value: (None, None))
    session = _prepare_kernel(lambda: FakeSession(), FakeManager(), execution.id, "lease")

    assert session == record.id
    assert len(calls) == 1, "即使模板没有隐藏单元格，也必须准备 worker 的内核 session"
    assert calls[0][0] == record.id


def test_enqueue_is_idempotent_per_client_request_id(db_session_factory, kernel_case):
    request_id = str(uuid.uuid4())
    with db_session_factory() as db:
        first, created = _enqueue(db, kernel_case, client_request_id=request_id)
        assert created is True
        second, created_again = _enqueue(db, kernel_case, client_request_id=request_id)
        assert created_again is False
        assert second.id == first.id
        rows = db.scalars(select(KernelExecution).where(
            KernelExecution.record_id == kernel_case.record_id)).all()
        assert len(rows) == 1, "重复点击不得创建第二条执行"


def test_claim_serializes_one_running_execution_per_record(db_session_factory, kernel_case, test_settings):
    with db_session_factory() as db:
        first, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        second, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        other, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        other.record_id = kernel_case.other_record_id
        db.commit()
        first_id, other_id = first.id, other.id

        claimed = claim_execution(db, test_settings)
        assert claimed is not None
        claimed_id, token = claimed
        row = db.get(KernelExecution, claimed_id, populate_existing=True)
        assert row.status == "running" and row.lease_token == token
        assert row.attempts == 1 and row.started_at is not None and row.node_id
        # 同一记录的第二个执行必须排队；另一个记录的执行仍可被认领
        second_claim = claim_execution(db, test_settings)
        assert second_claim is not None
        assert second_claim[0] == other_id
        assert claim_execution(db, test_settings) is None
        assert db.get(KernelExecution, second.id).status == "queued"


def test_heartbeat_renews_lease_and_requires_token(db_session_factory, kernel_case, test_settings):
    with db_session_factory() as db:
        execution, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, token = claim_execution(db, test_settings)
        # SQLite 的 CURRENT_TIMESTAMP 只有秒级精度：先把租约压短，才能断言续期把它推后
        row = db.get(KernelExecution, execution_id, populate_existing=True)
        row.lease_until = db.scalar(select(func.now())) + timedelta(seconds=5)
        db.commit()
        before = row.lease_until
        assert heartbeat_execution(db, execution_id, token) is True
        after = db.get(KernelExecution, execution_id, populate_existing=True).lease_until
        assert after > before, "心跳必须把 lease_until 往后推"
        assert heartbeat_execution(db, execution_id, str(uuid.uuid4())) is False


def test_expired_lease_cannot_heartbeat_or_finish(db_session_factory, kernel_case, test_settings):
    with db_session_factory() as db:
        _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, token = claim_execution(db, test_settings)
        row = db.get(KernelExecution, execution_id)
        row.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()
        assert heartbeat_execution(db, execution_id, token) is False
        assert finish_execution(db, execution_id, token, "succeeded") is False


def test_recovery_never_replays_stateful_code_after_worker_loss(
        db_session_factory, kernel_case, test_settings, monkeypatch):
    """结果栅栏不能防止重放改变共享变量；worker 丢失后本次执行必须终止。"""
    monkeypatch.setattr(kernel_queue, "_interrupt_orphan", lambda record_id: (True, False))
    with db_session_factory() as db:
        _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, old_token = claim_execution(db, test_settings)
        row = db.get(KernelExecution, execution_id)
        row.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()

        assert recover_executions(db, test_settings) == 1
        recovered = db.get(KernelExecution, execution_id, populate_existing=True)
        assert recovered.status == "failed" and recovered.error_code == "WORKER_LOST"
        assert "变量可能已经部分改变" in recovered.error_message
        assert claim_execution(db, test_settings) is None, "worker 丢失的学生代码不得自动重放"

        # 旧 worker 写入仍被栅栏拒绝，执行保持明确的失败状态。
        assert finish_execution(db, execution_id, old_token, "succeeded",
                                outputs=[{"msg_type": "stream", "content": {"text": "stale"}}]) is False
        final = db.get(KernelExecution, execution_id, populate_existing=True)
        assert final.status == "failed" and final.outputs == []


def test_recovering_record_blocks_following_execution_until_stop_is_confirmed(
        db_session_factory, kernel_case, test_settings, monkeypatch):
    monkeypatch.setattr(kernel_queue, "_interrupt_orphan", lambda record_id: (False, False))
    with db_session_factory() as db:
        first, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        second, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, _token = claim_execution(db, test_settings)
        assert execution_id == first.id
        row = db.get(KernelExecution, execution_id)
        row.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()

        assert recover_executions(db, test_settings) == 0
        recovering = db.get(KernelExecution, execution_id, populate_existing=True)
        assert recovering.status == "recovering"
        assert recovering.lease_token and recovering.lease_until
        assert claim_execution(db, test_settings) is None
        assert db.get(KernelExecution, second.id).status == "queued"



def test_recovery_exhausts_attempts_into_failed(db_session_factory, kernel_case,
                                                test_settings, monkeypatch):
    monkeypatch.setattr(kernel_queue, "_interrupt_orphan", lambda record_id: (True, False))
    with db_session_factory() as db:
        _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, _token = claim_execution(db, test_settings)
        row = db.get(KernelExecution, execution_id)
        row.attempts = KERNEL_MAX_ATTEMPTS
        row.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()
        assert recover_executions(db, test_settings) == 1
        final = db.get(KernelExecution, execution_id, populate_existing=True)
        assert final.status == "failed" and final.error_code == "WORKER_LOST"
        assert claim_execution(db, test_settings) is None


def test_recovery_cancels_requested_execution_after_confirmed_stop(
        db_session_factory, kernel_case, test_settings, monkeypatch):
    monkeypatch.setattr(kernel_queue, "_interrupt_orphan", lambda record_id: (True, False))
    with db_session_factory() as db:
        execution, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, _token = claim_execution(db, test_settings)
        assert execution_id == execution.id
        row = db.get(KernelExecution, execution_id)
        row.cancel_requested = True
        row.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()

        assert recover_executions(db, test_settings) == 1
        final = db.get(KernelExecution, execution_id, populate_existing=True)
        assert final.status == "cancelled"
        assert final.error_code is None
        assert final.finished_at is not None


def test_worker_loss_stop_preserves_responsive_kernel_and_marks_destroyed_state_lost(
        kernel_case, test_settings, monkeypatch):
    manager = KernelManager(test_settings)
    record_id = kernel_case.record_id
    session = KernelSession(record_id, f"dai-kernel-rec-{record_id}", {"ip": "127.0.0.1"})
    manager._sessions[record_id] = session
    monkeypatch.setattr(manager, "_inspect_container_running", lambda _name: True)
    monkeypatch.setattr(manager, "_send_interrupt", lambda _session: None)
    monkeypatch.setattr(manager, "_probe_kernel", lambda _session: True)
    monkeypatch.setattr(manager, "_touch_session_activity", lambda *_args, **_kwargs: None)
    assert manager.stop_execution_after_worker_loss(record_id) == (True, False)

    monkeypatch.setattr(manager, "_probe_kernel", lambda _session: False)
    monkeypatch.setattr(manager, "destroy", lambda _record_id: None)
    assert manager.stop_execution_after_worker_loss(record_id) == (True, True)

    def fail_destroy(_record_id):
        raise RuntimeError("cannot confirm stop")

    monkeypatch.setattr(manager, "destroy", fail_destroy)
    assert manager.stop_execution_after_worker_loss(record_id) == (False, False)


def test_cancel_queued_and_running(db_session_factory, kernel_case, test_settings):
    with db_session_factory() as db:
        queued, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        running, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        # 先认领 queued 那条，running 那条保持排队
        execution_id, token = claim_execution(db, test_settings)
        assert execution_id == queued.id
        cancelled = request_cancel(db, running.id)
        assert cancelled.status == "cancelled" and cancelled.cancel_requested is True

        still_running = request_cancel(db, queued.id)
        assert still_running.status == "running" and still_running.cancel_requested is True
        # 取消后的执行只能落 cancelled，不能落 succeeded
        assert finish_execution(db, queued.id, token, "succeeded") is False
        assert finish_execution(db, queued.id, token, "cancelled", outputs=[]) is True
        db.commit()

        # 重启内核时批量取消：只影响非终态
        another, _ = _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        assert cancel_record_executions(db, kernel_case.record_id) == 1
        assert db.get(KernelExecution, another.id, populate_existing=True).status == "cancelled"
        assert cancel_record_executions(db, kernel_case.record_id) == 0


def test_lease_guard_renews_lease_while_long_execution_runs(
        db_session_factory, kernel_case, test_settings):
    """长任务执行期间 LeaseGuard 持续续租；租约被抢走时立即 fail-closed。"""
    from app.services.kernel_queue import KernelLeaseGuard, LeaseLost

    with db_session_factory() as db:
        _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, token = claim_execution(db, test_settings)
        row = db.get(KernelExecution, execution_id, populate_existing=True)
        row.lease_until = db.scalar(select(func.now())) + timedelta(seconds=2)
        db.commit()

    guard = KernelLeaseGuard(db_session_factory, execution_id, token,
                             lease_seconds=30, interval_seconds=0.05)
    with guard:
        guard.assert_owned()
        time.sleep(0.3)  # 模拟长执行：心跳线程在此期间续租
        with db_session_factory() as db:
            renewed = db.get(KernelExecution, execution_id, populate_existing=True).lease_until
            now = db.scalar(select(func.now()))
        assert renewed > now + timedelta(seconds=20), "长执行期间租约必须被续期"

    # 对照组：租约被别的 worker 抢走后，旧 guard 必须 fail-closed
    stolen = KernelLeaseGuard(db_session_factory, execution_id, token,
                              lease_seconds=30, interval_seconds=10)
    with db_session_factory() as db:
        db.execute(update(KernelExecution).where(KernelExecution.id == execution_id)
                   .values(lease_token=str(uuid.uuid4())))
        db.commit()
    with stolen:
        try:
            stolen.assert_owned()
        except LeaseLost:
            pass
        else:  # pragma: no cover - 失栅栏必须报错
            raise AssertionError("租约被抢走后 assert_owned 必须抛 LeaseLost")


def test_cleanup_idle_keeps_in_flight_kernel_and_reclaims_after_finish(
        db_session_factory, kernel_case, test_settings, monkeypatch):
    """对照组：在途执行的 kernel 不被回收；同一 kernel 完成后超阈值即被回收。"""
    import fakeredis
    import redis

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)
    record_id = kernel_case.record_id
    with db_session_factory() as db:
        _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
        execution_id, token = claim_execution(db, test_settings)
        assert has_in_flight_execution(db, record_id) is True

    km = KernelManager(test_settings)
    km._sessions[record_id] = KernelSession(record_id, f"dai-kernel-rec-{record_id}",
                                            {"ip": "127.0.0.1"})
    km._sessions[record_id].last_active_at = 0  # 早已超过空闲阈值
    km._resource_factories[record_id] = db_session_factory
    km._write_session_redis(record_id, km._sessions[record_id])
    client.set(km._activity_key(record_id), "0")
    destroyed = []
    km.destroy = lambda rid: destroyed.append(rid)

    # 在途（running）→ 不回收
    km.cleanup_idle(1)
    assert destroyed == [], "正在执行的内核不得被当作空闲回收"

    with db_session_factory() as db:
        assert finish_execution(db, execution_id, token, "succeeded", outputs=[]) is True
        db.commit()
        assert has_in_flight_execution(db, record_id) is False

    # 对照组：同一内核执行完成后超过阈值 → 被回收
    km.cleanup_idle(1)
    assert destroyed == [record_id]

    # 排队中同样受保护（还没轮到执行）
    with db_session_factory() as db:
        _enqueue(db, kernel_case, client_request_id=str(uuid.uuid4()))
    destroyed.clear()
    km.cleanup_idle(1)
    assert destroyed == [], "排队等待执行的 kernel 也不得被回收"


def test_idle_cleanup_uses_published_timeout_and_shared_monotonic_activity(
        db_session_factory, kernel_case, test_settings, monkeypatch):
    import fakeredis
    import redis

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)
    now = time.time()
    rid = kernel_case.record_id
    km = KernelManager(test_settings)
    newer = KernelSession(rid, f"dai-kernel-rec-{rid}", {"ip": "127.0.0.1"},
                          last_active_at=now - 1000)
    km._sessions[rid] = newer
    km._resource_factories[rid] = db_session_factory
    km._write_session_redis(rid, newer)

    stale = KernelSession(rid, f"dai-kernel-rec-{rid}", {"ip": "127.0.0.1"},
                          last_active_at=now - 4000)
    km._write_session_redis(rid, stale)
    restored = km._read_session_redis(rid)
    assert restored is not None
    assert restored.last_active_at >= now - 1000, "旧进程不能覆盖较新的共享活动时间"
    assert client.ttl(f"kernel:session:{rid}") >= 7490

    destroyed = []
    km.destroy = lambda record_id: destroyed.append(record_id)
    with db_session_factory() as db:
        version = db.get(NotebookTemplateVersion, kernel_case.version_id)
        version.kernel_idle_seconds = 7200
        db.commit()
        km.cleanup_idle(max_idle_seconds=900, db=db)
        assert destroyed == [], "已空闲 1000 秒的内核不应被 7200 秒模板阈值回收"

        version.kernel_idle_seconds = 300
        db.commit()
        km.cleanup_idle(max_idle_seconds=900, db=db)
        assert destroyed == [rid], "空闲回收必须使用当前记录发布版本的 300 秒阈值"


def test_exec_lock_renewal_is_cas_guarded_and_outlives_ttl():
    client = LockStubRedis()
    key, token = "kernel:lock:7", "token-a"
    assert client.set(key, token, nx=True, ex=EXEC_LOCK_TTL) is True
    stop = threading.Event()
    thread = threading.Thread(target=KernelManager._renew_exec_lock,
                              args=(client, key, token, stop),
                              kwargs={"ttl": 5, "interval": 0.02})
    thread.start()
    time.sleep(0.12)
    assert client.renew_calls >= 2, "持有锁期间必须持续续期"
    assert client.ttl(key) == 5

    # 锁被别的执行者接管 → 不再续别人的锁（CAS 语义）
    client.values[key] = "token-b"
    client.expires[key] = 5
    renews = client.renew_calls
    time.sleep(0.1)
    assert client.values[key] == "token-b"
    assert client.expires[key] == 5, "不得续期不属于自己的锁"
    assert client.renew_calls < renews + 3
    stop.set()
    thread.join(timeout=2)


def test_long_execution_renews_lock_and_releases_it_afterwards(
        db_session_factory, kernel_case, test_settings, monkeypatch):
    """长任务执行期间锁被续期（不会被 TTL 放行第二个执行者），结束后释放。"""
    import app.services.kernel_manager as kernel_manager

    km = KernelManager(test_settings)
    record_id = kernel_case.record_id
    km._sessions[record_id] = KernelSession(record_id, f"dai-kernel-rec-{record_id}",
                                            {"ip": "127.0.0.1"})
    km._touch_resources = lambda rid, session: None
    client = LockStubRedis()
    proc = SlowProcess(duration=0.5)

    monkeypatch.setattr(kernel_manager, "EXEC_LOCK_TTL", 0.3)
    monkeypatch.setattr(kernel_manager.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(kernel_manager.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="cid\n", stderr=""))
    monkeypatch.setattr("redis.from_url", lambda *a, **k: client)

    result = km.execute(record_id, "print(1)", timeout_seconds=5)

    assert result["outcome"] == "succeeded"
    assert client.renew_calls >= 1, "长执行期间必须续期执行锁"
    assert client.values.get(f"kernel:lock:{record_id}") is None, "执行结束必须释放锁"


def test_in_flight_check_fails_closed_without_database(kernel_case, test_settings):
    """没有可用数据库连接时不得误判为空闲（fail-closed 保留内核）。"""
    km = KernelManager(test_settings)
    record_id = kernel_case.record_id
    km._sessions[record_id] = KernelSession(record_id, "c", {"ip": "127.0.0.1"})
    km._sessions[record_id].last_active_at = 0

    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("db down")

    km._resource_factories[record_id] = BrokenFactory()
    destroyed = []
    km.destroy = lambda rid: destroyed.append(rid)
    km.cleanup_idle(1)
    assert destroyed == []

def test_lease_loss_during_preparation_never_executes_student_code(
    db_session_factory, kernel_case, test_settings, monkeypatch,
):
    from app.worker import kernel_worker

    with db_session_factory() as db:
        _enqueue(db, kernel_case)
        db.commit()
        execution_id, token = claim_execution(db, test_settings)
    guards = []

    class PreparationGuard(kernel_queue.KernelLeaseGuard):
        def __enter__(self):
            guards.append(self)
            return self

        def __exit__(self, *_):
            pass

    state = {"student_side_effects": 0}
    monkeypatch.setattr(kernel_worker, "KernelLeaseGuard", PreparationGuard)
    monkeypatch.setattr("app.services.kernel_manager.get_kernel_manager", lambda: object())
    monkeypatch.setattr(kernel_worker, "_interrupt_kernel", lambda *_: None)
    monkeypatch.setattr(kernel_worker, "_prepare_kernel", lambda *_: guards[0]._lost())

    def execute(*args, **kwargs):
        state["student_side_effects"] += 1
        return {"outcome": "succeeded", "outputs": []}

    monkeypatch.setattr("app.services.lab_workspace.execute_kernel_code", execute)
    kernel_worker.process_execution(db_session_factory, execution_id, token, test_settings)
    assert state["student_side_effects"] == 0
    assert guards[0].cancel_event.is_set()
    with db_session_factory() as db:
        assert db.get(KernelExecution, execution_id).status == "running"


def test_pre_cancelled_kernel_execution_never_starts_runner(test_settings, monkeypatch):
    manager = KernelManager(test_settings)
    event = threading.Event()
    event.set()
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: pytest.fail("cancelled code started"))
    result = manager.execute(123, "state += 1", interrupt_event=event)
    assert result["outcome"] == "cancelled"
    assert result["state_lost"] is False


def test_cancellation_before_payload_does_not_send_student_code(test_settings, monkeypatch):
    manager = KernelManager(test_settings)
    manager._sessions[123] = KernelSession(123, "cancelled-runner", {"ip": "127.0.0.1"})
    monkeypatch.setattr("subprocess.run", lambda *_a, **_k:
                        SimpleNamespace(returncode=0, stdout="cid\n", stderr=""))
    monkeypatch.setattr(manager, "_touch_resources", lambda *_: None)
    monkeypatch.setattr(manager, "_touch_session_activity", lambda *_a, **_k: None)
    event = threading.Event()
    client = LockStubRedis()
    monkeypatch.setattr("redis.from_url", lambda *_a, **_k: client)
    inputs = []

    class WaitingRunner:
        returncode = 0
        killed = False

        def kill(self):
            self.killed = True

        def communicate(self, input=None, timeout=None):
            inputs.append(input)
            return '{"outputs":[]}', ""

    proc = WaitingRunner()
    def start(*_a, **_k):
        event.set()  # Lease lost after runner starts, before code reaches stdin.
        return proc

    monkeypatch.setattr("subprocess.Popen", start)
    monkeypatch.setattr(manager, "_send_interrupt", lambda *_: None)
    result = manager.execute(123, "state += 1", interrupt_event=event)
    assert result["outcome"] == "cancelled"
    assert all(payload is None for payload in inputs)
    assert proc.killed
    assert client.values.get("kernel:lock:123") is None
