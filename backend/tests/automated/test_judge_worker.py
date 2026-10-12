"""Judge + sample-run 第三轮 RED 测试"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from app import models
from app.worker.judge_worker import (
    _get_timeout,
    _run_docker_pytest,
    _status_from_pytest,
    process_submission,
)
from conftest import auth_header, create_assignment_db, create_course_db, create_user, login
from teaching_test_support import grant_course_students


def _setup_course(client, db_session_factory, course_status="published", assignment_status="published"):
    create_user(db_session_factory, "t_j", "teacher")
    create_user(db_session_factory, "s_j", "student")
    t_tok, _ = login(client, "t_j")
    s_tok, _ = login(client, "s_j")
    cid = create_course_db(db_session_factory, teacher_username='t_j', title='Judge Course', status=course_status, visibility='public')
    if course_status == 'published':
        client.post(f'/api/v1/courses/{cid}/enroll', headers=auth_header(s_tok))
    a = client.post('/api/v1/assignments', headers=auth_header(t_tok), json={
        'course_id': cid, 'title': 'A1', 'status': 'draft',
    })
    aid = a.json()['id']
    q = client.post(f'/api/v1/assignments/{aid}/questions', headers=auth_header(t_tok), json={
        'title': 'Q1', 'function_name': 'add', 'grading_mode': 'legacy',
        'public_cases': [{'args': [1, 2], 'expected': 3}],
        'hidden_tests': 'HIDDEN_SENTINEL_XYZ\ndef test_hidden(): assert add(1,2)==3',
    })
    grant_course_students(db_session_factory, cid, ['s_j'])
    if assignment_status == 'published':
        pub = client.post(f'/api/v1/assignments/{aid}/publish', headers=auth_header(t_tok))
        assert pub.status_code == 200, pub.text
    qid = q.json()['id']
    return t_tok, s_tok, cid, aid, qid


# ═══════════════════════════════════════════════════════════════
# 1. sample-run 无 DB 副作用
# ═══════════════════════════════════════════════════════════════

def test_sample_run_no_submission_no_queue(client, db_session_factory):
    _, s_tok, _, _, qid = _setup_course(client, db_session_factory)
    with db_session_factory() as db:
        before = db.query(models.Submission).count()

    with patch('app.api.judge._run_docker_pytest') as mock_sandbox:
        mock_sandbox.return_value = ('1 passed', '', 0, 50)
        r = client.post(f'/api/v1/judge/questions/{qid}/sample-run',
                        headers=auth_header(s_tok),
                        json={'question_id': qid, 'code': 'def add(a,b): return a+b'})

    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'accepted'
    with db_session_factory() as db:
        after = db.query(models.Submission).count()
    assert after == before


# ═══════════════════════════════════════════════════════════════
# 2. 权限拒绝：未选课、draft assignment、draft course、teacher
# ═══════════════════════════════════════════════════════════════

def test_sample_run_permission_denied_all_cases(client, db_session_factory):
    # 未选课
    create_user(db_session_factory, "t_perm2", "teacher")
    create_user(db_session_factory, "s_perm2", "student")
    create_user(db_session_factory, "s_perm2_aud", "student")
    t_tok, _ = login(client, "t_perm2")
    s_tok, _ = login(client, "s_perm2")
    cid = create_course_db(db_session_factory, teacher_username='t_perm2', title='C', status='published', visibility='public')
    # 不选课（评分事实不可变：题必须在发布前建好）
    aid = create_assignment_db(db_session_factory, course_id=cid, teacher_username='t_perm2', title='A', status='draft')
    with db_session_factory() as db:
        aud = db.query(models.User).filter(models.User.username == "s_perm2_aud").first()
        aud_id = aud.id
    grant_course_students(db_session_factory, cid, ['s_perm2_aud'])
    # 当前发布要求有效 audience；s_perm2 保持未选课，另建一名受众学生用于发布
    r = client.patch(f'/api/v1/assignments/{aid}', headers=auth_header(t_tok), json={
        "audience_mode": "whitelist_only",
        "whitelist_student_ids": [aud_id],
    })
    assert r.status_code == 200, r.text
    q = client.post(f'/api/v1/assignments/{aid}/questions', headers=auth_header(t_tok), json={
        'title': 'Q', 'function_name': 'f', 'hidden_tests': 'def test(): pass', 'grading_mode': 'legacy',
    })
    qid = q.json()['id']
    pub = client.post(f'/api/v1/assignments/{aid}/publish', headers=auth_header(t_tok))
    assert pub.status_code == 200, pub.text

    r = client.post(f'/api/v1/judge/questions/{qid}/sample-run',
                    headers=auth_header(s_tok), json={'question_id': qid, 'code': 'pass'})
    assert r.status_code == 404, f"未授课授权的任务不泄露存在: {r.status_code}"

    # teacher
    r = client.post(f'/api/v1/judge/questions/{qid}/sample-run',
                    headers=auth_header(t_tok), json={'question_id': qid, 'code': 'pass'})
    assert r.status_code == 403, f"teacher: {r.status_code}"

    # draft assignment（用不同用户名）
    create_user(db_session_factory, "t_da2", "teacher")
    create_user(db_session_factory, "s_da2", "student")
    t_da, _ = login(client, "t_da2")
    s_da, _ = login(client, "s_da2")
    c2id = create_course_db(db_session_factory, teacher_username='t_da2', title='C2', status='published', visibility='public')
    client.post(f'/api/v1/courses/{c2id}/enroll', headers=auth_header(s_da))
    a2 = client.post('/api/v1/assignments', headers=auth_header(t_da), json={'course_id':c2id,'title':'A2','status':'draft'})
    q2 = client.post(f'/api/v1/assignments/{a2.json()["id"]}/questions', headers=auth_header(t_da), json={
        'title':'Q2','function_name':'f','hidden_tests':'def test(): pass',
    })
    grant_course_students(db_session_factory, c2id, ['s_da2'])
    r = client.post(f'/api/v1/judge/questions/{q2.json()["id"]}/sample-run',
                    headers=auth_header(s_da), json={'question_id':q2.json()['id'],'code':'pass'})
    assert r.status_code == 403, f"draft assignment: {r.status_code}"

    # draft course
    create_user(db_session_factory, "t_dc2", "teacher")
    create_user(db_session_factory, "s_dc2", "student")
    t_dc, _ = login(client, "t_dc2")
    s_dc, _ = login(client, "s_dc2")
    c3 = client.post('/api/v1/courses', headers=auth_header(t_dc), json={'title':'C3','status':'draft'})
    c3id = c3.json()['id']
    a3id = create_assignment_db(db_session_factory, course_id=c3id, teacher_username='t_dc2', title='A3', status='draft')
    q3 = client.post(f'/api/v1/assignments/{a3id}/questions', headers=auth_header(t_dc), json={
        'title':'Q3','function_name':'f','hidden_tests':'def test(): pass', 'grading_mode': 'legacy',
    })
    with db_session_factory() as db:
        dc_student = db.query(models.User).filter(models.User.username == "s_dc2").first()
        dc_student_id = dc_student.id
    grant_course_students(db_session_factory, c3id, ['s_dc2'])
    # 草稿课程无法 enroll，使用 whitelist_only 作为有效 audience
    r = client.patch(f'/api/v1/assignments/{a3id}', headers=auth_header(t_dc), json={
        "audience_mode": "whitelist_only",
        "whitelist_student_ids": [dc_student_id],
    })
    assert r.status_code == 200, r.text
    pub3 = client.post(f'/api/v1/assignments/{a3id}/publish', headers=auth_header(t_dc))
    assert pub3.status_code == 200, pub3.text
    r = client.post(f'/api/v1/judge/questions/{q3.json()["id"]}/sample-run',
                    headers=auth_header(s_dc), json={'question_id':q3.json()['id'],'code':'pass'})
    assert r.status_code in (403, 404), f"draft course: {r.status_code}"


# ═══════════════════════════════════════════════════════════════
# 3. Docker argv 精确断言
# ═══════════════════════════════════════════════════════════════

def test_docker_sandbox_argv_exact_params():
    settings = MagicMock()
    settings.judge_cpu_limit = 2.0
    settings.judge_image = 'dai-judge'

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
        _run_docker_pytest(Path('/tmp/t'), settings, timeout_seconds=5, memory_limit_mb=128,
                           test_filename="test_sample.py")

    argv = mock_run.call_args[0][0]
    # --cpus after global value
    cpu_idx = argv.index('--cpus')
    assert argv[cpu_idx + 1] == '2.0', f"--cpus value: {argv[cpu_idx + 1]}"
    # --memory after question value
    mem_idx = argv.index('--memory')
    assert argv[mem_idx + 1] == '128m', f"--memory value: {argv[mem_idx + 1]}"
    # timeout kwarg
    assert mock_run.call_args[1]['timeout'] == 5
    # test filename
    assert argv[-2:] == ['-p', 'no:cacheprovider'] or 'test_sample.py' in argv
    assert 'test_sample.py' in ' '.join(argv)

    assert '--network' in argv and 'none' in argv
    assert '--cap-drop' in argv and 'ALL' in argv
    assert '--security-opt' in argv
    assert '--read-only' in argv
    tmpfs_idx = argv.index('--tmpfs')
    assert 'size=' in argv[tmpfs_idx + 1]
    assert '--user' in argv and '1000:1000' in argv


# ═══════════════════════════════════════════════════════════════
# 4. Timeout cleanup + 5. Hidden sentinel
# ═══════════════════════════════════════════════════════════════

def test_docker_timeout_cleans_up():
    settings = MagicMock()
    settings.judge_cpu_limit = 1.0
    settings.judge_image = 'dai-judge'
    rm_calls = []

    def fake_run(args, **kwargs):
        if args[0] == 'docker' and args[1] == 'rm':
            rm_calls.append(args)
            return MagicMock(returncode=0)
        raise subprocess.TimeoutExpired(args, kwargs.get('timeout', 30))

    with patch('subprocess.run', side_effect=fake_run):
        _, _, rc, _ = _run_docker_pytest(Path('/tmp/t'), settings, timeout_seconds=3, memory_limit_mb=128)
    assert rc == 124
    assert len(rm_calls) >= 1


def test_sample_run_does_not_leak_hidden_sentinel(client, db_session_factory):
    """mock _run_docker_pytest，捕获写入的 test_sample.py，断言不含 HIDDEN_SENTINEL"""
    _, s_tok, _, _, qid = _setup_course(client, db_session_factory)

    captured_test_content = []

    def mock_sandbox(workdir, *args, **kwargs):
        test_file = workdir / "test_sample.py"
        if test_file.exists():
            captured_test_content.append(test_file.read_text())
        return ('1 passed', '', 0, 50)

    with patch('app.api.judge._run_docker_pytest', side_effect=mock_sandbox):
        client.post(f'/api/v1/judge/questions/{qid}/sample-run',
                    headers=auth_header(s_tok),
                    json={'question_id': qid, 'code': 'def add(a,b): return a+b'})

    assert len(captured_test_content) > 0, "未捕获测试文件内容"
    content = captured_test_content[0]
    assert 'HIDDEN_SENTINEL_XYZ' not in content, f"hidden sentinel leaked: {content}"
    assert 'def add' in content


# ═══════════════════════════════════════════════════════════════
# 6. Docker fail → system_error / JUDGE_UNAVAILABLE
# ═══════════════════════════════════════════════════════════════

def test_formal_docker_fail_system_error():
    settings = MagicMock()
    settings.judge_use_docker = True
    settings.judge_timeout_seconds = 30
    settings.judge_memory_limit_mb = 256
    settings.judge_image = 'dai-judge'
    db = MagicMock()
    q = MagicMock()
    q.time_limit_ms = 5000
    q.memory_limit_mb = 128
    q.hidden_tests = 'def test(): pass'
    q.grading_mode = 'legacy'
    q.test_groups = []
    sub = MagicMock()
    sub.id = 1
    sub.code = 'pass'
    sub.status = 'queued'
    sub.grading_status = 'queued'
    sub.question_id = 1
    # 历史题目：没有评分版本基线（A08 之后必须显式给出，否则 MagicMock 会被当成绑定版本）
    sub.grading_version_id = None
    db.get.side_effect = lambda m, i: q if m == models.JudgeQuestion else sub
    redis = MagicMock()

    with patch('subprocess.run', side_effect=FileNotFoundError('no docker')):
        with patch('app.services.judge_queue.claim_job', return_value=True):
            # 历史题目没有任何评分版本：显式让版本解析返回 None（MagicMock db 会返回真值 mock）
            with patch('app.services.grading_version_service.active_version_for',
                       return_value=None):
                process_submission(db, redis, settings, 1)
    # 结果写入是受认领栅栏保护的条件 UPDATE，全 mock 的 db 不反映内存副作用，
    # 因此断言发出的 SQL 值（真实落库行为由 test_judge_claim_fence.py 覆盖）。
    from conftest import executed_update_values
    from app.models import Submission as _Submission
    values = executed_update_values(db, _Submission)
    assert any(v.get('status') == 'review_required' for v in values), values
    assert any(v.get('score') is None for v in values), values
    db.commit.assert_called()
    # 缺少可信基线在旧评分器/Docker/Redis 之前即安全转人工。
    redis.setex.assert_not_called()


def test_sample_run_docker_unavailable(client, db_session_factory):
    _, s_tok, _, _, qid = _setup_course(client, db_session_factory)
    with patch('app.api.judge._run_docker_pytest', side_effect=FileNotFoundError('no docker')):
        r = client.post(f'/api/v1/judge/questions/{qid}/sample-run',
                        headers=auth_header(s_tok),
                        json={'question_id': qid, 'code': 'pass'})
    assert r.status_code == 503, r.text
    assert r.json()['detail']['code'] == 'JUDGE_UNAVAILABLE'


def test_get_timeout_values():
    q = MagicMock()
    s = MagicMock()
    s.judge_timeout_seconds = 30
    q.time_limit_ms = 1001; assert _get_timeout(q, s) == 2
    q.time_limit_ms = 60000; assert _get_timeout(q, s) == 30
    q.time_limit_ms = 0; assert _get_timeout(q, s) == 1
    q.time_limit_ms = 100; assert _get_timeout(q, s) == 1


# ═══════════════════════════════════════════════════════════════
# P0 回归测试
# ═══════════════════════════════════════════════════════════════

def test_p0_1_worker_passes_queue_sequence_to_blocking_pop(monkeypatch):
    """Exercise the actual worker's Redis call, including FIFO direction."""
    from test_judge_worker_fifo import _message, _redis_loop

    loop, client, popped = _redis_loop(monkeypatch)
    client.rpush("judge:queue", _message(123))
    loop.run()
    assert loop.processed == [123]
    assert popped[0][:2] == ("blpop", ("judge:queue", "judge:exam:queue"))


