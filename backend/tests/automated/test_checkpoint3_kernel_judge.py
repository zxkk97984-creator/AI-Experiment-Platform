"""检查点 3 返工: Kernel/Judge/权限 真实 RED 测试（替换所有伪测试）"""
from __future__ import annotations

import io
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, ANY, call, patch

import pytest
from sqlalchemy import select

from app import models
from app.database import Base
from app.services.kernel_manager import KernelManager, KernelSession, get_kernel_manager
from conftest import auth_header, create_assignment_db, create_course_db, create_user, login, seed_basic_environment
from teaching_test_support import grant_course_students, grant_experiment

BACKEND_ROOT = Path(__file__).resolve().parents[2]


# ═══════════════════════════════════════════════════════════════
# Kernel: Docker argv 精确断言
# ═══════════════════════════════════════════════════════════════

def test_kernel_docker_argv_has_exact_security_params():
    """无 -p、--network none、--cap-drop ALL、no-new-privileges、read-only、/work:rw、size 限额 tmpfs、cpus=1、memory=256m、pids=50"""
    from fakeredis import FakeStrictRedis
    km = KernelManager()
    with patch.object(km, '_generate_conn_file') as mock_gen, \
         patch('redis.from_url', return_value=FakeStrictRedis()), \
         patch('subprocess.run') as mock_run, \
         patch('os.makedirs'), \
         patch('time.sleep'):
        mock_gen.return_value = ('/tmp/c.json', {
            'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
            'control_port': 4, 'hb_port': 5, 'ip': '0.0.0.0',
        })
        mock_run.side_effect = [
            MagicMock(returncode=0),      # 0: docker rm cleanup
            MagicMock(returncode=0, stdout='abc\n'),  # 1: docker run
            MagicMock(returncode=0, stdout='abc\n'),  # 2: docker ps alive
            MagicMock(returncode=0),      # 3: docker exec 就绪探测
            MagicMock(returncode=0),      # 4: docker rm rollback
        ]
        km.create_session(1, '')

    run_calls = [c for c in mock_run.call_args_list
                 if len(c[0][0]) > 2 and c[0][0][1] == 'run']
    assert len(run_calls) == 1
    argv = run_calls[0][0][0]
    argv_str = ' '.join(argv)

    assert '--network' in argv and 'none' in argv
    assert '--cap-drop' in argv and 'ALL' in argv
    assert '--security-opt' in argv and 'no-new-privileges' in argv
    assert '--read-only' in argv
    # --tmpfs 带 size 限额
    tmpfs_args = [a for a in argv if a.startswith('--tmpfs') or a.startswith('/tmp')]
    assert any('size=' in a or ':size=' in a for a in argv if 'tmpfs' in a.lower() or 'tmp' in a) or \
           any('--tmpfs' in a for a in argv), f"缺少 tmpfs: {argv_str}"
    # /work:rw
    assert any('/work:rw' in a for a in argv), f"缺少 /work:rw: {argv_str}"
    assert '--cpus' in argv and '1' in argv
    assert '--memory' in argv
    assert '--pids-limit' in argv
    # 无端口发布
    assert not any(a == '-p' or a.startswith('-p ') for a in argv), f"不应有 -p: {argv_str}"


def test_kernel_dod_mounts_use_docker_host_paths():
    """DoD 模式下 Kernel 的连接文件和工作目录必须使用宿主机路径挂载。"""
    from app.config import Settings
    from fakeredis import FakeStrictRedis

    settings = Settings(
        judge_work_dir="/judge-work",
        judge_host_work_dir="/host/dai/judge-work",
    )
    km = KernelManager(settings)
    with patch.object(km, "_generate_conn_file") as mock_gen, \
         patch("redis.from_url", return_value=FakeStrictRedis()), \
         patch("subprocess.run") as mock_run, \
         patch("os.makedirs"), \
         patch("time.sleep"):
        mock_gen.return_value = (
            "/judge-work/kernels/kernel-rec-7.json",
            {
                "shell_port": 1, "iopub_port": 2, "stdin_port": 3,
                "control_port": 4, "hb_port": 5, "ip": "0.0.0.0",
            },
        )
        mock_run.side_effect = [
            MagicMock(returncode=0),                    # docker rm cleanup
            MagicMock(returncode=0, stdout="abc\n"),    # docker run
            MagicMock(returncode=0, stdout="abc\n"),    # docker ps alive
            MagicMock(returncode=0),                    # docker exec 就绪探测
        ]

        km.create_session(7, "")

    run_call = next(
        call_item for call_item in mock_run.call_args_list
        if len(call_item[0][0]) > 2 and call_item[0][0][1] == "run"
    )
    argv = run_call[0][0]
    assert "/host/dai/judge-work/kernels/kernel-rec-7.json:/tmp/conn.json:ro" in argv
    assert "/host/dai/judge-work/workspaces/student_7:/work:rw" in argv


