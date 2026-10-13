"""Kernel 第二轮返工 RED 测试 — 只测 Kernel"""
from __future__ import annotations

import io
import json
import subprocess
import time
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from app.services.kernel_manager import (
    KernelManager,
    KernelSession,
    get_kernel_manager,
)


class RecordingStdin(io.StringIO):
    """记录写入内容的 stdin 替身：生产代码会主动 close()，之后仍要能读数。"""

    def __init__(self):
        super().__init__()
        self._snapshot = ""

    def write(self, data):
        self._snapshot += data
        return super().write(data)

    def getvalue(self):
        try:
            return super().getvalue()
        except ValueError:
            return self._snapshot


class FakeProcess:
    """subprocess.Popen 的最小替身：stdin 收代码，communicate 返回预设输出。"""

    def __init__(self, stdout="", stderr="", returncode=0, partial_stdout=None):
        self.stdin = RecordingStdin()
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.partial_stdout = partial_stdout
        self.killed = False
        self.interrupted = False
        self.communicate_calls = 0
        # 生产代码通过 communicate(input=payload) 写 stdin（不能先 close 再 communicate）
        self.stdin_payload = None

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if input is not None and self.stdin_payload is None:
            self.stdin_payload = input
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9


class HangingProcess(FakeProcess):
    """超时替身：中断前 communicate 一直抛 TimeoutExpired，中断后返回部分输出。"""

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if input is not None and self.stdin_payload is None:
            self.stdin_payload = input
        if not self.interrupted:
            # 模拟真实等待：不空转，让 deadline 尽快到达
            time.sleep(min(timeout or 0, 0.02))
            raise subprocess.TimeoutExpired("docker", timeout)
        if self.partial_stdout is not None:
            return self.partial_stdout, self.stderr
        return self.stdout, self.stderr


def _live_session(record_id=1, **kwargs):
    km = KernelManager()
    session = KernelSession(record_id, f"dai-kernel-rec-{record_id}", {"ip": "127.0.0.1"}, **kwargs)
    km._sessions[record_id] = session
    return km, session


def _docker_run(*, exec_stdout="", exec_stderr="", exec_returncode=0, probe_returncode=0,
                on_exec=None, on_kill=None):
    """subprocess.run 替身：docker ps 视为存活，其余 docker 调用可定制。"""
    def _run(args, **kwargs):
        if isinstance(args, (list, tuple)) and len(args) > 2 and args[1] == "ps":
            return MagicMock(returncode=0, stdout="container-id\n")
        if isinstance(args, (list, tuple)) and len(args) > 2 and args[1] == "exec":
            if "kill" in args:
                if on_kill is not None:
                    on_kill()
                return MagicMock(returncode=0, stdout="")
            if on_exec is not None:
                return on_exec(args, kwargs)
            return MagicMock(returncode=probe_returncode, stdout=exec_stdout, stderr=exec_stderr)
        return MagicMock(returncode=0, stdout="")
    return _run


# ═══════════════════════════════════════════════════════════════
# 1. Runner 连接持久 ipykernel，代码走 stdin
# ═══════════════════════════════════════════════════════════════

def test_runner_argv_has_trusted_script_and_no_student_code():
    """docker exec argv 含 /.dai/kernel_runner.py，不含学生代码；代码走 stdin"""
    km, _session = _live_session(1)

    tricky_code = 'print("hello\\nworld")\nx = """quotes"""\n# backslash: \\'

    with patch('redis.from_url'), \
         patch('subprocess.run', side_effect=_docker_run()), \
         patch('app.services.kernel_manager.subprocess.Popen') as mock_popen:
        proc = FakeProcess(stdout='{"outputs":[]}')
        mock_popen.return_value = proc
        km.execute(1, tricky_code)

    argv = mock_popen.call_args[0][0]
    argv_str = ' '.join(str(a) for a in argv)

    # trusted runner path (in argv)
    assert '/.dai/kernel_runner.py' in argv or 'kernel_runner.py' in argv_str
    # no student code in argv
    assert 'hello' not in argv_str
    assert '"""quotes"""' not in argv_str
    # stdin 传递的 JSON 包含代码（且代码不出现在 argv）
    decoded = json.loads(proc.stdin_payload)
    assert decoded['code'] == tricky_code