def test_p0_2_exam_functions_defined_at_module_level():
    """P0-2: process_exam_answer 和 enqueue_exam_answer 在模块导入时可访问"""
    from app.worker import judge_worker as jw
    assert hasattr(jw, "process_exam_answer"), "process_exam_answer 未在模块顶层定义"
    assert hasattr(jw, "enqueue_exam_answer"), "enqueue_exam_answer 未在模块顶层定义"
    assert hasattr(jw, "EXAM_JUDGE_QUEUE"), "EXAM_JUDGE_QUEUE 未在模块顶层定义"
    assert callable(jw.process_exam_answer)
    assert callable(jw.enqueue_exam_answer)


def test_p0_5_maybe_finalize_blocks_on_running(db_session_factory):
    """P0-5: finalize_if_ready 不应在存在 running 答案时汇总"""
    from app.services.exam_grading import finalize_if_ready, FinalizeOutcome
    from datetime import datetime, timezone
    from app.models import Course, Exam, ExamAnswer, ExamQuestion, ExamSubmission, User

    with db_session_factory() as db:
        teacher = User(username="p05_t", real_name="P05T", role="teacher", status="active",
                       password_hash="x")
        student = User(username="p05_s", real_name="P05S", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="P05C", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="P05E", status="published", duration_minutes=60)
        db.add(exam); db.flush()
        q1 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q1",
                          correct_answer={}, points=10, hidden_tests="assert True")
        q2 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q2",
                          correct_answer={}, points=20, hidden_tests="assert True")
        db.add_all([q1, q2]); db.flush()
        sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
        db.add(sub); db.flush()
        ans1 = ExamAnswer(submission_id=sub.id, question_id=q1.id,
                          code_answer="def a(): pass", grading_status="completed", score=10.0)
        ans2 = ExamAnswer(submission_id=sub.id, question_id=q2.id,
                          code_answer="def b(): pass", grading_status="running")
        db.add_all([ans1, ans2]); db.commit()
        sub_id = sub.id

    # 测试1: 存在 running 答案 → waiting，不汇总
    with db_session_factory() as db:
        r = finalize_if_ready(sub_id, db)
        assert r.outcome == FinalizeOutcome.WAITING, f"running 答案应 waiting: {r}"
        sub_check = db.get(ExamSubmission, sub_id)
        assert sub_check.status == "grading", "存在未完成答案时不应汇总"

    # 测试2: 全部完成 → 应汇总
    with db_session_factory() as db:
        ans2 = db.get(ExamAnswer, ans2.id)
        ans2.grading_status = "completed"
        ans2.score = 20.0
        db.commit()

    with db_session_factory() as db:
        r2 = finalize_if_ready(sub_id, db)
        assert r2.outcome == FinalizeOutcome.GRADED
        sub_check = db.get(ExamSubmission, sub_id)
        assert sub_check.status == "graded"
        assert sub_check.score == 30.0


