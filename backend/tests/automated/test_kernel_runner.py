"""Kernel 第二轮返工 RED 测试 — 只测 Kernel"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from app.services.kernel_manager import (
    KernelManager,
    KernelSession,
    get_kernel_manager,
)


# ═══════════════════════════════════════════════════════════════
# 1. Runner 连接持久 ipykernel，代码走 stdin
# ═══════════════════════════════════════════════════════════════

def test_runner_argv_has_trusted_script_and_no_student_code():
    """docker exec argv 含 /.dai/kernel_runner.py 和 /tmp/conn.json，不含学生代码"""
    km = KernelManager()
    session = KernelSession(1, "dai-kernel-rec-1", {"ip": "127.0.0.1"})
    session._is_alive = True
    km._sessions[1] = session

    tricky_code = 'print("hello\\nworld")\nx = """quotes"""\n# backslash: \\'

    with patch('redis.from_url'), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"outputs":[]}')

        km.execute(1, tricky_code)

    exec_calls = [c for c in mock_run.call_args_list
                  if len(c[0][0]) > 2 and 'exec' in c[0][0]]
    assert len(exec_calls) >= 1, "must have docker exec call"
    argv = exec_calls[0][0][0]
    argv_str = ' '.join(str(a) for a in argv)

    # trusted runner path (in argv)
    assert '/.dai/kernel_runner.py' in argv or 'kernel_runner.py' in argv_str
    # conn file NOT in argv（runner 内部读取容器内的文件）
    # no student code in argv
    assert 'hello' not in argv_str
    assert '"""quotes"""' not in argv_str
    # stdin 传递的 input JSON 包含代码
    input_arg = exec_calls[0][1].get('input')
    assert input_arg is not None, "代码应通过 stdin input= 传递"
    decoded = json.loads(input_arg)
    assert decoded['code'] == tricky_code


def test_environment_session_uses_canonical_runner_path():
    """绑定环境版本的 Kernel 必须调用环境镜像内的 canonical runner。"""
    km = KernelManager()
    session = KernelSession(
        1,
        "dai-kernel-rec-1",
        {"ip": "127.0.0.1"},
        image_ref="sha256:" + "a" * 64,
        environment_version_id=2,
    )
    session._is_alive = True
    km._sessions[1] = session

    with patch('redis.from_url'), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"outputs":[]}')
        km.execute(1, "print(1)")

    exec_calls = [
        c for c in mock_run.call_args_list
        if len(c[0][0]) > 4 and c[0][0][0:2] == ["docker", "exec"]
        and c[0][0][-2] == "python"
    ]
    assert len(exec_calls) == 1
    assert exec_calls[0][0][0][-1] == "/opt/dai/kernel_runner.py"


def test_two_executions_use_same_session():
    """连续两次 execute 针对同一 container/session"""
    km = KernelManager()
    session = KernelSession(1, "dai-kernel-rec-1", {"ip": "127.0.0.1"})
    session._is_alive = True
    km._sessions[1] = session

    with patch('redis.from_url'), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"outputs":[]}')
        km.execute(1, "x = 1")
        km.execute(1, "print(x)")

    exec_calls = [c for c in mock_run.call_args_list
                  if len(c[0][0]) > 2 and 'exec' in c[0][0]]
    assert len(exec_calls) == 2
    # 两次 exec 同一个容器
    containers = {c[0][0][3] for c in exec_calls}  # docker exec -i <name> ...
    assert len(containers) == 1


# ═══════════════════════════════════════════════════════════════
# 2. 超时销毁 + 立即重建
# ═══════════════════════════════════════════════════════════════

def test_timeout_destroys_and_recreates():
    """docker exec 超时 → rm old → create new → session 在内存和 Redis 中"""
    km = KernelManager()
    old_session = KernelSession(1, "dai-kernel-rec-1", {"ip": "127.0.0.1"})
    old_session._is_alive = True
    km._sessions[1] = old_session

    fake_redis = MagicMock()
    fake_redis.set.return_value = True

    with patch('redis.from_url', return_value=fake_redis), \
         patch.object(km, '_generate_conn_file') as mock_gen, \
         patch('subprocess.run') as mock_run, \
         patch('os.makedirs'), \
         patch('time.sleep'):

        mock_gen.return_value = ('/tmp/c.json', {
            'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
            'control_port': 4, 'hb_port': 5, 'ip': '0.0.0.0',
        })

        # 第一次 subprocess.run: docker exec → TimeoutExpired
        def run_side_effect(args, **kwargs):
            if 'exec' in args:
                raise subprocess.TimeoutExpired(args, 30)
            return MagicMock(returncode=0, stdout='ok')

        mock_run.side_effect = run_side_effect

        with pytest.raises(RuntimeError, match='超时|timeout|TIMEOUT'):
            km.execute(1, "while True: pass")

    # 断言 destroy（rm -f）
    rm_calls = [c for c in mock_run.call_args_list
                if len(c[0][0]) >= 3 and 'rm' in c[0][0]]
    assert len(rm_calls) >= 1, "超时后必须 rm 旧容器"

    # 断言 timeout=30（subprocess.run 的 kwarg）
    exec_calls = [c for c in mock_run.call_args_list if 'exec' in str(c[0][0])]
    assert len(exec_calls) >= 1
    # subprocess.run(args, timeout=N) — timeout is a kwarg
    assert exec_calls[0][1].get('timeout') == 30, f"timeout={exec_calls[0][1].get('timeout')}"
    assert 1 in km._sessions
    assert km._sessions[1] is not old_session
    assert fake_redis.setex.called


def test_runner_infrastructure_failure_is_not_reported_as_cell_success():
    """A broken trusted runner must fail the request, not become a fake output."""
    km = KernelManager()
    session = KernelSession(1, "dai-kernel-rec-1", {"ip": "127.0.0.1"})
    km._sessions[1] = session
    fake_redis = MagicMock()
    fake_redis.set.return_value = True

    def run_side_effect(args, **kwargs):
        if args[1] == "ps":
            return MagicMock(returncode=0, stdout="container-id\n")
        return MagicMock(returncode=1, stdout="", stderr="runner crashed")

    with patch("redis.from_url", return_value=fake_redis), \
         patch("subprocess.run", side_effect=run_side_effect):
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
    def fake_execute(rid, code):
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

    def fake_execute(rid, code):
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
    km.execute = lambda rid, code: {"outputs": [], "execution_time_ms": 1}
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
    km = KernelManager()
    session = KernelSession(1, 'test-container', {"ip": "127.0.0.1"})
    session._is_alive = True
    km._sessions[1] = session

    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    fake_redis.eval.return_value = 1

    with patch('redis.from_url', return_value=fake_redis), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"outputs":[]}')
        km.execute(1, "print(1)")

    assert fake_redis.eval.call_count >= 1, "must use Lua eval for token-safe unlock"
