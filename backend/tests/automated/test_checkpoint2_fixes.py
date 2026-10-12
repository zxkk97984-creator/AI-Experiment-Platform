"""检查点 1 第三轮返工测试 — 真实行为覆盖"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import Base
from app.services.kernel_manager import KernelManager
from conftest import auth_header, create_course_db, create_user, login, seed_basic_environment
from kernel_test_support import run_cell
from teaching_test_support import grant_experiment

BACKEND_ROOT = Path(__file__).resolve().parents[2]


# ═══════════════════════════════════════════════════════════════
# 辅助：构造真实 NotebookTemplate + Version + 绑定到 lesson
# ═══════════════════════════════════════════════════════════════

def _create_template_with_version(db_session_factory, owner_id=1) -> tuple[int, int]:
    """创建模板并发布一个版本，返回 (template_id, version_id)"""
    with db_session_factory() as db:
        tmpl = models.NotebookTemplate(
            name="测试模板", status="published", owner_id=owner_id,
            draft_cells=[
                {"id": "c1", "type": "code", "source": "print(1)", "order": 0, "student_editable": True, "source_hidden": False},
                {"id": "c2", "type": "markdown", "source": "# 标题", "order": 1, "student_editable": False, "source_hidden": False},
                {"id": "c3", "type": "code", "source": "secret()", "order": 2, "student_editable": False, "source_hidden": True},
                {"id": "c4", "type": "code", "source": "x=1", "order": 3, "student_editable": True, "source_hidden": False},
            ],
        )
        db.add(tmpl)
        db.flush()
        ver = models.NotebookTemplateVersion(
            template_id=tmpl.id,
            version_number=1,
            sha256="a" * 64,
            cells=tmpl.draft_cells,
            cell_order=["c1", "c2", "c3", "c4"],
            published_by_id=owner_id,
        )
        db.add(ver)
        db.flush()
        tmpl.current_version_id = ver.id
        db.commit()
        return tmpl.id, ver.id


def _setup_lesson_with_template(client, db_session_factory, course_status="published"):
    """创建教师+课程+选课+章节+课时（绑定模板）+返回关键 ID"""
    create_user(db_session_factory, "t1", "teacher")
    create_user(db_session_factory, "s1", "student")
    t_tok, _ = login(client, "t1")
    s_tok, _ = login(client, "s1")

    tid, vid = _create_template_with_version(db_session_factory)

    cid = create_course_db(
        db_session_factory, teacher_username="t1", title="测试课程",
        status=course_status, visibility="public",
    )

    ch = client.post(f"/api/v1/courses/{cid}/chapters", headers=auth_header(t_tok), json={"title": "章"})
    chid = ch.json()["id"]

    # 用原始 SQL 给 lesson 绑定 template_id（PATCH lessons 可能不支持）
    with db_session_factory() as db:
        lesson = models.Lesson(
            chapter_id=chid, title="Notebook 课", content_type="markdown",
            template_id=tid,
        )
        db.add(lesson)
        db.commit()
        lid = lesson.id

    # 管理员显式分班并冻结本班实验版本；共享课程本身不授予执行权。
    if course_status == "published":
        grant_experiment(db_session_factory, "lesson", lid, ["s1"])

    return t_tok, s_tok, cid, lid, tid, vid


# ═══════════════════════════════════════════════════════════════
# 问题 1: 元数据 FK
# ═══════════════════════════════════════════════════════════════

def test_current_version_id_has_fk_in_metadata():
    table = Base.metadata.tables["notebook_templates"]
    col = table.c["current_version_id"]
    fk_targets = {fk.target_fullname for fk in col.foreign_keys}
    assert any(t.endswith(".id") and "notebook_template_versions" in t for t in fk_targets), (
        f"current_version_id 没有指向 notebook_template_versions 的 FK, 实际: {fk_targets}"
    )


# ═══════════════════════════════════════════════════════════════
# 问题 2: 跨用户拒绝
# ═══════════════════════════════════════════════════════════════

def test_cross_user_record_access_denied(client, db_session_factory):
    """A 成功创建 record，B GET/PUT/execute 全部 404，不泄露记录存在"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)
    create_user(db_session_factory, "s2", "student")
    s2_tok, _ = login(client, "s2")

    # A 创建 record（200=已存在，201=新建）
    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
    assert r.status_code in (200, 201), r.text
    rid = r.json()["id"]

    # B GET detail → 404
    r = client.get(f"/api/v1/experiments/records/{rid}", headers=auth_header(s2_tok))
    assert r.status_code == 404, f"B GET 应 404 实际 {r.status_code}"
    assert r.json()["detail"]["code"] == "RECORD_NOT_FOUND"

    # B PUT cells → 404
    r = client.put(f"/api/v1/experiments/records/{rid}/cells", headers=auth_header(s2_tok), json={
        "cells": {"c1": "print(2)"}, "record_revision": 1,
    })
    assert r.status_code == 404, f"B PUT 应 404 实际 {r.status_code}"

    # B execute → 404
    r = client.post(f"/api/v1/experiments/records/{rid}/cells/c1/execute", headers=auth_header(s2_tok), json={
        "code": "print(1)",
    })
    assert r.status_code == 404, f"B execute 应 404 实际 {r.status_code}"

    # B interrupt → 404
    r = client.post(f"/api/v1/experiments/records/{rid}/interrupt", headers=auth_header(s2_tok))
    assert r.status_code == 404

    # B restart → 404
    r = client.post(f"/api/v1/experiments/records/{rid}/restart", headers=auth_header(s2_tok))
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════
# 问题 3: 退课后 record 拒绝访问 + 列表不泄露
# ═══════════════════════════════════════════════════════════════