def test_environment_session_uses_canonical_runner_path():
    """绑定环境版本的 Kernel 必须调用环境镜像内的 canonical runner。"""
    km, _session = _live_session(
        1, image_ref="sha256:" + "a" * 64, environment_version_id=2)

    with patch('redis.from_url'), \
         patch('subprocess.run', side_effect=_docker_run()), \
         patch('app.services.kernel_manager.subprocess.Popen') as mock_popen:
        mock_popen.return_value = FakeProcess(stdout='{"outputs":[]}')
        km.execute(1, "print(1)")

    argv = mock_popen.call_args[0][0]
    assert argv[0:2] == ["docker", "exec"] and argv[-2] == "python"
    assert argv[-1] == "/opt/dai/kernel_runner.py"


def test_two_executions_use_same_session():
    """连续两次 execute 针对同一 container/session"""
    km, _session = _live_session(1)

    with patch('redis.from_url'), \
         patch('subprocess.run', side_effect=_docker_run()), \
         patch('app.services.kernel_manager.subprocess.Popen') as mock_popen:
        mock_popen.side_effect = [
            FakeProcess(stdout='{"outputs":[]}'),
            FakeProcess(stdout='{"outputs":[]}'),
        ]
        km.execute(1, "x = 1")
        km.execute(1, "print(x)")

    assert mock_popen.call_count == 2
    # 两次 exec 同一个容器
    containers = {c[0][0][3] for c in mock_popen.call_args_list}
    assert containers == {"dai-kernel-rec-1"}


# ═══════════════════════════════════════════════════════════════
# 2. 超时语义：先中断保留变量，只有内核无响应才重建
# ═══════════════════════════════════════════════════════════════

def test_timeout_with_responsive_kernel_preserves_variables_and_container():
    """超时 → 只中断当前执行：不 rm 容器、session 不变、state_lost=False。"""
    km, old_session = _live_session(1)

    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.eval.return_value = 1
    proc = HangingProcess(partial_stdout='{"outputs":[{"msg_type":"stream","content":{"text":"1/3"}}]}')

    with patch('redis.from_url', return_value=fake_redis), \
         patch('subprocess.run', side_effect=_docker_run(on_kill=lambda: setattr(proc, "interrupted", True))), \
         patch('app.services.kernel_manager.subprocess.Popen', return_value=proc):
        result = km.execute(1, "while True: pass", timeout_seconds=1)

    assert result["outcome"] == "timeout"
    assert result["state_lost"] is False
    assert "已保留" in result["message"]
    # 部分输出保留，且变量未被丢弃（没有销毁重建）
    assert result["outputs"][0]["content"]["text"] == "1/3"
    assert km._sessions[1] is old_session
    assert proc.interrupted is True, "超时必须中断当前执行"


def test_timeout_with_unresponsive_kernel_rebuilds_and_reports_state_lost():
    """内核无响应 → 销毁重建 + state_lost=True + 明确「请从头重跑」文案。"""
    km, old_session = _live_session(1)

    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.eval.return_value = 1
    proc = HangingProcess(partial_stdout='{"outputs":[]}')
    removed = []

    def fake_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and args[1] == "rm":
            removed.append(args)
        if isinstance(args, (list, tuple)) and args[1] == "exec" and "kill" in args:
            proc.interrupted = True
        if isinstance(args, (list, tuple)) and args[1] == "exec":
            return MagicMock(returncode=1, stdout="", stderr="no reply")  # 探测无响应
        return MagicMock(returncode=0, stdout="container-id\n")

    with patch('redis.from_url', return_value=fake_redis), \
         patch('subprocess.run', side_effect=fake_run), \
         patch('app.services.kernel_manager.subprocess.Popen', return_value=proc), \
         patch.object(km, '_generate_conn_file', return_value=('/tmp/c.json', {
             'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
             'control_port': 4, 'hb_port': 5, 'ip': '0.0.0.0'})), \
         patch('os.makedirs'), \
         patch('time.sleep'):

        result = km.execute(1, "while True: pass", timeout_seconds=1)

    assert result["outcome"] == "failed"
    assert result["state_lost"] is True
    assert "状态已丢失" in result["message"]
    assert "从头" in result["message"]
    assert removed, "内核无响应必须 rm 旧容器"
    assert km._sessions[1] is not old_session, "重建后的 session 必须替换旧 session"
    fake_redis.delete.assert_any_call("kernel:session:1")


