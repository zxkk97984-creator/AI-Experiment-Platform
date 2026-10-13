"""真实 Docker 验收：跨单元格状态、>30 秒执行、超时保留变量、无响应才重建、空闲回收保护。

默认跳过；显式跑：
  DAI_RUN_DOCKER_SMOKE=1 DAI_REDIS_URL=redis://127.0.0.1:<port>/0 \
    ../.runtime/school-security-venv/bin/python -m pytest tests/automated/test_kernel_async_docker_smoke.py -q
"""
from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time

import pytest

from app.models import ExperimentRecord
from app.services.kernel_manager import KernelManager
from app.services.kernel_queue import claim_execution, enqueue_execution, finish_execution

pytestmark = pytest.mark.skipif(
    os.environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
    reason="set DAI_RUN_DOCKER_SMOKE=1 to run real Docker Kernel smoke tests",
)


def _new_record_id() -> int:
    return 960_000_000 + secrets.randbelow(9_000_000)


def _stream_text(result) -> str:
    return "".join(
        (output.get("content") or {}).get("text", "")
        for output in (result.get("outputs") or [])
        if output.get("msg_type") == "stream"
    )


def _error_text(result) -> str:
    parts = []
    for output in result.get("outputs") or []:
        content = output.get("content") or {}
        if output.get("msg_type") == "error" and content.get("ename"):
            parts.append(f"{content['ename']}: {content.get('evalue', '')}")
    return "\n".join(parts)