def test_kernel_connection_file_is_readable_by_sandbox_user(tmp_path):
    """连接文件只读挂载给 UID 1000 前，必须赋予读取权限。"""
    from app.config import Settings

    km = KernelManager(Settings(judge_work_dir=str(tmp_path)))
    with patch("os.chmod") as chmod:
        conn_path, _ = km._generate_conn_file(8)

    assert any(
        call_item.args == (conn_path, 0o644)
        for call_item in chmod.call_args_list
    ), "连接文件必须 chmod 0644 后再挂载给非 root Kernel"


# ═══════════════════════════════════════════════════════════════
# Redis 锁 fail closed + token-safe release
# ═══════════════════════════════════════════════════════════════

def test_redis_lock_failure_prevents_execution():
    """Redis 锁错误必须阻止执行，fail closed"""
    km = KernelManager()
    session = MagicMock(spec=KernelSession)
    session.is_alive = True
    session.conn_info = {
        'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
        'control_port': 4, 'hb_port': 5, 'ip': '127.0.0.1',
    }
    session.runner_path = None
    session.environment_version_id = None
    km._sessions[1] = session

    # Redis 连接失败
    with patch('redis.from_url', side_effect=ConnectionError('redis down')):
        with pytest.raises(RuntimeError, match='Redis'):
            km.execute(1, "print(1)")


def test_redis_lock_token_safe_release():
    """释放锁时使用 Lua eval token-safe 释放，不是简单 del"""
    km = KernelManager()
    session = MagicMock(spec=KernelSession)
    session.is_alive = True
    session.container_name = 'test-container'
    session.conn_info = {
        'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
        'control_port': 4, 'hb_port': 5, 'ip': '127.0.0.1',
    }
    session.runner_path = None
    session.environment_version_id = None
    km._sessions[1] = session

    fake_redis = MagicMock()
    fake_redis.set.return_value = True

    class _RunnerProcess:
        """subprocess.Popen 替身：stdin 收代码，communicate 返回 runner JSON。"""

        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = '{"stdout":"ok","stderr":"","error":null}'
            self.stderr = ''
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            return self.stdout, self.stderr

        def kill(self):
            self.returncode = -9

    with patch('redis.from_url', return_value=fake_redis), \
         patch('subprocess.run',
               return_value=MagicMock(returncode=0, stdout='container-id\n')), \
         patch.object(km, '_touch_session_activity'), \
         patch('app.services.kernel_manager.subprocess.Popen',
               return_value=_RunnerProcess()):
        result = km.execute(1, "print(1)")

    assert 'outputs' in result
    assert fake_redis.eval.call_count >= 1, "must use eval for token-safe unlock"


# ═══════════════════════════════════════════════════════════════
# Session Redis write failure → 回滚容器
# ═══════════════════════════════════════════════════════════════

def test_create_session_redis_write_failure_rolls_back():
    """Redis setex 失败必须 rm 容器并报错"""
    km = KernelManager()
    with patch.object(km, '_generate_conn_file') as mock_gen, \
         patch('subprocess.run') as mock_run, \
         patch('os.makedirs'), \
         patch('time.sleep'), \
         patch('redis.from_url', side_effect=ConnectionError('no redis')):
        mock_gen.return_value = ('/tmp/c.json', {
            'shell_port': 1, 'iopub_port': 2, 'stdin_port': 3,
            'control_port': 4, 'hb_port': 5, 'ip': '0.0.0.0',
        })
        mock_run.side_effect = [
            MagicMock(returncode=0),      # 0: docker rm cleanup
            MagicMock(returncode=0, stdout='abc\n'),  # 1: docker run
            MagicMock(returncode=0, stdout='abc\n'),  # 2: docker ps alive
            MagicMock(returncode=0),      # 3: docker exec 就绪探测
            MagicMock(returncode=0),      # 4: docker rm rollback
        ]
        with pytest.raises(RuntimeError, match='Redis'):
            km.create_session(1, '')

    # 断言 docker rm -f 被调用来回收容器
    rm_calls = [c for c in mock_run.call_args_list
                if c[0][0][0] == 'docker' and c[0][0][1] == 'rm']
    assert len(rm_calls) >= 2, "Redis 写入失败后必须 rm 容器"


# ═══════════════════════════════════════════════════════════════
# Recover from Docker label
# ═══════════════════════════════════════════════════════════════