def test_dropped_enrollment_blocks_record_access(client, db_session_factory):
    """学生选课→创建 record→退课→GET detail 必须拒绝"""
    _, s_tok, cid, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)

    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
    assert r.status_code in (200, 201)
    rid = r.json()["id"]

    # 班级学生不能自行退课；管理员移出后立即失去执行权。
    dropped = client.delete(f"/api/v1/courses/{cid}/enroll", headers=auth_header(s_tok))
    assert dropped.status_code == 409
    create_user(db_session_factory, "roster_admin", "admin")
    admin_token, _ = login(client, "roster_admin")
    with db_session_factory() as db:
        student_id = db.query(models.User).filter_by(username="s1").one().id
    removed = client.delete(f"/api/v1/courses/{cid}/students/{student_id}", headers=auth_header(admin_token))
    assert removed.status_code == 204, removed.text

    # GET detail 应拒绝
    r = client.get(f"/api/v1/experiments/records/{rid}", headers=auth_header(s_tok))
    assert r.status_code == 403, f"退课后应 403 实际 {r.status_code}"
    assert r.json()["detail"]["code"] == "LESSON_NOT_AVAILABLE"

    # PUT cells 应拒绝
    r = client.put(f"/api/v1/experiments/records/{rid}/cells", headers=auth_header(s_tok), json={
        "cells": {"c1": "print(2)"}, "record_revision": 1,
    })
    assert r.status_code == 403

    # 列表不应泄露
    r = client.get("/api/v1/experiments/records", headers=auth_header(s_tok))
    ids = [item["id"] for item in r.json()["items"]]
    assert rid not in ids, "退课后 record 不应出现在列表"