def test_runner_infrastructure_failure_is_not_reported_as_cell_success():
    """A broken trusted runner must fail the request, not become a fake output."""
    km, _session = _live_session(1)
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.eval.return_value = 1

    with patch("redis.from_url", return_value=fake_redis), \
         patch("subprocess.run", side_effect=_docker_run()), \
         patch("app.services.kernel_manager.subprocess.Popen",
               return_value=FakeProcess(returncode=1, stderr="runner crashed")):
        with pytest.raises(RuntimeError, match="runner crashed"):
            km.execute(1, "print(1)")


# ═══════════════════════════════════════════════════════════════
# 3. Docker argv 精确断言
# ═══════════════════════════════════════════════════════════════

def test_docker_argv_exact_security_params():
    """精确匹配 --network none, --cap-drop ALL, no-new-privileges, read-only,
    tmpfs size=64m, cpus=1, memory=256m, pids=50, /work:rw, 无 -p"""
    km = KernelManager()
    with patch.object(km, '_generate_conn_file') as mock_gen, \
         patch('subprocess.run') as mock_run, \
         patch('os.makedirs'), \
         patch('time.sleep'):
        mock_gen.return_value = ('/tmp/c.json', {
            'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
            'control_port': 4, 'hb_port': 5, 'ip': '0.0.0.0',
        })
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
            MagicMock(returncode=0, stdout='abc\n'),
        ]
        try:
            km.create_session(1, '')
        except Exception:
            pass

    run_calls = [c for c in mock_run.call_args_list
                 if len(c[0][0]) > 2 and c[0][0][1] == 'run']
    assert len(run_calls) >= 1
    argv = run_calls[0][0][0]

    # positional checks
    assert argv[0] == 'docker' and argv[1] == 'run'
    assert ['--network', 'none'] in _pairs(argv)
    assert ['--cap-drop', 'ALL'] in _pairs(argv)
    assert ['--security-opt', 'no-new-privileges'] in _pairs(argv)
    assert '--read-only' in argv
    assert any('size=64m' in a for a in argv), f"tmpfs must have size=64m: {argv}"
    assert ['--cpus', '1'] in _pairs(argv)
    assert ['--memory', '256m'] in _pairs(argv)
    assert ['--pids-limit', '50'] in _pairs(argv)
    # /work:rw
    assert any('/work:rw' in a for a in argv), f"missing /work:rw: {argv}"
    # no -p
    assert not any(a == '-p' for a in argv), f"must not have -p: {argv}"


def _pairs(lst):
    """将列表转换为相邻对"""
    return [list(lst[i:i+2]) for i in range(len(lst) - 1)]


# ═══════════════════════════════════════════════════════════════
# 4. Hidden init
# ═══════════════════════════════════════════════════════════════