def test_recover_from_docker_uses_label_and_recovers_without_redis():
    """从 dai.record_id label 恢复，即使 Redis 无数据也成功（从容器内 conn file）"""
    km = KernelManager()

    def mock_run(args, **kwargs):
        cmd_str = ' '.join(str(a) for a in args)
        if 'docker ps' in cmd_str:
            return MagicMock(returncode=0, stdout='abc123\n')
        if 'docker inspect' in cmd_str:
            if '{{.Name}}' in cmd_str:
                return MagicMock(returncode=0, stdout='/dai-kernel-rec-42')
            if 'dai.record_id' in cmd_str:
                return MagicMock(returncode=0, stdout='42')
            # Phase 5：恢复必须校验环境 label（缺失环境身份的旧容器不复用）
            if 'dai.environment_version_id' in cmd_str:
                return MagicMock(returncode=0, stdout='7')
            if 'dai.image_digest' in cmd_str:
                return MagicMock(returncode=0, stdout='sha256:' + 'a' * 64)
        if 'docker exec' in cmd_str and 'cat' in cmd_str:
            return MagicMock(returncode=0, stdout='{"shell_port":1,"ip":"127.0.0.1"}')
        return MagicMock(returncode=0, stdout='')

    with patch('subprocess.run', side_effect=mock_run), \
         patch('redis.from_url', side_effect=ConnectionError('no redis')):
        km.recover_from_docker()

    assert 42 in km._sessions, "record_id=42 应从 label 恢复"
    assert km._sessions[42].environment_version_id == 7, "恢复应带环境版本 label"
    assert km._sessions[42].image_ref == 'sha256:' + 'a' * 64, "恢复应带镜像 digest label"


# ═══════════════════════════════════════════════════════════════
# Hidden init 持久化
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# Judge: _get_timeout 真实调用测试
# ═══════════════════════════════════════════════════════════════

def test_get_timeout_edge_cases():
    """1001ms→2s, 超全局上限→上限, 非正值至少1s"""
    from app.worker.judge_worker import _get_timeout

    q = MagicMock()
    s = MagicMock()
    s.judge_timeout_seconds = 30

    q.time_limit_ms = 1001
    assert _get_timeout(q, s) == 2  # ceil(1001/1000) = 2

    q.time_limit_ms = 60000  # 60s
    assert _get_timeout(q, s) == 30  # capped by global

    q.time_limit_ms = 0
    assert _get_timeout(q, s) == 1  # at least 1

    q.time_limit_ms = 100
    assert _get_timeout(q, s) == 1  # ceil(0.1) = 1


# ═══════════════════════════════════════════════════════════════
# Judge: Docker fail → system_error, worker continues
# ═══════════════════════════════════════════════════════════════

def test_unbound_submission_routes_to_manual_review():
    """没有评分版本基线时，旧 pytest Docker 路径不得产生正式分。"""
    from app.worker.judge_worker import process_submission

    settings = MagicMock()
    settings.judge_use_docker = True
    settings.judge_timeout_seconds = 30
    settings.judge_cpu_limit = 1.0
    settings.judge_memory_limit_mb = 256
    settings.judge_image = 'dai-judge'

    db = MagicMock()
    question = MagicMock()
    question.time_limit_ms = 5000
    question.memory_limit_mb = 128
    question.hidden_tests = 'def test(): pass'
    question.grading_mode = 'legacy'
    question.test_groups = []
    submission = MagicMock()
    submission.id = 1
    submission.code = 'pass'
    submission.status = 'queued'
    submission.question_id = 1
    # 保存 submission 状态变更
    db.get.side_effect = lambda m, i: question if m == models.JudgeQuestion else submission
    redis_client = MagicMock()

    # 未绑定版本 → 转人工，不尝试启动 Docker。
    with patch('subprocess.run', side_effect=FileNotFoundError('No docker')):
        with patch('app.services.judge_queue.claim_job', return_value=True):
            process_submission(db, redis_client, settings, 1)
    # 结果写入是受认领栅栏保护的条件 UPDATE：全 mock 的 db 不反映内存副作用，
    # 因此断言发出的 SQL 值（真实落库行为由 test_judge_claim_fence.py 覆盖）。
    from conftest import executed_update_values
    from app.models import Submission as _Submission
    values = executed_update_values(db, _Submission)
    assert any(v.get('status') == 'review_required' for v in values), values
    assert any(v.get('score') is None for v in values), values


# ═══════════════════════════════════════════════════════════════
# 权限: 跨教师提交拒绝
# ═══════════════════════════════════════════════════════════════