def test_draft_course_blocks_ensure(client, db_session_factory):
    """draft course 不能 ensure record"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory, course_status="draft")
    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "COURSE_NOT_AVAILABLE"


def test_not_enrolled_blocks_ensure(client, db_session_factory):
    """未选课学生不能 ensure"""
    create_user(db_session_factory, "s_none", "student")
    s_tok, _ = login(client, "s_none")
    _, _, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)
    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "LESSON_NOT_AVAILABLE"


# ═══════════════════════════════════════════════════════════════
# 问题 4: hidden cell 真实初始化行为
# ═══════════════════════════════════════════════════════════════

class FakeKernelManager:
    """记录调用顺序和次数的 fake kernel manager"""
    def __init__(self):
        self.executed = []       # [(record_id, code)]
        self.destroyed = []      # [record_id]
        self.sessions = {}       # {record_id: FakeSession}
        self.initialized_versions = set()

    def get_or_create_session(self, record_id, lesson_storage_dir="", **kwargs):
        # image_ref / environment_version_id 等运行参数由真实实现消费，fake 忽略
        if record_id not in self.sessions:
            self.sessions[record_id] = FakeSession()
        return self.sessions[record_id]

    def execute(self, record_id, code):
        self.executed.append((record_id, code))
        return {"outputs": [], "execution_time_ms": 10,
                "outcome": "succeeded", "state_lost": False, "message": None}

    def destroy(self, record_id):
        self.destroyed.append(record_id)
        self.sessions.pop(record_id, None)
        self.initialized_versions = {
            marker for marker in self.initialized_versions if marker[0] != record_id
        }

    def interrupt(self, record_id):
        pass

    def restart(self, record_id, lesson_storage_dir=""):
        self.sessions.pop(record_id, None)
        self.initialized_versions = {
            marker for marker in self.initialized_versions if marker[0] != record_id
        }
        return self.get_or_create_session(record_id)

    def is_template_initialized(self, record_id, version_id):
        return (record_id, version_id) in self.initialized_versions

    def mark_template_initialized(self, record_id, version_id):
        self.initialized_versions.add((record_id, version_id))


class FakeSession:
    pass


@pytest.fixture()
def fake_km():
    return FakeKernelManager()


def test_hidden_cells_executed_in_order_once_per_session(client, db_session_factory, fake_km, test_settings):
    """hidden cell 按 order 执行、同一 session 只初始化一次（异步执行同步跑完）"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)

    with patch("app.api.experiments.get_kernel_manager", return_value=fake_km):
        # 第一次 execute 触发隐藏初始化
        r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
        assert r.status_code in (200, 201)
        rid = r.json()["id"]

        run_cell(client, db_session_factory, test_settings, fake_km, rid, "c1", "print(1)", auth_header(s_tok))

        # hidden cell c3 被执行过
        hidden_codes = [c for (rec, c) in fake_km.executed if c == "secret()"]
        assert len(hidden_codes) == 1, f"hidden cell 应执行一次 实际 {len(hidden_codes)}"

        # 第二次 execute 同一 session → hidden 不重复执行
        run_cell(client, db_session_factory, test_settings, fake_km, rid, "c4", "x=1", auth_header(s_tok))
        hidden_codes_after = [c for (rec, c) in fake_km.executed if c == "secret()"]
        assert len(hidden_codes_after) == 1, "hidden 不应重复执行"

        # 执行顺序：c3 (hidden) 在 c1 之前
        exec_order = fake_km.executed
        hidden_idx = next(i for i, (_, c) in enumerate(exec_order) if c == "secret()")
        c1_idx = next(i for i, (_, c) in enumerate(exec_order) if c == "print(1)")
        assert hidden_idx < c1_idx, f"hidden cell 应在用户 cell 之前执行: hidden@{hidden_idx} c1@{c1_idx}"


def test_hidden_cell_failure_destroys_kernel(client, db_session_factory):
    """hidden cell 执行失败 → destroy + KERNEL_INIT_FAILED"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)

    class FailingKM(FakeKernelManager):
        def execute(self, record_id, code):
            if code == "secret()":
                raise RuntimeError("boom")
            return super().execute(record_id, code)

    fkm = FailingKM()
    with patch("app.api.experiments.get_kernel_manager", return_value=fkm):
        r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
        rid = r.json()["id"]

        r = client.post(f"/api/v1/experiments/records/{rid}/cells/c1/execute",
                        headers=auth_header(s_tok), json={"code": "print(1)"})
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "KERNEL_INIT_FAILED"
        assert len(fkm.destroyed) == 1


def test_hidden_cells_not_in_record_or_detail(client, db_session_factory, fake_km, test_settings):
    """hidden cell ID/源码/输出/数量不出现 record 和 detail"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)

    with patch("app.api.experiments.get_kernel_manager", return_value=fake_km):
        r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
        rid = r.json()["id"]

        # cells_sources 不应含 hidden cell
        assert "c3" not in r.json().get("cells_sources", {})

        # 执行一个用户 cell（异步 + 同步跑完）
        run_cell(client, db_session_factory, test_settings, fake_km, rid, "c1", "print(1)", auth_header(s_tok))

        # GET detail
        r = client.get(f"/api/v1/experiments/records/{rid}", headers=auth_header(s_tok))
        detail = r.json()
        cell_ids = [c["id"] for c in detail["cells"]]
        assert "c3" not in cell_ids, "detail cells 不应含 hidden cell"
        # execution_count 不应计算 hidden
        assert detail["execution_count"] == 1, f"execution_count 应为 1 实际 {detail['execution_count']}"


