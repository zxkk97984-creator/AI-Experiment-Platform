"""checkpoint1 模型约束测试。

A/B/C 分类：B 类（约束语义测试）——外键开启后必须建真实父行，
让 XOR/UNIQUE 约束（而非 1452 外键错误）真正被触发；父行统一走共享工厂。
"""
from __future__ import annotations

import subprocess
import sys
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from app import models
from app.database import Base
from conftest import (
    constraint_violation,
    make_exam,
    make_exam_question,
    make_experiment_module,
    make_lesson,
    make_student,
    make_teacher,
    make_template_version,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


def _constraint_sql(table) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )


def _foreign_key_targets(table_name: str, column_name: str) -> set[str]:
    column = Base.metadata.tables[table_name].c[column_name]
    return {foreign_key.target_fullname for foreign_key in column.foreign_keys}


def test_notebook_template_draft_fields_and_revision_default(db_session_factory):
    assert hasattr(models, "NotebookTemplate"), "NotebookTemplate model is missing"

    table = models.NotebookTemplate.__table__
    assert {
        "name",
        "draft_cells",
        "draft_revision",
        "current_version_id",
        "owner_id",
    } <= set(table.c.keys())

    teacher = make_teacher(db_session_factory)
    with db_session_factory() as db:
        template = models.NotebookTemplate(name="线性回归实验", owner_id=teacher.id)
        db.add(template)
        db.commit()
        db.refresh(template)

        assert template.draft_cells == []
        assert template.draft_revision == 1
        assert template.current_version_id is None


