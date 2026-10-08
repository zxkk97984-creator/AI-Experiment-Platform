"""Phase 4：教师端环境选择测试

覆盖（plan 第 8.1/9.1/11 节与 Phase 4 检查项）：
- 教师创建作业显式传 available 环境版本并保存
- 创建作业传不可用（draft）版本 → 409 VERSION_NOT_AVAILABLE
- 创建作业省略环境 → 服务层解析 basic 当前可用版本
- 发布门禁：默认/覆盖环境不可用 → 409；题目内存低于环境最低值 → 409 MEMORY_BELOW_ENV_MIN
- 已发布作业环境字段不可直接修改（发布后绑定不可变）
- 题目覆盖环境（inherit 默认 + 指定版本）与 import 策略
- 教师 available 选项字段完整且无敏感字段
- Studio 模板创建/导入带环境、草稿保存环境与 cells 同一 revision、发布复制到模板版本
- Studio 发布时草稿环境不可用 → 409
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.no_auto_env_seed

import json

from sqlalchemy import select

from app.models import (
    Assignment,
    Chapter,
    Course,
    CourseEnrollment,
    EnvironmentProfile,
    EnvironmentVersion,
    JudgeQuestion,
    Lesson,
    NotebookTemplate,
    NotebookTemplateVersion,
    User,
)
from app.services.environment_seed import seed_environment_catalog
from conftest import auth_header, create_user, login

API = "/api/v1/environments"
ASSIGN_API = "/api/v1/assignments"
STUDIO_API = "/api/v1/studio"


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════

def _login_teacher(client, db_session_factory, username="teach4"):
    create_user(db_session_factory, username, "teacher")
    tok, _ = login(client, username)
    return tok


def _seed(db_session_factory, test_settings, *, basic_available=True):
    """seed 三个档位；basic v1 默认标记 available（带 digest），返回 basic 版本 id。"""
    with db_session_factory() as db:
        seed_environment_catalog(db, test_settings)
        basic = db.scalar(
            select(EnvironmentVersion)
            .join(EnvironmentProfile, EnvironmentProfile.id == EnvironmentVersion.profile_id)
            .where(EnvironmentProfile.slug == "basic", EnvironmentVersion.version_number == 1)
        )
        assert basic is not None, "seed 后 basic v1 应存在"
        if basic_available and basic.status != "available":
            basic.status = "available"
            basic.image_digest = "sha256:" + "a" * 64
            basic.python_version = "3.12"
            db.commit()
        return basic.id


def _make_data_available(db_session_factory):
    """把 data v1 标记为 available（768 MB），返回版本 id。"""
    with db_session_factory() as db:
        data = db.scalar(
            select(EnvironmentVersion)
            .join(EnvironmentProfile, EnvironmentProfile.id == EnvironmentVersion.profile_id)
            .where(EnvironmentProfile.slug == "data", EnvironmentVersion.version_number == 1)
        )
        assert data is not None
        data.status = "available"
        data.image_digest = "sha256:" + "b" * 64
        data.python_version = "3.12"
        db.commit()
        return data.id


def _make_draft_version(db_session_factory, slug="basic"):
    """创建一个全新的 draft 版本（未构建），用于校验教师不可选择/发布不可用版本。"""
    with db_session_factory() as db:
        profile = db.scalar(
            select(EnvironmentProfile).where(EnvironmentProfile.slug == slug)
        )
        assert profile is not None
        version = EnvironmentVersion(
            profile_id=profile.id,
            version_number=99,
            status="draft",
            base_image_ref="python:3.12-slim",
            minimum_memory_mb=256,
            manifest_sha256="m" * 64,
        )
        db.add(version)
        db.commit()
        return version.id


def _make_course_with_lesson(db_session_factory, teacher_id, title="教师课程"):
    """建课程 + 章节 + 课时，返回 (course_id, lesson_id)。

    同时创建一名已选课学生作为作业发布的 all_enrolled audience，
    以适应当前“发布作业/考试必须有有效 audience”的后端规则。
    """
    with db_session_factory() as db:
        course = Course(title=title, status="published", teacher_id=teacher_id)
        db.add(course)
        db.flush()
        chapter = Chapter(course_id=course.id, title="第 1 章")
        db.add(chapter)
        db.flush()
        lesson = Lesson(chapter_id=chapter.id, title="课时 1", content_type="notebook")
        db.add(lesson)
        student = db.scalar(select(User).where(User.username == "env-student"))
        if student is None:
            student = User(
                username="env-student",
                real_name="环境测试学生",
                role="student",
                status="active",
                password_hash="not-used",
            )
            db.add(student)
            db.flush()
        db.add(CourseEnrollment(
            course_id=course.id,
            student_id=student.id,
            status="enrolled",
            origin="manual",
        ))
        db.commit()
        return course.id, lesson.id


# ═══════════════════════════════════════════════════════════════
# 作业：创建与默认解析
# ═══════════════════════════════════════════════════════════════

def test_teacher_creates_assignment_with_environment(client, db_session_factory, test_settings):
    """创建作业显式传 available 环境 → 201 且保存环境与 import 策略（plan 8.1）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{ASSIGN_API}",
        headers=auth_header(tok),
        json={
            "course_id": course_id,
            "title": "环境作业",
            "environment_version_id": basic_id,
            "import_policy_mode": "restricted",
            "allowed_imports": ["numpy", "pandas"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["environment_version_id"] == basic_id
    assert body["import_policy_mode"] == "restricted"
    assert body["allowed_imports"] == ["numpy", "pandas"]


def test_create_assignment_rejects_unavailable_environment(client, db_session_factory, test_settings):
    """教师只能选择 available 版本——draft 版本 → 409 VERSION_NOT_AVAILABLE"""
    basic_id = _seed(db_session_factory, test_settings, basic_available=False)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{ASSIGN_API}",
        headers=auth_header(tok),
        json={"course_id": course_id, "title": "A", "environment_version_id": basic_id},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "VERSION_NOT_AVAILABLE"


def test_create_assignment_defaults_to_basic_available(client, db_session_factory, test_settings):
    """省略环境字段 → 服务层解析 basic 当前可用版本（Phase 4 服务层接管创建路径）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(f"{ASSIGN_API}", headers=auth_header(tok), json={"course_id": course_id, "title": "A"})
    assert r.status_code == 201, r.text
    assert r.json()["environment_version_id"] == basic_id


def test_assignment_allowed_imports_normalized(client, db_session_factory, test_settings):
    """白名单 import 名归一化去重（sklearn.metrics → sklearn）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{ASSIGN_API}",
        headers=auth_header(tok),
        json={
            "course_id": course_id,
            "title": "A",
            "environment_version_id": basic_id,
            "import_policy_mode": "restricted",
            "allowed_imports": ["numpy", "numpy", "sklearn.metrics"],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["allowed_imports"] == ["numpy", "sklearn"]


# ═══════════════════════════════════════════════════════════════
# 作业：发布门禁与不可变
# ═══════════════════════════════════════════════════════════════

def test_publish_gate_blocks_unavailable_environment(client, db_session_factory, test_settings):
    """作业默认环境未构建（draft）→ 发布 409（plan 8.1 发布门禁）"""
    basic_id = _seed(db_session_factory, test_settings, basic_available=False)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)
    with db_session_factory() as db:
        assignment = Assignment(
            course_id=course_id, title="A", status="draft",
            environment_version_id=basic_id,
        )
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id

    r = client.post(f"{ASSIGN_API}/{assignment_id}/publish", headers=auth_header(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "VERSION_NOT_AVAILABLE"


def test_publish_gate_blocks_insufficient_memory(client, db_session_factory, test_settings):
    """题目内存低于环境最低内存 → 409 MEMORY_BELOW_ENV_MIN（plan 3 内存门禁）"""
    basic_id = _seed(db_session_factory, test_settings)  # basic minimum_memory_mb=256
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)
    with db_session_factory() as db:
        assignment = Assignment(
            course_id=course_id, title="A", status="draft",
            environment_version_id=basic_id,
        )
        db.add(assignment)
        db.commit()
        assignment_id = assignment.id
        question = JudgeQuestion(
            assignment_id=assignment_id, title="Q", function_name="f",
            hidden_tests="assert True", memory_limit_mb=128, grading_mode="legacy",
        )
        db.add(question)
        from conftest import grant_teaching_fixture
        student = db.scalar(select(User).where(User.username == 'env-student'))
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[student.id])
        db.commit()

    r = client.post(f"{ASSIGN_API}/{assignment_id}/publish", headers=auth_header(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "MEMORY_BELOW_ENV_MIN"

    # 调高内存后可发布（无 AI 题目、环境可用）
    with db_session_factory() as db:
        question = db.scalar(
            select(JudgeQuestion).where(JudgeQuestion.assignment_id == assignment_id)
        )
        assert question is not None
        question.memory_limit_mb = 512
        db.commit()
    r = client.post(f"{ASSIGN_API}/{assignment_id}/publish", headers=auth_header(tok))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"


def test_published_assignment_environment_immutable(client, db_session_factory, test_settings):
    """已发布作业环境字段不可直接改 → 409 ASSIGNMENT_NOT_EDITABLE；draft 可改（plan 8.1）"""
    basic_id = _seed(db_session_factory, test_settings)
    data_id = _make_data_available(db_session_factory)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)

    with db_session_factory() as db:
        assignment = Assignment(
            course_id=course_id, title="A", status="published",
            environment_version_id=basic_id,
        )
        db.add(assignment)
        db.commit()
        published_id = assignment.id
        draft = Assignment(
            course_id=course_id, title="B", status="draft",
            environment_version_id=basic_id,
        )
        db.add(draft)
        db.commit()
        draft_id = draft.id

    r = client.patch(
        f"{ASSIGN_API}/{published_id}",
        headers=auth_header(tok),
        json={"environment_version_id": data_id},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "ASSIGNMENT_NOT_EDITABLE"

    # 非环境字段仍可改（标题）
    r = client.patch(
        f"{ASSIGN_API}/{published_id}",
        headers=auth_header(tok),
        json={"title": "新标题"},
    )
    assert r.status_code == 200, r.text

    # draft 作业可切换环境，且新环境必须 available
    r = client.patch(
        f"{ASSIGN_API}/{draft_id}",
        headers=auth_header(tok),
        json={"environment_version_id": data_id, "import_policy_mode": "restricted"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["environment_version_id"] == data_id


# ═══════════════════════════════════════════════════════════════
# 题目：覆盖环境与 import 策略
# ═══════════════════════════════════════════════════════════════

def _create_draft_assignment(client, tok, course_id):
    r = client.post(f"{ASSIGN_API}", headers=auth_header(tok), json={"course_id": course_id, "title": "A"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_question_override_environment(client, db_session_factory, test_settings):
    """题目可覆盖作业默认环境；默认 inherit（environment_version_id=None）（plan 8.1）"""
    _seed(db_session_factory, test_settings)
    data_id = _make_data_available(db_session_factory)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)
    assignment_id = _create_draft_assignment(client, tok, course_id)

    base = {
        "title": "Q1", "function_name": "f", "hidden_tests": "assert True",
    }
    r = client.post(
        f"{ASSIGN_API}/{assignment_id}/questions",
        headers=auth_header(tok),
        json={**base, "environment_version_id": data_id, "import_policy_mode": "restricted", "allowed_imports": ["numpy"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["environment_version_id"] == data_id
    assert body["import_policy_mode"] == "restricted"
    assert body["allowed_imports"] == ["numpy"]

    # 不带环境 → 继承作业（None），策略默认 inherit
    r = client.post(
        f"{ASSIGN_API}/{assignment_id}/questions",
        headers=auth_header(tok),
        json={**base, "title": "Q2"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["environment_version_id"] is None
    assert r.json()["import_policy_mode"] == "inherit"

    # 题目覆盖不可用版本 → 409
    unavailable = _make_draft_version(db_session_factory)
    r = client.post(
        f"{ASSIGN_API}/{assignment_id}/questions",
        headers=auth_header(tok),
        json={**base, "title": "Q3", "environment_version_id": unavailable},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "VERSION_NOT_AVAILABLE"


def test_update_question_environment(client, db_session_factory, test_settings):
    """draft 作业内更新题目环境字段生效；切回继承可置 None（plan 8.1）"""
    basic_id = _seed(db_session_factory, test_settings)
    data_id = _make_data_available(db_session_factory)
    tok = _login_teacher(client, db_session_factory)
    course_id, _ = _make_course_with_lesson(db_session_factory, teacher_id=1)
    assignment_id = _create_draft_assignment(client, tok, course_id)

    r = client.post(
        f"{ASSIGN_API}/{assignment_id}/questions",
        headers=auth_header(tok),
        json={"title": "Q1", "function_name": "f", "hidden_tests": "assert True"},
    )
    qid = r.json()["id"]

    r = client.patch(
        f"{ASSIGN_API}/{assignment_id}/questions/{qid}",
        headers=auth_header(tok),
        json={"environment_version_id": data_id, "import_policy_mode": "unrestricted"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["environment_version_id"] == data_id
    assert r.json()["import_policy_mode"] == "unrestricted"

    # 覆盖版本不可用 → 409
    r = client.patch(
        f"{ASSIGN_API}/{assignment_id}/questions/{qid}",
        headers=auth_header(tok),
        json={"environment_version_id": basic_id},
    )
    assert r.status_code == 200, r.text


# ═══════════════════════════════════════════════════════════════
# 教师可用环境选项
# ═══════════════════════════════════════════════════════════════

def test_teacher_available_option_shape(client, db_session_factory, test_settings):
    """available 选项含 picker 所需字段（包摘要/内存），不含 digest/tag（plan 6.2）"""
    basic_id = _seed(db_session_factory, test_settings)
    data_id = _make_data_available(db_session_factory)
    tok = _login_teacher(client, db_session_factory)

    r = client.get(f"{API}/available", headers=auth_header(tok))
    assert r.status_code == 200, r.text
    options = r.json()
    ids = [o["environment_version_id"] for o in options]
    assert basic_id in ids and data_id in ids
    for opt in options:
        assert "image_digest" not in opt
        assert "image_tag" not in opt
        assert "base_image_ref" not in opt
        assert "manifest_sha256" not in opt
        assert opt["minimum_memory_mb"] > 0
        assert isinstance(opt["packages"], list)


# ═══════════════════════════════════════════════════════════════
# Notebook/Studio：创建、草稿保存、发布
# ═══════════════════════════════════════════════════════════════

def test_studio_create_template_with_environment(client, db_session_factory, test_settings):
    """教师创建 Notebook 模板显式带环境（plan 9.1）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    teacher_id = 1
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id)

    r = client.post(
        f"{STUDIO_API}/templates",
        headers=auth_header(tok),
        json={
            "name": "实验模板",
            "description": "d",
            "lesson_id": lesson_id,
            "environment_version_id": basic_id,
            "import_policy_mode": "restricted",
            "allowed_imports": ["pandas"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["draft_environment_version_id"] == basic_id
    assert body["draft_import_policy_mode"] == "restricted"
    assert body["draft_allowed_imports"] == ["pandas"]


def test_studio_create_template_defaults_to_basic(client, db_session_factory, test_settings):
    """省略环境 → 服务层解析 basic 当前可用版本（兼容既有创建入口）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{STUDIO_API}/templates",
        headers=auth_header(tok),
        json={"name": "T", "lesson_id": lesson_id},
    )
    assert r.status_code == 201, r.text
    assert r.json()["draft_environment_version_id"] == basic_id


def test_studio_create_template_rejects_unavailable(client, db_session_factory, test_settings):
    """模板创建绑定 draft 版本 → 409 VERSION_NOT_AVAILABLE"""
    basic_id = _seed(db_session_factory, test_settings, basic_available=False)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{STUDIO_API}/templates",
        headers=auth_header(tok),
        json={"name": "T", "lesson_id": lesson_id, "environment_version_id": basic_id},
    )
    assert r.status_code == 409, r.text


_CELLS = [
    {"id": "c1", "type": "code", "source": "print(1)", "order": 0, "student_editable": True, "source_hidden": False}
]


def test_studio_save_draft_updates_environment_same_revision(client, db_session_factory, test_settings):
    """草稿保存：环境与 cells 同一 revision 更新（plan 9.1）"""
    basic_id = _seed(db_session_factory, test_settings)
    data_id = _make_data_available(db_session_factory)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{STUDIO_API}/templates",
        headers=auth_header(tok),
        json={"name": "T", "lesson_id": lesson_id, "environment_version_id": basic_id},
    )
    template_id = r.json()["id"]
    assert r.json()["draft_revision"] == 1

    r = client.put(
        f"{STUDIO_API}/templates/{template_id}/draft",
        headers=auth_header(tok),
        json={
            "draft_revision": 1,
            "cells": _CELLS,
            "environment_version_id": data_id,
            "import_policy_mode": "restricted",
            "allowed_imports": ["numpy"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft_revision"] == 2
    assert body["draft_environment_version_id"] == data_id
    assert body["draft_import_policy_mode"] == "restricted"
    assert body["draft_allowed_imports"] == ["numpy"]
    assert len(body["draft_cells"]) == 1

    # 不传环境字段 → 保留草稿已有环境
    r = client.put(
        f"{STUDIO_API}/templates/{template_id}/draft",
        headers=auth_header(tok),
        json={"draft_revision": 2, "cells": _CELLS},
    )
    assert r.status_code == 200, r.text
    assert r.json()["draft_environment_version_id"] == data_id


def test_studio_publish_copies_environment_to_version(client, db_session_factory, test_settings):
    """发布把草稿环境复制到新模板版本，版本环境不可变（plan 9.1）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{STUDIO_API}/templates",
        headers=auth_header(tok),
        json={
            "name": "T", "lesson_id": lesson_id, "environment_version_id": basic_id,
            "import_policy_mode": "restricted", "allowed_imports": ["pandas"],
        },
    )
    template_id = r.json()["id"]
    r = client.put(
        f"{STUDIO_API}/templates/{template_id}/draft",
        headers=auth_header(tok),
        json={"draft_revision": 1, "cells": _CELLS},
    )

    r = client.post(f"{STUDIO_API}/templates/{template_id}/publish", headers=auth_header(tok))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version_number"] == 1
    assert body["environment_version_id"] == basic_id
    assert body["import_policy_mode"] == "restricted"
    assert body["allowed_imports"] == ["pandas"]

    # 历史版本列表同样返回环境字段
    r = client.get(f"{STUDIO_API}/templates/{template_id}/versions", headers=auth_header(tok))
    assert r.status_code == 200, r.text
    assert r.json()[0]["environment_version_id"] == basic_id


def test_studio_publish_blocks_unavailable_draft_environment(client, db_session_factory, test_settings):
    """发布门禁：草稿环境不可用 → 409 VERSION_NOT_AVAILABLE（plan 9.1）"""
    basic_id = _seed(db_session_factory, test_settings, basic_available=False)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    # 直接 ORM 创建绑定 draft 环境的模板（API 创建路径会拒绝 draft 版本）
    with db_session_factory() as db:
        template = NotebookTemplate(
            name="T", owner_id=1, status="draft",
            draft_cells=[], draft_revision=1, draft_metadata={},
            draft_environment_version_id=basic_id,
        )
        db.add(template)
        db.commit()
        template_id = template.id

    r = client.post(f"{STUDIO_API}/templates/{template_id}/publish", headers=auth_header(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "VERSION_NOT_AVAILABLE"


def test_studio_import_create_with_environment(client, db_session_factory, test_settings):
    """导入创建模板同样绑定环境（multipart form，plan 9.1 兼容路径）"""
    basic_id = _seed(db_session_factory, test_settings)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    notebook = json.dumps({
        "cells": [
            {"cell_type": "code", "source": "print(1)", "metadata": {}, "id": "x1"}
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }).encode("utf-8")
    r = client.post(
        f"{STUDIO_API}/templates/import",
        headers=auth_header(tok),
        files={"file": ("t.ipynb", notebook, "application/x-ipynb+json")},
        data={
            "name": "导入模板",
            "lesson_id": str(lesson_id),
            "environment_version_id": str(basic_id),
            "import_policy_mode": "restricted",
            "allowed_imports_json": json.dumps(["numpy"]),
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["draft_environment_version_id"] == basic_id
    assert body["draft_import_policy_mode"] == "restricted"
    assert body["draft_allowed_imports"] == ["numpy"]
    assert len(body["draft_cells"]) == 1


def test_studio_draft_save_rejects_unavailable_environment(client, db_session_factory, test_settings):
    """草稿保存绑定不可用环境 → 409 VERSION_NOT_AVAILABLE"""
    basic_id = _seed(db_session_factory, test_settings)
    bad_id = _make_draft_version(db_session_factory)
    tok = _login_teacher(client, db_session_factory)
    _, lesson_id = _make_course_with_lesson(db_session_factory, teacher_id=1)

    r = client.post(
        f"{STUDIO_API}/templates",
        headers=auth_header(tok),
        json={"name": "T", "lesson_id": lesson_id, "environment_version_id": basic_id},
    )
    template_id = r.json()["id"]
    r = client.put(
        f"{STUDIO_API}/templates/{template_id}/draft",
        headers=auth_header(tok),
        json={"draft_revision": 1, "cells": _CELLS, "environment_version_id": bad_id},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "VERSION_NOT_AVAILABLE"