# ═══════════════════════════════════════════════════════════════
# 问题 5: save cells 合并 + 拒绝
# ═══════════════════════════════════════════════════════════════

def test_save_cells_merges_not_overwrites(client, db_session_factory):
    """保存部分 cell 时，其他 editable cell 源码保留"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)
    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
    rid = r.json()["id"]
    rev1 = r.json()["record_revision"]

    # 只保存 c1
    r = client.put(f"/api/v1/experiments/records/{rid}/cells", headers=auth_header(s_tok), json={
        "cells": {"c1": "print(99)"}, "record_revision": rev1,
    })
    assert r.status_code == 200

    # GET detail 验证 c4 仍保留原始源码
    r = client.get(f"/api/v1/experiments/records/{rid}", headers=auth_header(s_tok))
    c4 = next(c for c in r.json()["cells"] if c["id"] == "c4")
    assert c4["source"] == "x=1", f"c4 原始源码应保留 实际 {c4['source']}"
    c1 = next(c for c in r.json()["cells"] if c["id"] == "c1")
    assert c1["source"] == "print(99)"


def test_save_cells_rejects_readonly_markdown_hidden_unknown(client, db_session_factory):
    """拒绝只读/markdown/hidden/未知 cell"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)
    r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
    rid = r.json()["id"]
    rev = r.json()["record_revision"]

    # markdown cell c2 不可编辑
    r = client.put(f"/api/v1/experiments/records/{rid}/cells", headers=auth_header(s_tok), json={
        "cells": {"c2": "# changed"}, "record_revision": rev,
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "CELL_NOT_EDITABLE"

    # hidden cell c3 不可编辑
    r = client.put(f"/api/v1/experiments/records/{rid}/cells", headers=auth_header(s_tok), json={
        "cells": {"c3": "hack"}, "record_revision": rev,
    })
    assert r.status_code == 403
    assert "CELL_NOT_EDITABLE" in r.json()["detail"]["code"]

    # 未知 cell
    r = client.put(f"/api/v1/experiments/records/{rid}/cells", headers=auth_header(s_tok), json={
        "cells": {"unknown_x": "x"}, "record_revision": rev,
    })
    assert r.status_code == 403
    assert "CELL_NOT_EDITABLE" in r.json()["detail"]["code"]


# ═══════════════════════════════════════════════════════════════
# 问题 6: 可见执行序号
# ═══════════════════════════════════════════════════════════════

def test_execution_count_increments_per_visible_cell(client, db_session_factory, fake_km, test_settings):
    """同一 cell 连跑三次得到 1、2、3（异步执行：POST 202 → 同步跑完 → 读执行状态）"""
    _, s_tok, _, lid, _, _ = _setup_lesson_with_template(client, db_session_factory)

    with patch("app.api.experiments.get_kernel_manager", return_value=fake_km):
        r = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid}", headers=auth_header(s_tok))
        rid = r.json()["id"]

        for expected_count in (1, 2, 3):
            execution_id = run_cell(client, db_session_factory, test_settings, fake_km,
                                    rid, "c1", "print(1)", auth_header(s_tok))
            r = client.get(f"/api/v1/experiments/records/{rid}/executions/{execution_id}",
                           headers=auth_header(s_tok))
            assert r.status_code == 200, r.text
            assert r.json()["execution_count"] == expected_count, (
                f"执行第{expected_count}次 期望{expected_count} 实际{r.json()['execution_count']}"
            )


# ═══════════════════════════════════════════════════════════════
# 问题 7: notebooks 转发 + catchall
# ═══════════════════════════════════════════════════════════════

def test_notebooks_get_returns_retired_with_deprecation(client, db_session_factory):
    """旧 notebooks 入口统一终止，并提示迁移到 experiments。"""
    create_user(db_session_factory, "tnb", "teacher")
    create_user(db_session_factory, "snb", "student")
    t_tok, _ = login(client, "tnb")
    s_tok, _ = login(client, "snb")

    cid = create_course_db(
        db_session_factory, teacher_username="tnb", title="NB Course",
        status="published", visibility="public",
    )
    ch = client.post(f"/api/v1/courses/{cid}/chapters", headers=auth_header(t_tok), json={"title": "Ch"})
    le = client.post(f"/api/v1/chapters/{ch.json()['id']}/lessons", headers=auth_header(t_tok), json={
        "title": "No Template", "content_type": "markdown",
    })
    lid = le.json()["id"]
    client.post(f"/api/v1/courses/{cid}/enroll", headers=auth_header(s_tok))

    r = client.get(f"/api/v1/notebooks/{lid}", headers=auth_header(s_tok))
    assert r.headers.get("Deprecation") == "true"
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "DEPRECATED"


def test_notebooks_catchall_returns_410_and_deprecation(client, db_session_factory):
    """未映射的旧 notebooks 路由返回 410 DEPRECATED"""
    create_user(db_session_factory, "sc", "student")
    s_tok, _ = login(client, "sc")

    r = client.get("/api/v1/notebooks/some/old/path", headers=auth_header(s_tok))
    assert r.headers.get("Deprecation") == "true"
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "DEPRECATED"


# ═══════════════════════════════════════════════════════════════
# 问题 8: 注释文字精确测试
# ═══════════════════════════════════════════════════════════════

def test_experiments_py_has_exact_user_comment():
    content = (BACKEND_ROOT / "app" / "api" / "experiments.py").read_text(encoding="utf-8")
    assert "实验模块 API（Notebook 风格 kernel 端点）" in content, (
        "缺少用户原始注释「实验模块 API（Notebook 风格 kernel 端点）」"
    )


# ═══════════════════════════════════════════════════════════════
# 问题 9: 迁移 downgrade 到上一 revision 再 upgrade head
# ═══════════════════════════════════════════════════════════════

# 迁移 B（c5d6e7f8a901）前置校验：basic 档位必须存在 available 且带 image_digest 的版本
REVISION_A = "b4c5d6e7f890"


def _alembic_env(tmp_path, db_name):
    db_path = tmp_path / db_name
    env = dict(__import__("os").environ)
    env["DAI_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return env


def _upgrade_head_seeded(env):
    """分段升级到 head：迁移 A → 插入 basic v1 种子 → head（迁移 B 前置校验）。"""
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


def test_migration_downgrade_one_step_then_upgrade_head(tmp_path):
    """downgrade -1 再 upgrade head"""
    env = _alembic_env(tmp_path, "step.db")
    # V2 migrations are forward-only by default.  This test deliberately
    # exercises the reversible path on a disposable SQLite database.
    env["DAI_ALLOW_ENVIRONMENT_V2_DOWNGRADE"] = "true"
    env["DAI_ALLOW_ACCOUNT_MANAGEMENT_DOWNGRADE"] = "true"
    env["DAI_ALLOW_LAB_DOWNGRADE"] = "true"
    env["DAI_ALLOW_TEACHING_DOWNGRADE"] = "1"
    env["DAI_ALLOW_EXAM_INTEGRITY_DOWNGRADE"] = "1"

    _upgrade_head_seeded(env)

    r2 = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "-1"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=30, env=env,
    )
    assert r2.returncode == 0, f"downgrade -1 failed: {r2.stderr}\n{r2.stdout}"

    # disposable school migration 单步回退后，保留旧表及种子数据，可重新升级。
    r3 = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=30, env=env,
    )
    assert r3.returncode == 0, f"re-upgrade failed: {r3.stderr}\n{r3.stdout}"
