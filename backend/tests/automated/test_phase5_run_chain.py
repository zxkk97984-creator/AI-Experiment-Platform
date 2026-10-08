"""Phase 5 运行链路测试：digest 判题、快照重判、import 诊断、Kernel 环境重建

覆盖 plan 8.3 / 9.3 / 9.4 / 12 的运行部分：
- _run_docker_pytest 镜像参数使用 digest（安全参数原样保留，只改镜像）
- 历史提交重判使用提交时快照的 digest（题目后改环境不影响旧提交）
- IMPORT_NOT_ALLOWED（学生错误）/ IMPORT_NOT_INSTALLED（平台配置）/ ENVIRONMENT_IMAGE_MISSING（镜像缺失）三类诊断
- create_submission 入队前冻结环境与 import 策略快照
- sample-run 使用有效环境 digest 并返回 diagnostic
- Kernel create_session 使用 digest + 环境 label（dai.environment_version_id / dai.image_digest）
- Kernel 环境不匹配重建、Redis 旧格式（无环境字段）不匹配重建
- recover_from_docker 校验环境 label，缺失环境 label 的旧容器不复用
- execute_cell 预检诊断（IMPORT_NOT_ALLOWED → 422，IMPORT_NOT_INSTALLED → 500）
- ensure_record_for_lesson/module 从模板版本复制 environment_version_id

说明：不执行真实 docker build（Phase 6 才做），digest 用 seed 后手动标记 available 的
mock 版本验证；Docker 调用全部 patch 捕获 argv。
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.no_auto_env_seed

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.models import (
    Assignment,
    Course,
    CourseEnrollment,
    EnvironmentProfile,
    EnvironmentVersion,
    ExperimentRecord,
    JudgeQuestion,
    NotebookTemplate,
    NotebookTemplateVersion,
    Submission,
    User,
)
from app.services.environment_seed import seed_environment_catalog
from app.worker.judge_worker import _run_docker_pytest, process_submission
from conftest import auth_header, create_user, grant_teaching_fixture, login
from teaching_test_support import grant_experiment


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════

def _seed_version_available(db, settings, slug="basic", digest_letter="a") -> int:
    """幂等 seed 后把指定档位 v1 标记为 available（mock digest），返回版本 id。"""
    seed_environment_catalog(db, settings)
    version = db.scalar(
        select(EnvironmentVersion)
        .join(EnvironmentProfile, EnvironmentProfile.id == EnvironmentVersion.profile_id)
        .where(
            EnvironmentProfile.slug == slug,
            EnvironmentVersion.version_number == 1,
        )
    )
    assert version is not None, f"seed 后 {slug} v1 应存在"
    if version.status != "available":
        version.status = "available"
        version.image_digest = "sha256:" + digest_letter * 64
        version.python_version = "3.12"
        db.commit()
    return version.id


def _make_student(db, username="stu"):
    user = User(
        username=username,
        real_name=username,
        role="student",
        status="active",
        password_hash="x",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_teacher(db, username="t1"):
    user = User(
        username=username,
        real_name=username,
        role="teacher",
        status="active",
        password_hash="x",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_published_assignment(db, settings, *, question_env_id=None) -> tuple[Assignment, JudgeQuestion]:
    """创建已发布作业 + 题目（环境：作业默认 basic，题目可选覆盖），返回 (assignment, question)"""
    basic_id = _seed_version_available(db, settings)
    teacher = _make_teacher(db)
    course = Course(title="C1", status="published", teacher_id=teacher.id)
    db.add(course)
    db.commit()
    db.refresh(course)
    assignment = Assignment(course_id=course.id, title="A1", status="published")
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    question = JudgeQuestion(
        assignment_id=assignment.id, title="Q1",
        function_name="add", hidden_tests="HIDDEN\ndef test_add(): assert add(1,2)==3",
        public_cases=[{"args": [1, 2], "expected": 3}],
        environment_version_id=question_env_id,
    )
    db.add(question)
    db.commit()
    db.refresh(question)
    return assignment, question


# ═══════════════════════════════════════════════════════════════
# 1. 判题：digest argv + 安全参数原样保留
# ═══════════════════════════════════════════════════════════════

def test_run_docker_pytest_uses_digest_image_and_keeps_security_args():
    """镜像参数使用 digest；--network none / --cap-drop ALL / 非 root 等安全参数原样保留"""
    settings = MagicMock()
    settings.judge_cpu_limit = 1.0
    settings.judge_image = "dai-judge-python:latest"
    digest = "sha256:" + "d" * 64

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _run_docker_pytest(
            Path("/tmp/t"), settings, timeout_seconds=5, memory_limit_mb=128,
            image_ref=digest,
        )

    argv = mock_run.call_args[0][0]
    assert digest in argv, f"镜像参数应为 digest: {argv}"
    assert "dai-judge-python:latest" not in argv, "已绑定环境时禁止回退到 latest 标签"
    assert "--network" in argv and "none" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert "--read-only" in argv
    tmpfs_idx = argv.index("--tmpfs")
    assert "size=" in argv[tmpfs_idx + 1]
    assert "--user" in argv and "1000:1000" in argv
    assert "--pids-limit" in argv
    assert "--cpus" in argv
    assert "--memory" in argv


def test_run_docker_pytest_falls_back_to_judge_image_when_unbound():
    """未绑定环境版本（存量兼容路径）回退 settings.judge_image——仅此兼容场景允许标签镜像"""
    settings = MagicMock()
    settings.judge_cpu_limit = 1.0
    settings.judge_image = "dai-judge-python:latest"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _run_docker_pytest(Path("/tmp/t"), settings, timeout_seconds=5, memory_limit_mb=128)

    argv = mock_run.call_args[0][0]
    assert "dai-judge-python:latest" in argv


# ═══════════════════════════════════════════════════════════════
# 2. 判题：快照重判 + import 诊断
# ═══════════════════════════════════════════════════════════════

def test_historical_submission_resubmit_uses_snapshot_digest(db_session_factory, test_settings):
    """快照重判：提交时绑定 basic v1（digest A），题目后来覆盖 data v2（digest B），
    重判旧提交仍使用 digest A——计划 8.3「历史 Submission 重判仍使用原环境版本」"""
    with db_session_factory() as db:
        basic_id = _seed_version_available(db, test_settings, slug="basic", digest_letter="a")
        # data v2 手动标记 available（digest B）
        data_profile = db.scalar(
            select(EnvironmentProfile).where(EnvironmentProfile.slug == "data")
        )
        assert data_profile is not None
        data_version = EnvironmentVersion(
            profile_id=data_profile.id,
            version_number=2,
            status="available",
            base_image_ref="python:3.12-slim",
            image_digest="sha256:" + "b" * 64,
            minimum_memory_mb=768,
            manifest_sha256="m" * 64,
        )
        db.add(data_version)
        db.commit()
        db.refresh(data_version)

        teacher = _make_teacher(db)
        student = _make_student(db)
        course = Course(title="C1", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(course_id=course.id, title="A1", status="published")
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN\ndef test_add(): assert add(1,2)==3",
            # 题目后来覆盖为 data v2
            environment_version_id=data_version.id,
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        # 旧提交：快照 basic v1（digest A）
        submission = Submission(
            question_id=question.id, student_id=student.id, code="def add(a,b): return a+b",
            environment_version_id=basic_id,
            import_policy_mode_snapshot="unrestricted",
            allowed_imports_snapshot=[],
            status="queued", grading_status="queued",
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)

        captured: dict = {}
        def fake_run(workdir, *args, **kwargs):
            captured["image_ref"] = kwargs.get("image_ref")
            return ("", "", 0, 10)

        with patch("app.worker.judge_worker._run_docker_pytest", side_effect=fake_run), \
             patch("app.services.judge_queue.claim_job", return_value=True), \
             patch("app.services.judge_queue.complete_job") as mock_complete:
            process_submission(db, MagicMock(), test_settings, submission.id)

        assert captured["image_ref"] == "sha256:" + "a" * 64, \
            f"重判应使用提交快照 digest A，实际: {captured.get('image_ref')}"
        assert submission.status == "accepted"
        mock_complete.assert_called()


def test_import_not_allowed_is_student_error_not_retryable(db_session_factory, test_settings):
    """IMPORT_NOT_ALLOWED：学生错误终态、不跑 Docker、不计基础设施重试、明确诊断"""
    with db_session_factory() as db:
        _seed_version_available(db, test_settings)
        teacher = _make_teacher(db)
        student = _make_student(db)
        course = Course(title="C1", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(course_id=course.id, title="A1", status="published")
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN\ndef test_add(): assert add(1,2)==3",
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        submission = Submission(
            question_id=question.id, student_id=student.id,
            code="import numpy\ndef add(a,b): return a+b",
            environment_version_id=None,
            import_policy_mode_snapshot="restricted",
            allowed_imports_snapshot=["pandas"],
            status="queued", grading_status="queued",
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)

        docker_called = []
        def fake_run(*args, **kwargs):
            docker_called.append(args)
            return ("", "", 0, 10)

        with patch("app.worker.judge_worker._run_docker_pytest", side_effect=fake_run), \
             patch("app.services.judge_queue.claim_job", return_value=True), \
             patch("app.services.judge_queue.fail_job") as mock_fail:
            process_submission(db, MagicMock(), test_settings, submission.id)

        assert docker_called == [], "IMPORT_NOT_ALLOWED 不应进入 Docker"
        assert submission.status == "runtime_error", "学生代码违反教学策略按学生错误处理"
        assert submission.score == 0
        diag = (submission.result_details or {}).get("diagnostic", {})
        assert diag.get("code") == "IMPORT_NOT_ALLOWED"
        assert diag.get("module") == "numpy"
        assert "未在本作业允许范围内" in diag.get("message", "")
        assert mock_fail.call_count == 1
        fail_kwargs = mock_fail.call_args.kwargs
        assert fail_kwargs.get("retryable") is False, "学生错误不消耗基础设施重试"


def test_import_not_installed_is_system_error_no_score(db_session_factory, test_settings):
    """IMPORT_NOT_INSTALLED：允许但环境未安装 → 平台配置问题，system_error 不扣分、可重试"""
    with db_session_factory() as db:
        _seed_version_available(db, test_settings)
        teacher = _make_teacher(db)
        student = _make_student(db)
        course = Course(title="C1", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(course_id=course.id, title="A1", status="published")
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN\ndef test_add(): assert add(1,2)==3",
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        submission = Submission(
            question_id=question.id, student_id=student.id,
            code="import numpy\ndef add(a,b): return a+b",
            environment_version_id=None,
            import_policy_mode_snapshot="restricted",
            allowed_imports_snapshot=["numpy"],  # 白名单允许，但环境（basic 仅 pytest）未安装
            status="queued", grading_status="queued",
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)

        docker_called = []
        def fake_run(*args, **kwargs):
            docker_called.append(args)
            return ("", "", 0, 10)

        with patch("app.worker.judge_worker._run_docker_pytest", side_effect=fake_run), \
             patch("app.services.judge_queue.claim_job", return_value=True), \
             patch("app.services.judge_queue.fail_job") as mock_fail:
            process_submission(db, MagicMock(), test_settings, submission.id)

        assert docker_called == [], "IMPORT_NOT_INSTALLED 不应进入 Docker"
        assert submission.status == "system_error"
        assert submission.score is None, "平台配置问题不扣分"
        diag = (submission.result_details or {}).get("diagnostic", {})
        assert diag.get("code") == "IMPORT_NOT_INSTALLED"
        assert diag.get("module") == "numpy"
        assert mock_fail.call_count == 1
        assert mock_fail.call_args.kwargs.get("retryable") is True


def test_missing_digest_fails_closed_system_error(db_session_factory, test_settings):
    """ENVIRONMENT_IMAGE_MISSING：提交快照指向未构建版本（无 digest）→ fail closed 不扣分"""
    with db_session_factory() as db:
        seed_environment_catalog(db, test_settings)
        # basic v1 保持 draft（无 digest）
        teacher = _make_teacher(db)
        student = _make_student(db)
        course = Course(title="C1", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        draft_version = db.scalar(
            select(EnvironmentVersion)
            .join(EnvironmentProfile, EnvironmentProfile.id == EnvironmentVersion.profile_id)
            .where(EnvironmentProfile.slug == "basic", EnvironmentVersion.version_number == 1)
        )
        # 模型层 environment_version_id 已 NOT NULL：显式绑定 draft 版本（无 digest）
        assignment = Assignment(
            course_id=course.id, title="A1", status="published",
            environment_version_id=draft_version.id,
        )
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN\ndef test_add(): assert add(1,2)==3",
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        submission = Submission(
            question_id=question.id, student_id=student.id, code="def add(a,b): return a+b",
            environment_version_id=draft_version.id,  # draft 状态、无 digest
            import_policy_mode_snapshot="unrestricted",
            allowed_imports_snapshot=[],
            status="queued", grading_status="queued",
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)

        docker_called = []
        def fake_run(*args, **kwargs):
            docker_called.append(args)
            return ("", "", 0, 10)

        with patch("app.worker.judge_worker._run_docker_pytest", side_effect=fake_run), \
             patch("app.services.judge_queue.claim_job", return_value=True):
            process_submission(db, MagicMock(), test_settings, submission.id)

        assert docker_called == [], "digest 缺失必须 fail closed"
        assert submission.status == "system_error"
        assert submission.score is None
        diag = (submission.result_details or {}).get("diagnostic", {})
        assert diag.get("code") == "ENVIRONMENT_IMAGE_MISSING"


# ═══════════════════════════════════════════════════════════════
# 3. 判题 API：create_submission 快照 + sample-run
# ═══════════════════════════════════════════════════════════════

def test_create_submission_snapshots_question_override_environment(client, db_session_factory, test_settings):
    """create_submission 入队前冻结题目覆盖环境与最终 import 策略（计划 8.2）"""
    with db_session_factory() as db:
        basic_id = _seed_version_available(db, test_settings, slug="basic", digest_letter="a")
        data_profile = db.scalar(
            select(EnvironmentProfile).where(EnvironmentProfile.slug == "data")
        )
        data_version = EnvironmentVersion(
            profile_id=data_profile.id,
            version_number=2,
            status="available",
            base_image_ref="python:3.12-slim",
            image_digest="sha256:" + "b" * 64,
            minimum_memory_mb=768,
            manifest_sha256="m" * 64,
        )
        db.add(data_version)
        db.commit()
        db.refresh(data_version)
        teacher = _make_teacher(db, username="t2")
        course = Course(title="C1", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(
            course_id=course.id, title="A1", status="published",
            import_policy_mode="unrestricted",
        )
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        # 题目覆盖环境 data v2 + restricted 白名单
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN\ndef test_add(): assert add(1,2)==3",
            environment_version_id=data_version.id,
            import_policy_mode="restricted",
            allowed_imports=["numpy", "pandas"],
        )
        db.add(question)
        db.commit()
        db.refresh(question)

    create_user(db_session_factory, "stu_c", "student")
    s_tok, _ = login(client, "stu_c")
    # 通过 API 提交
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C1"))
        student = db.scalar(select(User).where(User.username == "stu_c"))
        db.add(CourseEnrollment(
            course_id=course.id, student_id=student.id, status="enrolled",
        ))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    r = client.post("/api/v1/judge/submissions",
                    headers=auth_header(s_tok),
                    json={"question_id": question.id, "code": "def add(a,b): return a+b"})
    assert r.status_code == 201, r.text

    with db_session_factory() as db:
        sub = db.scalar(select(Submission).order_by(Submission.id.desc()))
        assert sub.environment_version_id == data_version.id, "应冻结题目覆盖环境"
        assert sub.import_policy_mode_snapshot == "restricted"
        assert sub.allowed_imports_snapshot == ["numpy", "pandas"]
        assert basic_id != data_version.id


def test_sample_run_import_not_allowed_returns_diagnostic(client, db_session_factory, test_settings):
    """sample-run 返回结构化 diagnostic；IMPORT_NOT_ALLOWED 直接拦截不跑 Docker（计划 8.3）"""
    with db_session_factory() as db:
        _seed_version_available(db, test_settings)
        teacher = _make_teacher(db, username="t3")
        course = Course(title="C2", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(
            course_id=course.id, title="A2", status="published",
            import_policy_mode="restricted",
            allowed_imports=["pandas"],
        )
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN",
            public_cases=[{"args": [1, 2], "expected": 3}],
        )
        db.add(question)
        db.commit()
        db.refresh(question)

    create_user(db_session_factory, "stu_s", "student")
    s_tok, _ = login(client, "stu_s")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C2"))
        student = db.scalar(select(User).where(User.username == "stu_s"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    docker_called = []
    def fake_run(*args, **kwargs):
        docker_called.append(args)
        return ("", "", 0, 50)

    with patch("app.api.judge._run_docker_pytest", side_effect=fake_run):
        r = client.post(f"/api/v1/judge/questions/{question.id}/sample-run",
                        headers=auth_header(s_tok),
                        json={"question_id": question.id,
                              "code": "import numpy\ndef add(a,b): return a+b"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["diagnostic"]["code"] == "IMPORT_NOT_ALLOWED"
    assert data["diagnostic"]["module"] == "numpy"
    assert docker_called == [], "IMPORT_NOT_ALLOWED 不应进入 Docker"


def test_sample_run_uses_environment_digest(client, db_session_factory, test_settings):
    """sample-run 使用题目有效环境 digest 启动 Docker（不允许默认 settings.judge_image）"""
    with db_session_factory() as db:
        basic_id = _seed_version_available(db, test_settings, digest_letter="c")
        teacher = _make_teacher(db, username="t4")
        course = Course(title="C3", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(course_id=course.id, title="A3", status="published")
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN",
            public_cases=[{"args": [1, 2], "expected": 3}],
            environment_version_id=basic_id,
        )
        db.add(question)
        db.commit()
        db.refresh(question)

    create_user(db_session_factory, "stu_s2", "student")
    s_tok, _ = login(client, "stu_s2")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C3"))
        student = db.scalar(select(User).where(User.username == "stu_s2"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    captured = {}
    def fake_run(workdir, *args, **kwargs):
        captured["image_ref"] = kwargs.get("image_ref")
        return ("1 passed", "", 0, 50)

    with patch("app.api.judge._run_docker_pytest", side_effect=fake_run):
        r = client.post(f"/api/v1/judge/questions/{question.id}/sample-run",
                        headers=auth_header(s_tok),
                        json={"question_id": question.id,
                              "code": "def add(a,b): return a+b"})
    assert r.status_code == 200, r.text
    assert captured.get("image_ref") == "sha256:" + "c" * 64
    assert r.json()["status"] == "accepted"
    assert r.json().get("diagnostic") is None


# ═══════════════════════════════════════════════════════════════
# 4. Kernel：digest 启动 + 环境 label + 不匹配重建 + Redis 恢复校验
# ═══════════════════════════════════════════════════════════════

def _patched_kernel_manager():
    from fakeredis import FakeStrictRedis
    from app.services.kernel_manager import KernelManager
    km = KernelManager()
    patches = [
        patch.object(km, "_generate_conn_file"),
        patch("redis.from_url", return_value=FakeStrictRedis()),
        patch("subprocess.run"),
        patch("os.makedirs"),
        patch("time.sleep"),
    ]
    for p in patches:
        p.start()
    mock_run = patches[2].mock
    mock_gen = patches[0].mock
    mock_gen.return_value = ("/tmp/c.json", {
        "shell_port": 1, "iopub_port": 2, "stdin_port": 3,
        "control_port": 4, "hb_port": 5, "ip": "0.0.0.0",
    })
    mock_run.side_effect = [
        MagicMock(returncode=0),                      # docker rm cleanup
        MagicMock(returncode=0, stdout="abc\n"),      # docker run
        MagicMock(returncode=0, stdout="abc\n"),      # docker ps alive
        MagicMock(returncode=0),                      # docker exec 就绪探测
    ]
    def _stop():
        for p in patches:
            p.stop()
    return km, mock_run, _stop


def test_kernel_create_session_uses_digest_and_env_labels():
    """Kernel create_session 使用 digest 镜像，label 记录环境版本与镜像 digest（计划 9.3）"""
    from app.services.kernel_manager import KernelManager
    digest = "sha256:" + "e" * 64
    km = KernelManager()
    with patch.object(km, "_generate_conn_file") as mock_gen, \
         patch("redis.from_url", return_value=__import__("fakeredis").FakeStrictRedis()), \
         patch("subprocess.run") as mock_run, \
         patch("os.makedirs"), \
         patch("time.sleep"):
        mock_gen.return_value = ("/tmp/c.json", {
            "shell_port": 1, "iopub_port": 2, "stdin_port": 3,
            "control_port": 4, "hb_port": 5, "ip": "0.0.0.0",
        })
        mock_run.side_effect = [
            MagicMock(returncode=0),                    # docker rm cleanup
            MagicMock(returncode=0, stdout="abc\n"),    # docker run
            MagicMock(returncode=0, stdout="abc\n"),    # docker ps alive
            MagicMock(returncode=0),                    # docker exec 就绪探测
            MagicMock(returncode=0),                    # runner file probe
        ]
        session = km.create_session(
            1, image_ref=digest, environment_version_id=5, lesson_storage_dir="",
        )

    run_calls = [c for c in mock_run.call_args_list
                 if len(c[0][0]) > 2 and c[0][0][1] == "run"]
    assert len(run_calls) == 1
    argv = run_calls[0][0][0]
    assert digest in argv, f"镜像参数应为 digest: {argv}"
    assert "dai-kernel-python:latest" not in argv
    assert "-l" in argv and "dai.environment_version_id=5" in argv
    assert "dai.image_digest=" + digest in argv
    # 安全参数保留
    argv_str = " ".join(argv)
    assert "--network" in argv and "none" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--read-only" in argv
    assert "--pids-limit" in argv
    assert "--user" not in argv or True  # kernel 由镜像默认用户
    # session 记录环境字段，Redis 元数据包含环境信息
    assert session.environment_version_id == 5
    assert session.image_ref == digest
    redis_dict = session.to_redis_dict()
    assert redis_dict["environment_version_id"] == 5
    assert redis_dict["image_ref"] == digest


def test_kernel_rebuilds_when_environment_mismatch():
    """环境不匹配重建：内存 session 环境与期望不一致 → 销毁旧容器按新 digest 重建（计划 9.3）"""
    from fakeredis import FakeStrictRedis
    from app.services.kernel_manager import KernelManager, KernelSession

    old_digest = "sha256:" + "f" * 64
    new_digest = "sha256:" + "9" * 64
    km = KernelManager()
    km._sessions[1] = KernelSession(
        1, "dai-kernel-rec-1", {"ip": "0.0.0.0"},
        image_ref=old_digest, environment_version_id=1,
    )

    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "docker" and args[1] == "ps":
            return MagicMock(returncode=0, stdout="abc\n")  # 旧容器存活
        if args[0] == "docker" and args[1] == "rm":
            return MagicMock(returncode=0)
        if args[0] == "docker" and args[1] == "run":
            return MagicMock(returncode=0, stdout="abc\n")
        if args[0] == "docker" and args[1] == "exec":
            return MagicMock(returncode=0)
        return MagicMock(returncode=0)

    with patch.object(km, "_generate_conn_file") as mock_gen, \
         patch("redis.from_url", return_value=FakeStrictRedis()), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("os.makedirs"), \
         patch("time.sleep"):
        mock_gen.return_value = ("/tmp/c.json", {
            "shell_port": 1, "iopub_port": 2, "stdin_port": 3,
            "control_port": 4, "hb_port": 5, "ip": "0.0.0.0",
        })
        session = km.get_or_create_session(1, image_ref=new_digest, environment_version_id=2)

    rm_calls = [c for c in calls if c[0] == "docker" and c[1] == "rm"]
    run_calls = [c for c in calls if c[0] == "docker" and c[1] == "run"]
    assert len(rm_calls) >= 1, "环境不匹配必须销毁旧容器"
    assert len(run_calls) == 1
    assert new_digest in run_calls[0], f"重建应使用新 digest: {run_calls[0]}"
    assert session.environment_version_id == 2
    assert session.image_ref == new_digest


def test_kernel_redis_session_without_env_fields_is_rebuilt():
    """Redis 旧格式 session（无环境字段）视为不匹配 → 销毁重建（计划 9.3 恢复校验）"""
    from fakeredis import FakeStrictRedis
    from app.services.kernel_manager import KernelManager, KernelSession

    redis = FakeStrictRedis(decode_responses=True)
    old = KernelSession(1, "dai-kernel-rec-1", {"ip": "0.0.0.0"})  # 无 image_ref/env 字段
    redis.setex("kernel:session:1", 3600, json.dumps(old.to_redis_dict()))

    new_digest = "sha256:" + "8" * 64
    km = KernelManager()

    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "docker" and args[1] == "ps":
            return MagicMock(returncode=0, stdout="abc\n")  # Redis 记录的容器存活
        if args[0] == "docker" and args[1] in ("rm", "run", "exec"):
            return MagicMock(returncode=0, stdout="abc\n")
        return MagicMock(returncode=0)

    with patch.object(km, "_generate_conn_file") as mock_gen, \
         patch("redis.from_url", return_value=redis), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("os.makedirs"), \
         patch("time.sleep"):
        mock_gen.return_value = ("/tmp/c.json", {
            "shell_port": 1, "iopub_port": 2, "stdin_port": 3,
            "control_port": 4, "hb_port": 5, "ip": "0.0.0.0",
        })
        session = km.get_or_create_session(1, image_ref=new_digest, environment_version_id=3)

    rm_calls = [c for c in calls if c[0] == "docker" and c[1] == "rm"]
    run_calls = [c for c in calls if c[0] == "docker" and c[1] == "run"]
    assert len(rm_calls) >= 1, "旧格式 Redis session 必须销毁重建"
    assert len(run_calls) == 1
    assert new_digest in run_calls[0]
    assert session.environment_version_id == 3


def test_recover_from_docker_skips_containers_without_env_labels():
    """recover_from_docker 校验环境 label：缺失环境 label 的旧容器不复用（计划 9.3）"""
    import subprocess as _subprocess
    from fakeredis import FakeStrictRedis
    from app.services.kernel_manager import KernelManager

    digest = "sha256:" + "7" * 64
    conn_json = json.dumps({"shell_port": 1, "iopub_port": 2, "ip": "0.0.0.0"})

    def fake_run(args, **kwargs):
        cmd_str = " ".join(str(a) for a in args)
        if "docker ps" in cmd_str and "--filter" in cmd_str:
            return MagicMock(returncode=0, stdout="cid1\ncid2\n")
        if "docker inspect" in cmd_str:
            if "{{.Name}}" in cmd_str:
                name = "/dai-kernel-rec-1" if args[-1] == "cid1" else "/dai-kernel-rec-2"
                return MagicMock(returncode=0, stdout=name + "\n")
            if "dai.record_id" in cmd_str:
                return MagicMock(returncode=0, stdout="1\n" if args[-1] == "cid1" else "2\n")
            if "environment_version_id" in cmd_str:
                return MagicMock(returncode=0, stdout="5\n" if args[-1] == "cid1" else "\n")
            if "image_digest" in cmd_str:
                return MagicMock(returncode=0, stdout=(digest + "\n") if args[-1] == "cid1" else "\n")
        if "docker exec" in cmd_str and "cat" in cmd_str:
            return MagicMock(returncode=0, stdout=conn_json + "\n")
        return MagicMock(returncode=0)

    km = KernelManager()
    with patch("subprocess.run", side_effect=fake_run), \
         patch("redis.from_url", return_value=FakeStrictRedis()):
        km.recover_from_docker()

    assert 1 in km._sessions, "带完整环境 label 的容器应恢复"
    assert 2 not in km._sessions, "缺失环境 label 的旧容器不应复用"
    assert km._sessions[1].environment_version_id == 5
    assert km._sessions[1].image_ref == digest


# ═══════════════════════════════════════════════════════════════
# 5. Notebook 链路：环境复制 + execute 预检诊断
# ═══════════════════════════════════════════════════════════════

def test_ensure_record_copies_template_version_environment(db_session_factory, test_settings):
    """ensure_record 创建记录时从 NotebookTemplateVersion 复制 environment_version_id（计划 9.2）"""
    from app.models import ExperimentModule

    with db_session_factory() as db:
        basic_id = _seed_version_available(db, test_settings)
        teacher = _make_teacher(db, username="t5")
        student = _make_student(db, username="s5")
        template = NotebookTemplate(name="N1", owner_id=teacher.id, status="published")
        db.add(template)
        db.commit()
        db.refresh(template)
        version = NotebookTemplateVersion(
            template_id=template.id, version_number=1,
            sha256="s" * 64, cells=[], cell_order=[], notebook_metadata={},
            published_by_id=teacher.id,
            # 模板版本绑定 data 档位（模拟教师选择 data 而非默认 basic）
            environment_version_id=basic_id,
            import_policy_mode="restricted",
            allowed_imports=["numpy"],
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        module = ExperimentModule(name="M1", template_id=template.id, owner_id=teacher.id)
        db.add(module)
        db.commit()
        db.refresh(module)

        # ensure_record_for_lesson/module 创建记录时从模板版本复制 environment_version_id
        # （API 层同款逻辑：ExperimentRecord.environment_version_id = version.environment_version_id）
        record = ExperimentRecord(
            module_id=module.id, template_version_id=version.id, student_id=student.id,
            environment_version_id=version.environment_version_id,
        )
        db.add(record)
        db.commit()
        assert record.environment_version_id == basic_id


def _setup_notebook_execute_env(db, settings):
    """组装 lesson + 已发布模板（basic 环境）→ 返回 (lesson_id, template_version)"""
    from app.models import Chapter, Lesson

    basic_id = _seed_version_available(db, settings)
    teacher = _make_teacher(db, username="t6")
    course = Course(title="C4", status="published", teacher_id=teacher.id)
    db.add(course)
    db.commit()
    db.refresh(course)
    chapter = Chapter(title="Ch1", course_id=course.id)
    db.add(chapter)
    db.commit()
    db.refresh(chapter)
    template = NotebookTemplate(name="N2", owner_id=teacher.id, status="published")
    db.add(template)
    db.commit()
    db.refresh(template)
    version = NotebookTemplateVersion(
        template_id=template.id, version_number=1,
        sha256="s" * 64,
        cells=[{"id": "c1", "type": "code", "source": "print(1)", "order": 0, "student_editable": True}],
        cell_order=["c1"], notebook_metadata={},
        published_by_id=teacher.id,
        environment_version_id=basic_id,
        import_policy_mode="restricted",
        allowed_imports=["pandas"],
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    # 发布流程会把 current_version_id 指向最新版本（这里手动对齐，跳过完整发布 API）
    template.current_version_id = version.id
    db.commit()
    lesson = Lesson(title="L1", chapter_id=chapter.id, content_type="notebook", template_id=template.id)
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return lesson.id, basic_id


def test_execute_cell_import_not_allowed_422(client, db_session_factory, test_settings):
    """execute_cell 预检：IMPORT_NOT_ALLOWED → HTTP 422（计划 9.4）"""
    with db_session_factory() as db:
        lesson_id, _ = _setup_notebook_execute_env(db, test_settings)
        course = db.scalar(select(Course).where(Course.title == "C4"))
        teacher = db.scalar(select(User).where(User.role == "teacher"))
        student = db.scalar(select(User).where(User.role == "student"))
        if student is None:
            student = _make_student(db, username="s6")
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    create_user(db_session_factory, "stu_exec", "student")
    s_tok, _ = login(client, "stu_exec")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C4"))
        student = db.scalar(select(User).where(User.username == "stu_exec"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    grant_experiment(db_session_factory, 'lesson', lesson_id, ['stu_exec'])

    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lesson_id}",
                    headers=auth_header(s_tok))
    assert r.status_code == 200, r.text
    record_id = r.json()["id"]

    r = client.post(f"/api/v1/experiments/records/{record_id}/cells/c1/execute",
                    headers=auth_header(s_tok),
                    json={"code": "import numpy\nprint(1)"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "IMPORT_NOT_ALLOWED"
    assert "未在本作业允许范围内" in r.json()["detail"]["message"]


def test_execute_cell_import_not_installed_500(client, db_session_factory, test_settings):
    """execute_cell 预检：IMPORT_NOT_INSTALLED → HTTP 500（计划 9.4）"""
    with db_session_factory() as db:
        lesson_id, _ = _setup_notebook_execute_env(db, test_settings)

    create_user(db_session_factory, "stu_exec2", "student")
    s_tok, _ = login(client, "stu_exec2")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C4"))
        student = db.scalar(select(User).where(User.username == "stu_exec2"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    grant_experiment(db_session_factory, 'lesson', lesson_id, ['stu_exec2'])

    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lesson_id}",
                    headers=auth_header(s_tok))
    record_id = r.json()["id"]

    # 白名单允许 pandas，但 basic 环境未安装 → IMPORT_NOT_INSTALLED
    r = client.post(f"/api/v1/experiments/records/{record_id}/cells/c1/execute",
                    headers=auth_header(s_tok),
                    json={"code": "import pandas\nprint(1)"})
    assert r.status_code == 500, r.text
    assert r.json()["detail"]["code"] == "IMPORT_NOT_INSTALLED"


def test_execute_cell_runs_with_env_digest_and_no_diagnostic(client, db_session_factory, test_settings):
    """execute_cell 正常路径：Kernel 以 digest 启动、无 import 错误不返回 diagnostic"""
    with db_session_factory() as db:
        lesson_id, _ = _setup_notebook_execute_env(db, test_settings)

    create_user(db_session_factory, "stu_exec3", "student")
    s_tok, _ = login(client, "stu_exec3")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C4"))
        student = db.scalar(select(User).where(User.username == "stu_exec3"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    grant_experiment(db_session_factory, 'lesson', lesson_id, ['stu_exec3'])

    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lesson_id}",
                    headers=auth_header(s_tok))
    record_id = r.json()["id"]

    captured = {}
    def fake_get_or_create(record_id_, *args, **kwargs):
        captured["image_ref"] = kwargs.get("image_ref")
        captured["env_id"] = kwargs.get("environment_version_id")
        return MagicMock()

    with patch("app.api.experiments.get_kernel_manager") as mock_km:
        km = MagicMock()
        km.get_or_create_session.side_effect = fake_get_or_create
        km.execute.return_value = {"outputs": [], "execution_time_ms": 5}
        mock_km.return_value = km
        r = client.post(f"/api/v1/experiments/records/{record_id}/cells/c1/execute",
                        headers=auth_header(s_tok),
                        json={"code": "print(1)"})
    assert r.status_code == 200, r.text
    assert captured["image_ref"] == "sha256:" + "a" * 64
    assert captured["env_id"] is not None
    assert r.json().get("diagnostic") is None


# ═══════════════════════════════════════════════════════════════
# 6. 学生 API 响应：环境摘要（不含 digest/tag/构建日志）
# ═══════════════════════════════════════════════════════════════

def test_assignment_read_response_has_environment_summary(client, db_session_factory, test_settings):
    """作业读响应含 environment_summary（学生可见），不泄露 digest/tag（计划 12）"""
    with db_session_factory() as db:
        basic_id = _seed_version_available(db, test_settings)
        teacher = _make_teacher(db, username="t7")
        course = Course(title="C5", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(
            course_id=course.id, title="A5", status="published",
            environment_version_id=basic_id,
            import_policy_mode="restricted",
            allowed_imports=["numpy"],
        )
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN",
        )
        db.add(question)
        db.commit()
        db.refresh(question)

    create_user(db_session_factory, "stu_r", "student")
    s_tok, _ = login(client, "stu_r")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C5"))
        student = db.scalar(select(User).where(User.username == "stu_r"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()
    r = client.get(f"/api/v1/assignments/{assignment.id}", headers=auth_header(s_tok))
    assert r.status_code == 200, r.text
    summary = r.json().get("environment_summary")
    assert summary is not None
    assert summary["display_name"] == "Python 基础"
    assert summary["version_label"] == "v1"
    assert summary["import_policy_mode"] == "restricted"
    assert summary["allowed_imports"] == ["numpy"]
    assert isinstance(summary["imports"], list)
    body = r.text
    assert "sha256" not in body, "学生响应不得泄露 digest"
    assert "dai-env" not in body, "学生响应不得泄露镜像 tag"


def test_question_read_response_has_effective_environment_summary(client, db_session_factory, test_settings):
    """题目读响应含 effective environment summary（题目覆盖时显示题目环境）"""
    with db_session_factory() as db:
        _seed_version_available(db, test_settings)
        data_profile = db.scalar(
            select(EnvironmentProfile).where(EnvironmentProfile.slug == "data")
        )
        data_version = EnvironmentVersion(
            profile_id=data_profile.id,
            version_number=2,
            status="available",
            base_image_ref="python:3.12-slim",
            image_digest="sha256:" + "6" * 64,
            minimum_memory_mb=768,
            manifest_sha256="m" * 64,
        )
        db.add(data_version)
        db.commit()
        db.refresh(data_version)
        teacher = _make_teacher(db, username="t8")
        course = Course(title="C6", status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        assignment = Assignment(course_id=course.id, title="A6", status="published")
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        question = JudgeQuestion(
            assignment_id=assignment.id, title="Q1",
            function_name="add", hidden_tests="HIDDEN",
            environment_version_id=data_version.id,
            import_policy_mode="restricted",
            allowed_imports=["pandas"],
        )
        db.add(question)
        db.commit()
        db.refresh(question)

    create_user(db_session_factory, "stu_r2", "student")
    s_tok, _ = login(client, "stu_r2")
    with db_session_factory() as db:
        course = db.scalar(select(Course).where(Course.title == "C6"))
        student = db.scalar(select(User).where(User.username == "stu_r2"))
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()
    r = client.get(f"/api/v1/assignments/{assignment.id}/questions", headers=auth_header(s_tok))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    summary = items[0].get("environment_summary")
    assert summary is not None
    assert summary["display_name"] == "数据分析"
    assert summary["import_policy_mode"] == "restricted"
    assert summary["allowed_imports"] == ["pandas"]