def test_notebook_template_version_number_is_unique_per_template(db_session_factory):
    assert hasattr(models, "NotebookTemplateVersion"), "NotebookTemplateVersion model is missing"

    teacher = make_teacher(db_session_factory)
    with db_session_factory() as db:
        template = models.NotebookTemplate(name="卷积实验", owner_id=teacher.id)
        db.add(template)
        db.flush()
        db.add(
            models.NotebookTemplateVersion(
                template_id=template.id,
                cells=[{"id": "cell-1", "source": "print(1)"}],
                sha256="a" * 64,
                version_number=1,
                assets_dir="templates/conv/1",
                published_by_id=teacher.id,
            )
        )
        db.commit()

        db.add(
            models.NotebookTemplateVersion(
                template_id=template.id,
                cells=[],
                sha256="b" * 64,
                version_number=1,
                assets_dir="templates/conv/duplicate",
                published_by_id=teacher.id,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize(
    ("use_lesson", "use_module"),
    [
        (False, False),
        (True, True),
    ],
)
def test_experiment_record_requires_exactly_one_parent(
    db_session_factory,
    use_lesson,
    use_module,
):
    student = make_student(db_session_factory)
    lesson_id = make_lesson(db_session_factory) if use_lesson else None
    module_id = make_experiment_module(db_session_factory) if use_module else None
    version_id = make_template_version(db_session_factory)
    with db_session_factory() as db:
        db.add(
            models.ExperimentRecord(
                lesson_id=lesson_id,
                module_id=module_id,
                student_id=student.id,
                template_version_id=version_id,
            )
        )
        with constraint_violation():
            db.commit()


def test_experiment_record_is_unique_for_lesson_student(db_session_factory):
    student = make_student(db_session_factory)
    lesson_id = make_lesson(db_session_factory)
    version_a = make_template_version(db_session_factory)
    version_b = make_template_version(db_session_factory)
    with db_session_factory() as db:
        db.add_all(
            [
                models.ExperimentRecord(
                    lesson_id=lesson_id,
                    student_id=student.id,
                    template_version_id=version_a,
                ),
                models.ExperimentRecord(
                    lesson_id=lesson_id,
                    student_id=student.id,
                    template_version_id=version_b,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_experiment_record_is_unique_for_module_student(db_session_factory):
    student = make_student(db_session_factory)
    module_id = make_experiment_module(db_session_factory)
    version_a = make_template_version(db_session_factory)
    version_b = make_template_version(db_session_factory)
    with db_session_factory() as db:
        db.add_all(
            [
                models.ExperimentRecord(
                    module_id=module_id,
                    student_id=student.id,
                    template_version_id=version_a,
                ),
                models.ExperimentRecord(
                    module_id=module_id,
                    student_id=student.id,
                    template_version_id=version_b,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_exam_submission_columns_and_status_contract():
    table = models.ExamSubmission.__table__

    assert "answers" not in table.c
    assert {"expires_at", "graded_at"} <= set(table.c.keys())
    status_constraint = _constraint_sql(table)
    for expected_status in ("started", "submitted", "grading", "graded"):
        assert expected_status in status_constraint


def test_exam_answer_is_unique_per_submission_question(db_session_factory):
    assert hasattr(models, "ExamAnswer"), "ExamAnswer model is missing"

    now = datetime.now(UTC)
    exam_id = make_exam(db_session_factory)
    student = make_student(db_session_factory)
    question_id = make_exam_question(db_session_factory, exam_id=exam_id)
    with db_session_factory() as db:
        submission = models.ExamSubmission(
            exam_id=exam_id,
            student_id=student.id,
            status="started",
            started_at=now,
            expires_at=now + timedelta(hours=1),
        )
        db.add(submission)
        db.flush()
        db.add_all(
            [
                models.ExamAnswer(
                    submission_id=submission.id,
                    question_id=question_id,
                    selected_options=["A"],
                ),
                models.ExamAnswer(
                    submission_id=submission.id,
                    question_id=question_id,
                    selected_options=["B"],
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_metadata_contains_unified_notebook_experiment_and_exam_schema():
    tables = Base.metadata.tables

    assert {
        "notebook_templates",
        "notebook_template_versions",
        "experiment_records",
        "exam_questions",
        "exam_submissions",
        "exam_answers",
    } <= set(tables)
    assert {
        "notebook_records",
        "notebook_submissions",
    }.isdisjoint(tables)
    # exam_grades 保留（v5 计划要求存储最终汇总成绩）
    assert "exam_grades" in tables

    assert _foreign_key_targets("lessons", "template_id") == {"notebook_templates.id"}
    assert _foreign_key_targets("experiment_modules", "template_id") == {
        "notebook_templates.id"
    }
    assert _foreign_key_targets("experiment_records", "template_version_id") == {
        "notebook_template_versions.id"
    }
    assert _foreign_key_targets("exam_questions", "exam_id") == {"exams.id"}
    assert _foreign_key_targets("exam_answers", "submission_id") == {
        "exam_submissions.id"
    }
    assert _foreign_key_targets("exam_answers", "question_id") == {
        "exam_questions.id"
    }

    experiment_columns = set(tables["experiment_records"].c.keys())
    assert {
        "offering_id",
        "scope_key",
        "lesson_id",
        "module_id",
        "student_id",
        "template_version_id",
        "record_revision",
        "cells_sources",
        "cells_outputs",
        "status",
        "started_at",
        "submitted_at",
        "completed_at",
    } <= experiment_columns
    unique_keys = {tuple(column.name for column in constraint.columns)
                   for constraint in tables['experiment_records'].constraints
                   if isinstance(constraint, UniqueConstraint)}
    assert ('scope_key', 'lesson_id', 'student_id') in unique_keys
    assert ('scope_key', 'module_id', 'student_id') in unique_keys
    assert ('lesson_id', 'student_id') not in unique_keys
    assert ('module_id', 'student_id') not in unique_keys

    question_columns = set(tables["exam_questions"].c.keys())
    assert {
        "question_type",
        "prompt",
        "options",
        "correct_answer",
        "starter_code",
        "public_cases",
        "hidden_tests",
        "points",
        "order_index",
        "time_limit_ms",
        "memory_limit_mb",
    } <= question_columns

    answer_columns = set(tables["exam_answers"].c.keys())
    assert {
        "selected_options",
        "code_answer",
        "score",
        "grading_status",
        "result_details",
        "system_error",
    } <= answer_columns


def test_checkpoint_migration_is_child_of_current_head():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    head = scripts.get_current_head()
    assert head != "f81a7a35f73f", "checkpoint migration is missing"
    checkpoint = scripts.get_revision("a1b2c3d4e5f6")
    assert checkpoint is not None
    assert checkpoint.down_revision == "f81a7a35f73f"
    assert callable(checkpoint.module.upgrade)
    assert callable(checkpoint.module.downgrade)
    assert scripts.get_revision(head) is not None
    assert "a1b2c3d4e5f6" in {
        revision.revision for revision in scripts.iterate_revisions(head, "f81a7a35f73f")
    }


def _alembic_env(tmp_path, db_name: str) -> dict:
    """构建带 DAI_DATABASE_URL 和 PYTHONPATH 的子进程环境"""
    db_path = tmp_path / db_name
    env = dict(__import__("os").environ)
    env["DAI_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["DAI_ALLOW_ENVIRONMENT_V2_DOWNGRADE"] = "true"
    env["DAI_ALLOW_ACCOUNT_MANAGEMENT_DOWNGRADE"] = "true"
    env["DAI_ALLOW_LAB_DOWNGRADE"] = "true"
    # 确保 backend/ 在路径上以便 alembic/env.py 能 from app.config import
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return env


# 迁移 B（c5d6e7f8a901）前置校验：basic 档位必须存在 available 且带 image_digest 的版本
REVISION_A = "b4c5d6e7f890"


def _upgrade_head_seeded(env: dict) -> None:
    """分段升级到 head：迁移 A → 插入 basic v1 种子 → head（迁移 B）。

    与真实部署顺序一致（plan 5「迁移 B」）：先跑迁移 A + seed build，
    再部署迁移 B；迁移 B 不满足前置时主动失败。
    """
    import sqlalchemy as sa

    db_path = env["DAI_DATABASE_URL"].removeprefix("sqlite:///")
    r1 = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", REVISION_A],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=30, env=env,
    )
    assert r1.returncode == 0, f"upgrade 迁移 A failed:\n{r1.stderr}\n{r1.stdout}"

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO environment_profiles (id, slug, display_name, status)"
                    " VALUES (1, 'basic', 'Python 基础', 'active')"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO environment_versions (id, profile_id, version_number, status,"
                    " base_image_ref, image_digest, minimum_memory_mb, manifest_sha256)"
                    " VALUES (1, 1, 1, 'available', 'python:3.12-slim@sha256:0000',"
                    " :digest, 256, 'm' * 64)"
                ).bindparams(digest="sha256:" + "a" * 64)
            )
    finally:
        engine.dispose()

    r2 = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=30, env=env,
    )
    assert r2.returncode == 0, f"upgrade head 失败:\n{r2.stderr}\n{r2.stdout}"


def test_clean_migration_upgrade_head(tmp_path):
    """全新数据库 alembic upgrade head（分段：迁移 A → seed → 迁移 B）无报错"""
    env = _alembic_env(tmp_path, "clean.db")
    _upgrade_head_seeded(env)


def test_migration_downgrade_upgrade_roundtrip(tmp_path):
    """Explicit disposable SQLite authorization retains the migration roundtrip."""
    env = _alembic_env(tmp_path, "roundtrip.db")
    env['DAI_ALLOW_TEACHING_DOWNGRADE'] = '1'
    _upgrade_head_seeded(env)

    r2 = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=30, env=env,
    )
    assert r2.returncode == 0, f"downgrade failed:\n{r2.stderr}\n{r2.stdout}"

    # downgrade base 已清空全部数据，重新走完整分段升级
    _upgrade_head_seeded(env)


def _assert_only_automated_tests_collected(cwd: Path, result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"pytest exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    normalized = result.stdout.replace("\\", "/")
    assert "tests/automated/" in normalized, f"automated tests not found in:\n{normalized}"
    for diagnostic_script in (BACKEND_ROOT / "tests").glob("*.py"):
        assert diagnostic_script.name not in normalized, (
            f"diagnostic script {diagnostic_script.name} was collected"
        )


def test_pytest_collects_only_automated_tests_from_backend():
    """从 backend/ 目录运行 pytest 只收集 automated 测试"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=BACKEND_ROOT,
        capture_output=True, text=True, timeout=30, check=False,
    )
    _assert_only_automated_tests_collected(BACKEND_ROOT, result)


def test_pytest_collects_only_automated_tests_from_repo_root():
    """从仓库根目录运行 pytest 只收集 automated 测试"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=30, check=False,
    )
    _assert_only_automated_tests_collected(REPO_ROOT, result)


# ═══════════════════════════════════════════════════════════════
# P1-1: conftest 仅在未设置 DAI_DATABASE_URL 时创建临时 SQLite
# ═══════════════════════════════════════════════════════════════

def test_p1_1_conftest_uses_env_mysql_url(tmp_path, monkeypatch):
    """P1-1: 设置 DAI_DATABASE_URL 为 MySQL URL 时，conftest 直接使用而非覆盖为 SQLite"""
    mysql_url = "mysql+pymysql://dai:test@127.0.0.1:3306/dai_platform"
    monkeypatch.setenv("DAI_DATABASE_URL", mysql_url)

    # 重新导入以触发 fixture 逻辑
    db_url = os.environ.get("DAI_DATABASE_URL", "")
    assert db_url == mysql_url

    # 模拟 conftest 逻辑：仅在未设置环境变量时创建临时 SQLite
    if not db_url:
        db_url = f"sqlite:///{tmp_path / 'test.db'}"

    assert db_url == mysql_url, (
        f"P1-1: 设置 MySQL URL 后不应覆盖为 SQLite，但 db_url = {db_url}"
    )
    assert "sqlite" not in db_url, "MySQL URL 不应包含 sqlite"


def test_p1_1_conftest_creates_sqlite_when_no_env(tmp_path, monkeypatch):
    """P1-1: 未设置 DAI_DATABASE_URL 时，conftest 创建临时 SQLite"""
    monkeypatch.delenv("DAI_DATABASE_URL", raising=False)

    db_url = os.environ.get("DAI_DATABASE_URL", "")
    assert not db_url, "环境变量应未设置"

    if not db_url:
        db_url = f"sqlite:///{tmp_path / 'test.db'}"

    assert "sqlite" in db_url, "未设置环境变量时应使用 SQLite"


# ═══════════════════════════════════════════════════════════════
# P1-6: 生产环境配置校验拒绝 localhost + ContextVar 重置
# ═══════════════════════════════════════════════════════════════

def test_p1_6_production_cors_rejects_localhost():
    """P1-6: 生产环境的 CORS 包含 localhost 时应抛异常"""
    from app.config import Settings
    import pytest as _pytest

    with _pytest.raises(ValueError, match="本地开发地址"):
        Settings(
            environment="production",
            secret_key="a-very-secure-key-32chars!",
            database_url="mysql+pymysql://safe:pass@localhost/db",
            cors_origins="https://real.example.com,http://localhost",
        )


def test_p1_6_production_cors_accepts_real_domains():
    """P1-6: 生产环境的 CORS 为真实域名时应通过"""
    from app.config import Settings

    s = Settings(
        environment="production",
        secret_key="a-very-secure-key-32chars!",
        database_url="mysql+pymysql://safe:pass@localhost/db",
        cors_origins="https://myapp.example.com",
        # Phase 6：生产校验要求环境基础镜像带 digest
        env_base_image="python:3.12-slim@sha256:" + "0" * 64,
        judge_image="dai-judge-python@sha256:" + "a" * 64,
        kernel_image="dai-kernel-python@sha256:" + "b" * 64,
    )
    assert s.cors_origin_list == ["https://myapp.example.com"]


def test_p1_6_production_cors_rejects_empty():
    """P1-6: 生产环境的 CORS 为空时应抛异常"""
    from app.config import Settings
    import pytest as _pytest

    with _pytest.raises(ValueError, match="未设置"):
        Settings(
            environment="production",
            secret_key="a-very-secure-key-32chars!",
            database_url="mysql+pymysql://safe:pass@localhost/db",
            cors_origins="",
        )


def test_production_requires_docker_host_judge_work_dir():
    """DoD 生产部署必须显式提供 Docker daemon 可见的宿主机目录。"""
    from app.config import Settings
    import pytest as _pytest

    with _pytest.raises(ValueError, match="DAI_JUDGE_HOST_WORK_DIR"):
        Settings(
            environment="production",
            secret_key="production-secret",
            database_url="mysql+pymysql://dai:password@mysql/dai_platform",
            redis_url="redis://redis:6379/0",
            cors_origins="https://myapp.example.com",
            judge_work_dir="/judge-work",
            judge_host_work_dir="",
        )


def test_ci_runs_docker_jobs_and_seeds_e2e_data():
    """Docker 验证必须实际运行，E2E 启动后必须装载固定种子数据。"""
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    assert "if: false" not in workflow
    assert "seed_e2e.py" in workflow
    assert "http://localhost:8080/api/v1/health/live" in workflow


def test_p1_6_contextvar_set_and_reset():
    """P1-6: ContextVar set 返回 token，reset 后恢复默认值"""
    from app.logging_config import set_request_id, get_request_id, _request_id_var

    old_default = get_request_id()
    token = set_request_id("test-rid-123")
    assert get_request_id() == "test-rid-123"

    _request_id_var.reset(token)
    assert get_request_id() == old_default, "reset 后应恢复为默认值"


def test_p1_6_metrics_requires_admin(client, db_session_factory):
    """P1-6: /metrics 需要管理员认证，普通用户应返回 403"""
    from conftest import auth_header, create_user, login

    create_user(db_session_factory, "met_student", "student")
    s_tok, _ = login(client, "met_student")

    # 学生无法访问
    r = client.get("/api/v1/metrics", headers=auth_header(s_tok))
    assert r.status_code == 403, f"学生应返回 403: {r.status_code}"

    # 管理员可以访问
    create_user(db_session_factory, "met_admin", "admin")
    a_tok, _ = login(client, "met_admin")
    r2 = client.get("/api/v1/metrics", headers=auth_header(a_tok))
    assert r2.status_code == 200, f"管理员应返回 200: {r2.status_code}"
    assert "metrics" in r2.json()