def test_hidden_init_executes_in_order_and_skips_second_time():
    """两个 hidden cell 按 order 执行成功 → marker → 第二次 0 执行"""
    km = KernelManager()
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.get.return_value = None  # not initialized yet

    record_id = 1
    version = type('V', (), {
        'cells': [
            {'id': 'h1', 'source': 'a=1', 'order': 1, 'type': 'code', 'source_hidden': True},
            {'id': 'h2', 'source': 'b=2', 'order': 0, 'type': 'code', 'source_hidden': True},
        ]
    })()

    exec_calls = []
    def fake_execute(rid, code, **kwargs):
        exec_calls.append((rid, code))
        return {"outputs": [], "execution_time_ms": 5}

    km.execute = fake_execute
    # mock get_or_create_session to succeed
    session = KernelSession(record_id, 'test-c', {'ip': '127.0.0.1'})
    km.get_or_create_session = lambda rid, d='': session
    km._sessions[record_id] = session

    with patch('redis.from_url', return_value=fake_redis), \
         patch('app.api.experiments.get_kernel_manager', return_value=km):
        from app.api.experiments import _init_hidden_cells_once
        _init_hidden_cells_once(
            type('R', (), {'id': record_id, 'cells_outputs': {}, 'template_version_id': 1})(),
            version,
            type('D', (), {})(),
        )

    assert len(exec_calls) == 2
    assert exec_calls[0][1] == 'b=2'  # order 0
    assert exec_calls[1][1] == 'a=1'  # order 1

    # Second init → 0 execute (标记已存在)
    fake_redis.get.return_value = str(1)  # version 1 already initialized
    exec_calls.clear()
    with patch('redis.from_url', return_value=fake_redis), \
         patch('app.api.experiments.get_kernel_manager', return_value=km):
        _init_hidden_cells_once(
            type('R2', (), {'id': record_id, 'cells_outputs': {}, 'template_version_id': 1})(),
            version,
            type('D2', (), {})(),
        )
    assert len(exec_calls) == 0


def test_hidden_init_second_cell_failure_destroys_and_no_marker():
    """第二个 hidden cell 失败 → destroy + KERNEL_INIT_FAILED + 无 marker"""
    km = KernelManager()
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.get.return_value = None

    destroy_calls = []
    km.destroy = lambda rid: destroy_calls.append(rid)
    session = KernelSession(1, 'test-c', {'ip': '127.0.0.1'})
    km.get_or_create_session = lambda rid, d='': session
    km._sessions[1] = session

    version = type('V', (), {
        'cells': [
            {'id': 'h1', 'source': 'ok', 'order': 0, 'type': 'code', 'source_hidden': True},
            {'id': 'h2', 'source': 'fail', 'order': 1, 'type': 'code', 'source_hidden': True},
        ]
    })()

    def fake_execute(rid, code, **kwargs):
        if code == 'fail':
            raise RuntimeError("hidden init failed")
        return {"outputs": [], "execution_time_ms": 5}

    km.execute = fake_execute

    with patch('redis.from_url', return_value=fake_redis), \
         patch('app.api.experiments.get_kernel_manager', return_value=km):
        with pytest.raises(Exception) as exc_info:
            from app.api.experiments import _init_hidden_cells_once
            _init_hidden_cells_once(
                type('R', (), {'id': 1, 'cells_outputs': {}, 'template_version_id': 1})(),
                version,
                type('D', (), {})(),
            )

    assert len(destroy_calls) == 1
    assert 'KERNEL_INIT_FAILED' in str(exc_info.value)


def test_hidden_init_marker_persistence_failure_destroys_kernel():
    """A marker that cannot be persisted must not leave an ambiguous kernel."""
    km = KernelManager()
    session = KernelSession(1, "test-c", {"ip": "127.0.0.1"})
    km._sessions[1] = session
    km.get_or_create_session = lambda rid, d="": session
    km.execute = lambda rid, code, **kwargs: {"outputs": [], "execution_time_ms": 1}
    destroyed = []
    km.destroy = lambda rid: destroyed.append(rid)

    version = type("V", (), {
        "cells": [{
            "id": "hidden",
            "source": "seed = 1",
            "order": 0,
            "type": "code",
            "source_hidden": True,
        }],
    })()
    fake_redis = MagicMock()
    fake_redis.get.return_value = None
    fake_redis.setex.side_effect = ConnectionError("redis down")

    with patch("redis.from_url", return_value=fake_redis), \
         patch("app.api.experiments.get_kernel_manager", return_value=km):
        from app.api.experiments import _init_hidden_cells_once

        with pytest.raises(Exception) as exc_info:
            _init_hidden_cells_once(
                type("R", (), {"id": 1, "template_version_id": 7})(),
                version,
                type("D", (), {})(),
            )

    assert "KERNEL_INIT_FAILED" in str(exc_info.value)
    assert destroyed == [1]