def _container_id(name: str) -> str:
    out = subprocess.run(["docker", "ps", "-q", "-f", f"name={name}"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def test_timeout_interrupts_only_current_cell_and_long_cell_exceeds_30s(test_settings):
    """超时只中断当前执行（变量保留）；同一内核还能跑完 >30 秒的单元格。"""
    record_id = _new_record_id()
    manager = KernelManager(test_settings)
    try:
        session = manager.create_session(record_id, "")
        container_before = _container_id(session.container_name)
        assert _stream_text(manager.execute(record_id, "x = 42", timeout_seconds=120)).strip() == ""

        # 1) 超时：只中断当前执行，内核与变量保留
        timed = manager.execute(record_id, "import time; time.sleep(8)", timeout_seconds=2)
        assert timed["outcome"] == "timeout"
        assert timed["state_lost"] is False
        assert "已保留" in timed["message"]
        assert _container_id(session.container_name) == container_before, "超时不得重建内核"

        # 2) 变量仍在（修复前这里会是 NameError）
        cell3 = manager.execute(record_id, "print(x)", timeout_seconds=120)
        assert _stream_text(cell3).strip() == "42"
        assert "NameError" not in _error_text(cell3)

        # 3) 超过 30 秒的执行（旧实现 30s 硬超时必挂）正常返回
        long_cell = manager.execute(record_id, "import time; time.sleep(35); print('done')",
                                    timeout_seconds=120)
        assert long_cell["outcome"] == "succeeded"
        assert long_cell["execution_time_ms"] > 30_000
        assert _stream_text(long_cell).strip() == "done"
        assert _container_id(session.container_name) == container_before
    finally:
        manager.destroy(record_id)


def test_long_execution_keeps_exec_lock_renewed_past_its_ttl(test_settings):
    """长任务（>EXEC_LOCK_TTL）执行期间锁被心跳续期：第二个执行者必须被拒。"""
    record_id = _new_record_id()
    manager = KernelManager(test_settings)
    results: dict = {}

    def run_long_cell():
        try:
            results["result"] = manager.execute(
                record_id, "import time; time.sleep(70); print('long-done')", timeout_seconds=180)
        except Exception as exc:  # noqa: BLE001 - 记录异常供断言
            results["error"] = exc

    try:
        session = manager.create_session(record_id, "")
        worker = threading.Thread(target=run_long_cell, daemon=True)
        worker.start()
        # EXEC_LOCK_TTL=60：等到 65s 时，未续期的锁必然已过期
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            time.sleep(1)
            if not worker.is_alive():
                break
        import redis
        client = redis.from_url(test_settings.redis_url)
        ttl = client.ttl(f"kernel:lock:{record_id}")
        assert ttl and ttl > 0, f"长任务执行期间执行锁必须被续期，实际 ttl={ttl}"
        with pytest.raises(RuntimeError, match="正忙"):
            manager.execute(record_id, "print(1)", timeout_seconds=5)
        worker.join(timeout=60)
        assert not worker.is_alive()
        assert "error" not in results, results.get("error")
        assert results["result"]["outcome"] == "succeeded"
        assert _stream_text(results["result"]).strip() == "long-done"
        assert client.get(f"kernel:lock:{record_id}") is None, "执行结束必须释放锁"
    finally:
        manager.destroy(record_id)


def test_unresponsive_kernel_is_rebuilt_and_reports_state_lost(test_settings):
    """内核忽略 SIGINT → 探测无响应 → 销毁重建 + state_lost=True + 明确提示状态已丢失。"""
    record_id = _new_record_id()
    manager = KernelManager(test_settings)
    try:
        session = manager.create_session(record_id, "")
        container_before = _container_id(session.container_name)
        manager.execute(record_id, "keep = 'value'", timeout_seconds=120)

        # 忽略 SIGINT 后长睡：中断无效，探测必然排队超时 → 判定内核无响应
        stubborn = (
            "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(120)\n"
        )
        result = manager.execute(record_id, stubborn, timeout_seconds=3)
        assert result["outcome"] == "failed"
        assert result["state_lost"] is True
        assert "状态已丢失" in result["message"]
        assert "从头" in result["message"]

        session_after = manager._sessions[record_id]
        assert _container_id(session_after.container_name) != container_before, "必须销毁重建"

        # 重建后内存状态确实丢失（变量不可见）
        after = manager.execute(record_id, "print(keep)", timeout_seconds=120)
        assert "NameError" in _error_text(after)
    finally:
        manager.destroy(record_id)


@pytest.fixture()
def record_with_version(db_session_factory, test_settings):
    """最小实验记录图（真实 DB 行 + 真 Docker 内核）。"""
    from conftest import seed_basic_environment
    from lab_test_support import make_case

    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings)
    with db_session_factory() as db:
        record = ExperimentRecord(
            module_id=case.module_id,
            template_version_id=case.version_id,
            student_id=case.student.id,
            environment_version_id=case.environment_id,
            cells_sources={"code": "print(1)"},
            record_revision=1,
        )
        db.add(record)
        db.commit()
        return record.id


def test_in_flight_execution_is_not_reclaimed_but_finished_one_is(
        db_session_factory, test_settings, record_with_version):
    """对照组：在途执行的真实容器不被 cleanup_idle 回收；同一容器执行完成后即被回收。"""
    record_id = record_with_version
    manager = KernelManager(test_settings)
    try:
        session = manager.create_session(record_id, "", resource_factory=db_session_factory)
        container = session.container_name
        assert _container_id(container)

        with db_session_factory() as db:
            execution, _ = enqueue_execution(
                db, record_id=record_id, cell_id="code", code="print(1)", timeout_seconds=120,
                client_request_id=str(secrets.token_hex(8)))
            claimed = claim_execution(db, test_settings)
            assert claimed is not None and claimed[0] == execution.id

        # 早已超过空闲阈值，但在途执行必须让它活着
        session.last_active_at = 0
        with db_session_factory() as db:
            manager.cleanup_idle(1, db=db)
        assert _container_id(container), "在途执行的容器不得被当作空闲回收"

        with db_session_factory() as db:
            assert finish_execution(db, execution.id, claimed[1], "succeeded", outputs=[]) is True
            db.commit()
        # 对照组：同一容器执行完成后超阈值 → 回收
        with db_session_factory() as db:
            manager.cleanup_idle(1, db=db)
        assert not _container_id(container), "完成后超阈值的容器必须被回收"
    finally:
        manager.destroy(record_id)