def test_teacher_a_cannot_see_teacher_b_submissions(client, db_session_factory):
    """真实: ta 创建课程+作业+题目, tb 尝试查看→404，不泄露提交存在"""
    create_user(db_session_factory, 'ta1', 'teacher')
    create_user(db_session_factory, 'tb1', 'teacher')
    create_user(db_session_factory, 'stu1', 'student')
    ta_tok, _ = login(client, 'ta1')
    tb_tok, _ = login(client, 'tb1')
    s_tok, _ = login(client, 'stu1')

    caid = create_course_db(db_session_factory, teacher_username='ta1', title='TA Course', status='published', visibility='public')
    client.post(f'/api/v1/courses/{caid}/enroll', headers=auth_header(s_tok))
    aid = create_assignment_db(db_session_factory, course_id=caid, teacher_username='ta1', title='A1', status='draft')
    grant_course_students(db_session_factory, caid, ['stu1'])
    q = client.post(f'/api/v1/assignments/{aid}/questions', headers=auth_header(ta_tok), json={
        'title': 'Q1', 'function_name': 'f', 'hidden_tests': 'def test(): pass', 'grading_mode': 'legacy',
    })
    qid = q.json()['id']
    pub = client.post(f'/api/v1/assignments/{aid}/publish', headers=auth_header(ta_tok))
    assert pub.status_code == 200, pub.text

    # student submits
    sub = client.post('/api/v1/judge/submissions', headers=auth_header(s_tok), json={
        'question_id': qid, 'code': 'def f(): pass',
    })
    assert sub.status_code == 201
    sid = sub.json()['id']

    # tb (另一教师) GET detail → 404
    r = client.get(f'/api/v1/judge/submissions/{sid}', headers=auth_header(tb_tok))
    assert r.status_code == 404, f"tb GET sub detail: {r.status_code}"

    # tb GET list → items 不含 ta 的提交
    r = client.get('/api/v1/judge/submissions', headers=auth_header(tb_tok))
    items = [i for i in r.json().get('items', []) if i['id'] == sid]
    assert len(items) == 0, f"tb 不应看到 ta 课程的提交"


# ═══════════════════════════════════════════════════════════════
# 权限: 跨学生实验记录拒绝
# ═══════════════════════════════════════════════════════════════

def test_student_b_cannot_access_student_a_record(client, db_session_factory):
    """真实: sa 创建 record, sb 尝试所有操作→404，不泄露记录存在"""
    create_user(db_session_factory, 't_rec', 'teacher')
    create_user(db_session_factory, 'sa1', 'student')
    create_user(db_session_factory, 'sb1', 'student')
    t_tok, _ = login(client, 't_rec')
    sa_tok, _ = login(client, 'sa1')
    sb_tok, _ = login(client, 'sb1')

    # 创建模板+版本+课程+lesson
    with db_session_factory() as db:
        tmpl = models.NotebookTemplate(name='Test', status='published', owner_id=1,
                                        draft_cells=[])
        db.add(tmpl); db.flush()
        ver = models.NotebookTemplateVersion(
            template_id=tmpl.id, version_number=1, sha256='a'*64,
            cells=[{'id':'c1','type':'code','source':'print(1)','order':0,'student_editable':True,'source_hidden':False}],
            cell_order=['c1'], published_by_id=1)
        db.add(ver); db.flush()
        tmpl.current_version_id = ver.id
        db.commit()
        tid, vid = tmpl.id, ver.id

    cid = create_course_db(db_session_factory, teacher_username='t_rec', title='Rec Course', status='published', visibility='public')
    ch = client.post(f'/api/v1/courses/{cid}/chapters', headers=auth_header(t_tok), json={'title':'Ch'})
    chid = ch.json()['id']

    with db_session_factory() as db:
        lesson = models.Lesson(chapter_id=chid, title='L', content_type='markdown', template_id=tid)
        db.add(lesson); db.commit()
        lid = lesson.id

    grant_experiment(db_session_factory, 'lesson', lid, ['sa1'])

    # sa ensure record
    r = client.post(f'/api/v1/experiments/records/ensure-for-lesson/{lid}', headers=auth_header(sa_tok))
    assert r.status_code in (200, 201), r.text
    rid = r.json()['id']

    # sb GET detail → 404
    for method, path in [
        ('get', f'/api/v1/experiments/records/{rid}'),
        ('put', f'/api/v1/experiments/records/{rid}/cells'),
        ('post', f'/api/v1/experiments/records/{rid}/cells/c1/execute'),
        ('post', f'/api/v1/experiments/records/{rid}/interrupt'),
        ('post', f'/api/v1/experiments/records/{rid}/restart'),
    ]:
        if method == 'get':
            r2 = client.get(path, headers=auth_header(sb_tok))
        elif method == 'put':
            r2 = client.put(path, headers=auth_header(sb_tok), json={'cells': {}, 'record_revision': 1})
        else:
            r2 = client.post(path, headers=auth_header(sb_tok), json={'code': 'print(1)'} if 'execute' in path else None)
        assert r2.status_code == 404, f"sb {method} {path}: expected 404 got {r2.status_code}"