def test_destroy_clears_all_session_and_initialization_metadata():
    km = KernelManager()
    km._sessions[1] = KernelSession(
        1,
        "test-c",
        {"ip": "127.0.0.1"},
        initialized_template_version_id=7,
    )
    fake_redis = MagicMock()

    with patch("redis.from_url", return_value=fake_redis), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        km.destroy(1)

    fake_redis.delete.assert_any_call("kernel:session:1")
    fake_redis.delete.assert_any_call("kernel:init:1")


def test_restart_preserves_existing_course_mount_by_default():
    km = KernelManager()
    km._sessions[1] = KernelSession(
        1,
        "test-c",
        {"ip": "127.0.0.1"},
        lesson_storage_dir="C:/course-assets",
    )

    with patch.object(km, "destroy"), \
         patch.object(km, "create_session", return_value=MagicMock()) as create:
        km.restart(1)

    # Phase 5：restart 沿用现有 session 的环境身份（digest/环境版本从 session 继承）
    create.assert_called_once_with(1, "C:/course-assets", image_ref=None, environment_version_id=None, memory_mb=256, cpus=1.0, resource_factory=None)


# ═══════════════════════════════════════════════════════════════
# 5. Recovery with empty Redis
# ═══════════════════════════════════════════════════════════════

def test_recover_empty_redis_via_label_and_conn_file():
    """Docker label 恢复 + conn file → 内存 session + Redis setex"""
    km = KernelManager()
    fake_redis = MagicMock()
    fake_redis.get.return_value = None  # Redis empty

    def mock_run(args, **kwargs):
        cmd_str = ' '.join(str(a) for a in args)
        if 'docker ps' in cmd_str:
            return MagicMock(returncode=0, stdout='abc123\n')
        if 'docker inspect' in cmd_str:
            if '{{.Name}}' in cmd_str:
                return MagicMock(returncode=0, stdout='/dai-kernel-rec-42')
            if 'dai.record_id' in cmd_str:
                return MagicMock(returncode=0, stdout='42')
            # Phase 5：恢复必须校验环境 label
            if 'dai.environment_version_id' in cmd_str:
                return MagicMock(returncode=0, stdout='7')
            if 'dai.image_digest' in cmd_str:
                return MagicMock(returncode=0, stdout='sha256:' + 'b' * 64)
        if 'docker exec' in cmd_str and 'cat' in cmd_str:
            return MagicMock(returncode=0, stdout='{"shell_port":1,"ip":"127.0.0.1"}')
        return MagicMock(returncode=0, stdout='')

    with patch('subprocess.run', side_effect=mock_run), \
         patch('redis.from_url', return_value=fake_redis):
        km.recover_from_docker()

    assert 42 in km._sessions
    # Redis setex 被调用来重建元数据
    assert fake_redis.setex.called or fake_redis.set.called


# ═══════════════════════════════════════════════════════════════
# 6. Redis lock fail → docker exec never called
# ═══════════════════════════════════════════════════════════════

def test_redis_lock_failure_skips_docker_exec_and_fails_closed():
    """Redis 锁获取失败 → docker exec 从未调用 → RuntimeError"""
    km = KernelManager()
    session = KernelSession(1, 'test-container', {"ip": "127.0.0.1"})
    session._is_alive = True
    km._sessions[1] = session

    with patch('redis.from_url', side_effect=ConnectionError('redis down')), \
         patch('subprocess.run') as mock_run:
        with pytest.raises(RuntimeError, match='Redis'):
            km.execute(1, "print(1)")

    # docker exec 从未调用
    exec_calls = [c for c in mock_run.call_args_list
                  if len(c[0][0]) > 2 and 'exec' in c[0][0]]
    assert len(exec_calls) == 0, "Redis fail: docker exec must NOT be called"


def test_redis_lock_release_uses_token_safe_lua():
    """锁释放用 Lua eval token-safe，不是简单 del"""
    km, _session = _live_session(1, image_ref=None)

    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.eval.return_value = 1

    with patch('redis.from_url', return_value=fake_redis), \
         patch('subprocess.run', side_effect=_docker_run()), \
         patch('app.services.kernel_manager.subprocess.Popen',
               return_value=FakeProcess(stdout='{"outputs":[]}')):
        km.execute(1, "print(1)")

    assert fake_redis.eval.call_count >= 1, "must use Lua eval for token-safe unlock"