# ═══════════════════════════════════════════════════════════════
# P0-1: DoD 宿主机路径与容器路径分离
# ═══════════════════════════════════════════════════════════════

def test_p0_1_docker_v_uses_host_workdir_when_configured():
    """验证 judge_host_work_dir 设置后，Docker -v 参数使用宿主机路径"""
    from app.config import Settings

    settings = Settings(
        secret_key="test",
        judge_work_dir="/container/judge-work",
        judge_host_work_dir="/host/real/judge-work",
        judge_image="dai-judge-python:latest",
    )

    # 模拟 workdir 在容器内路径 /container/judge-work/dai-judge-abc123
    container_workdir = Path("/container/judge-work/dai-judge-abc123")
    host_workdir = Path("/host/real/judge-work/dai-judge-abc123")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="1 passed", stderr="", returncode=0)
        _run_docker_pytest(
            container_workdir, settings, timeout_seconds=5, memory_limit_mb=256,
            host_workdir=host_workdir,
        )

    # 验证 -v 参数使用了宿主机路径而非容器内路径
    call_args = mock_run.call_args[0][0]
    v_index = call_args.index("-v") if "-v" in call_args else -1
    assert v_index >= 0, "Docker 命令缺少 -v 参数"
    mount_arg = call_args[v_index + 1]
    assert str(host_workdir) in mount_arg, (
        f"Docker -v 应使用宿主机路径 {host_workdir}，实际为 {mount_arg}"
    )
    assert "/container/" not in mount_arg, (
        f"Docker -v 不应包含容器内路径，实际为 {mount_arg}"
    )


def test_p0_1_docker_v_falls_back_to_workdir_when_host_not_set():
    """未设置 judge_host_work_dir 时，Docker -v 回退到容器内路径（兼容旧行为）"""
    from app.config import Settings

    settings = Settings(
        secret_key="test",
        judge_work_dir="/judge-work",
        judge_host_work_dir="",  # 未设置
        judge_image="dai-judge-python:latest",
    )

    workdir = Path("/judge-work/dai-judge-xyz789")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="1 passed", stderr="", returncode=0)
        _run_docker_pytest(
            workdir, settings, timeout_seconds=5, memory_limit_mb=256,
            host_workdir=None,  # 未指定 host_workdir
        )

    call_args = mock_run.call_args[0][0]
    v_index = call_args.index("-v") if "-v" in call_args else -1
    assert v_index >= 0, "Docker 命令缺少 -v 参数"
    mount_arg = call_args[v_index + 1]
    assert str(workdir) in mount_arg, (
        f"未设置 host_workdir 时应回退到 workdir，预期 {workdir}，实际 {mount_arg}"
    )
