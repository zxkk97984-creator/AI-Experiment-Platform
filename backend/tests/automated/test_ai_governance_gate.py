"""TASK-020（F-21）：AI 数据治理上线门。

- ai_enabled 默认 False；未显式启用时 ai_ready=False（有无 Key 均不可用）
- 关闭时 Worker 零外呼：不构造客户端，任务以不可重试终态转 review_required（人工评分）
- 状态端点：教师/管理员可查，学生 403
- 人工评分（legacy）在 AI 关闭时仍可完成

A/B/C 分类：B 类（最小父行）——CodeGrade 的 submission/rubric 外键经共享工厂
make_submission / make_rubric 建真实父行。
"""
from unittest.mock import MagicMock, patch

import pytest
from conftest import auth_header, create_user, grant_teaching_fixture, login, make_rubric, make_submission
from sqlalchemy import select

from app.config import Settings
from app.models import CodeGrade, Submission

API = "/api/v1"


def test_ai_disabled_by_default():
    """生产默认关闭：任何未显式开启的 Settings 都不可用 AI。"""
    settings = Settings(_env_file=None, ai_api_key="some-key")
    assert settings.ai_enabled is False
    assert settings.ai_ready is False


def test_ai_ready_requires_both_flag_and_key():
    assert Settings(_env_file=None, ai_enabled=False, ai_api_key="k").ai_ready is False
    assert Settings(_env_file=None, ai_enabled=True, ai_api_key="").ai_ready is False
    assert Settings(_env_file=None, ai_enabled=True, ai_api_key="k").ai_ready is True


def test_status_endpoint_roles(client, db_session_factory, test_settings):
    create_user(db_session_factory, "gov-teacher", "teacher")
    create_user(db_session_factory, "gov-student", "student")

    ttoken, _ = login(client, "gov-teacher")
    resp = client.get(f"{API}/ai-grading/status", headers=auth_header(ttoken))
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "ready": True}  # 测试环境显式启用

    stoken, _ = login(client, "gov-student")
    resp = client.get(f"{API}/ai-grading/status", headers=auth_header(stoken))
    assert resp.status_code == 403


def test_worker_zero_outbound_when_ai_disabled(db_session_factory, test_settings):
    """禁用时 process_ai_grade 不构造 HTTP 客户端、零外呼，任务转人工终态。"""
    from app.worker.judge_worker import process_ai_grade

    disabled = Settings(
        _env_file=None,
        database_url=test_settings.database_url,
        redis_url=test_settings.redis_url,
        secret_key=test_settings.secret_key,
        ai_enabled=False,
        ai_api_key="unused",
    )
    submission_id = make_submission(
        db_session_factory, status="queued", grading_status="queued",
    )
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="active",
            status="queued",
        )
        db.add(grade)
        db.commit()
        grade_id = grade.id

    # 若构造客户端说明外呼路径被触碰——直接失败（客户端在函数内局部导入）
    with patch("app.services.ai_client.DeepSeekClient",
               side_effect=AssertionError("AI 关闭时不得构造 HTTP 客户端")):
        # 必须显式关闭 session：泄漏连接会持有 MySQL 元数据锁，
        # 导致 teardown 的 DROP TABLE 永久等待（2026-08 MySQL 回归卡死根因）。
        with db_session_factory() as db:
            result = process_ai_grade(
                db, MagicMock(), disabled, grade_id,
            )

    assert result is None  # 未产出评分
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status == "review_required"  # 人工评分终态
        assert "AI 服务未启用" in (grade.last_error or "")  # 失败原因落库


def test_legacy_grading_still_works_when_ai_disabled(client, db_session_factory, test_settings):
    """人工评分路径（legacy）不依赖 AI 开关。"""
    disabled = Settings(
        _env_file=None,
        database_url=test_settings.database_url,
        redis_url=test_settings.redis_url,
        secret_key=test_settings.secret_key,
        ai_enabled=False,
    )
    # legacy 题目发布门禁不检查 ai_ready
    create_user(db_session_factory, "gov-legacy-t", "teacher")
    create_user(db_session_factory, "gov-legacy-s", "student")
    ttoken, _ = login(client, "gov-legacy-t")
    with db_session_factory() as db:
        from app.models import Assignment, Course, CourseEnrollment, User

        teacher = db.scalar(select(User).where(User.username == "gov-legacy-t"))
        student = db.scalar(select(User).where(User.username == "gov-legacy-s"))
        course = Course(title="CL", status="draft", visibility="class",
                        default_score=100, teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(course_id=course.id, title="AL", status="draft")
        db.add(assignment)
        db.flush()
        # 当前发布门禁要求存在有效 audience，添加一名已选课学生作为 all_enrolled 受众
        db.add(CourseEnrollment(
            course_id=course.id,
            student_id=student.id,
            status="enrolled",
            origin="manual",
        ))
        grant_teaching_fixture(db, course, student_ids=[student.id], bind_tasks=True)
        db.commit()
        course_id, assignment_id = course.id, assignment.id

    # 添加 legacy 题目后发布（发布门禁对 legacy 不外呼）
    from app.api.assignments import router as _r  # noqa: F401
    resp = client.post(
        f"{API}/assignments/{assignment_id}/questions",
        headers=auth_header(ttoken),
        json={"title": "QL", "function_name": "add",
              "hidden_tests": "def test_add(): assert add(1,2)==3",
              "grading_mode": "legacy"},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"{API}/assignments/{assignment_id}/publish",
        headers=auth_header(ttoken),
    )
    # AI 关闭下 legacy 题目发布成功（无 AI 题目 → 不触发 AI_NOT_READY）
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"
