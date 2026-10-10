"""Task 2A：平台自身故障（system_error）不消耗学生的 max_attempts 名额。

判题基础设施故障——Docker 不可用、环境镜像缺失、结果不可验证——属于平台责任，
不得让学生付出提交次数。仅统计真正产生判题结论的提交。
"""
from __future__ import annotations

from conftest import (
    auth_header,
    create_assignment_db,
    create_course_db,
    create_user,
    grant_teaching_fixture,
    login,
)


def _setup_question(client, db_session_factory, *, max_attempts=1):
    """建课程/作业/题目并发布，返回 (student_token, question_id)。"""
    create_user(db_session_factory, "teacher", "teacher")
    student = create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "teacher")
    student_token, _ = login(client, "student")

    course_id = create_course_db(
        db_session_factory,
        teacher_username="teacher",
        title="机器学习基础",
        status="published",
        visibility="public",
    )

    assignment_id = create_assignment_db(
        db_session_factory, course_id=course_id, status="draft",
    )
    # 必须在作业创建之后再授权：grant_teaching_fixture 只会把 offering_id 为空的
    # 作业/考试绑定到授课实例，而发布门禁从授课实例解析受众（否则 AUDIENCE_EMPTY）。
    from app.models import Course
    with db_session_factory() as db:
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[student.id])
        db.commit()

    question_response = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        headers=auth_header(teacher_token),
        json={
            "title": "两数相加",
            "description": "实现 add",
            "function_name": "add",
            "signature": "def add(a: int, b: int) -> int",
            "starter_code": "def add(a, b):\n    return 0\n",
            "public_cases": [{"args": [1, 2], "expected": 3}],
            "hidden_tests": "def test_add():\n    assert user_code.add(1, 2) == 3\n",
            "grading_mode": "legacy",
            "time_limit_ms": 5000,
            "memory_limit_mb": 256,
        },
    )
    assert question_response.status_code == 201, question_response.text
    question_id = question_response.json()["id"]

    # max_attempts 目前没有 API 写入路径（仅模型字段），直接落库设置
    from app.models import JudgeQuestion
    with db_session_factory() as db:
        db.get(JudgeQuestion, question_id).max_attempts = max_attempts
        db.commit()

    publish_response = client.post(
        f"/api/v1/assignments/{assignment_id}/publish",
        headers=auth_header(teacher_token),
    )
    assert publish_response.status_code == 200, publish_response.text
    return student_token, question_id


def _insert_submission(db_session_factory, *, question_id, grading_status, score=None):
    from app.models import Submission, User
    from sqlalchemy import select
    with db_session_factory() as db:
        student_id = db.scalar(select(User.id).where(User.username == "student"))
        row = Submission(
            question_id=question_id,
            student_id=student_id,
            code="def add(a, b):\n    return a + b\n",
            status="system_error" if grading_status == "system_error" else "accepted",
            grading_status=grading_status,
            attempt_count=1,
            score=score,
        )
        db.add(row)
        db.commit()
        return row.id


def test_system_error_submission_does_not_consume_attempt(client, db_session_factory, redis_client):
    """已有一次 system_error 提交后，学生仍可再提交（次数未被消耗）。"""
    student_token, question_id = _setup_question(client, db_session_factory, max_attempts=1)
    _insert_submission(db_session_factory, question_id=question_id,
                       grading_status="system_error", score=None)

    response = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_id, "code": "def add(a, b):\n    return a + b\n"},
    )

    assert response.status_code == 201, response.text


def test_completed_submission_consumes_attempt(client, db_session_factory, redis_client):
    """已产生判题结论的提交仍正常占用次数（不得把退款做成放宽限制）。"""
    student_token, question_id = _setup_question(client, db_session_factory, max_attempts=1)
    _insert_submission(db_session_factory, question_id=question_id,
                       grading_status="completed", score=100.0)

    response = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_id, "code": "def add(a, b):\n    return a + b\n"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "MAX_ATTEMPTS_REACHED"


def test_queued_submission_consumes_attempt(client, db_session_factory, redis_client):
    """排队中/判题中的提交同样占用次数（学生主动重交不受影响）。"""
    student_token, question_id = _setup_question(client, db_session_factory, max_attempts=1)
    _insert_submission(db_session_factory, question_id=question_id, grading_status="queued")

    response = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_id, "code": "def add(a, b):\n    return a + b\n"},
    )

    assert response.status_code == 400, response.text
