from __future__ import annotations
from unittest.mock import patch

from conftest import auth_header, create_assignment_db, create_course_db, create_user, grant_teaching_fixture, login
from app.worker.judge_worker import process_submission


def _grant_student(db_session_factory, course_id):
    from app.models import Course, User
    from sqlalchemy import select
    with db_session_factory() as db:
        sid = db.scalar(select(User.id).where(User.username == "student"))
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[sid])
        db.commit()


def test_course_assignment_submission_and_worker_result(
    client,
    db_session_factory,
    redis_client,
    test_settings,
):
    create_user(db_session_factory, "teacher", "teacher")
    student = create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "teacher")
    student_token, _ = login(client, "student")

    course_id = create_course_db(
        db_session_factory,
        teacher_username="teacher",
        title="机器学习基础",
        description="AI course",
        status="published",
        visibility="public",
    )
    from app.models import Course
    with db_session_factory() as db:
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[student.id])
        db.commit()

    chapter_response = client.post(
        f"/api/v1/courses/{course_id}/chapters",
        headers=auth_header(teacher_token),
        json={"title": "第一章", "order_index": 1},
    )
    assert chapter_response.status_code == 201
    chapter_id = chapter_response.json()["id"]

    lesson_response = client.post(
        f"/api/v1/chapters/{chapter_id}/lessons",
        headers=auth_header(teacher_token),
        json={
            "title": "线性回归",
            "content_type": "markdown",
            "content": "# Linear Regression",
            "order_index": 1,
            "status": "published",
        },
    )
    assert lesson_response.status_code == 201

    enroll_response = client.post(
        f"/api/v1/courses/{course_id}/enroll",
        headers=auth_header(student_token),
    )
    assert enroll_response.status_code == 201

    chapters_response = client.get(
        f"/api/v1/courses/{course_id}/chapters",
        headers=auth_header(student_token),
    )
    assert chapters_response.status_code == 200
    assert chapters_response.json()["items"][0]["lessons"][0]["title"] == "线性回归"

    assignment_id = create_assignment_db(
        db_session_factory,
        course_id=course_id,
        teacher_username="teacher",
        title="函数作业",
        status="draft",
    )
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
            "hidden_tests": "def test_add():\n    assert user_code.add(1, 2) == 3\n    assert user_code.add(-1, 1) == 0\n",
            "grading_mode": "legacy",
            "time_limit_ms": 5000,
            "memory_limit_mb": 256,
        },
    )
    assert question_response.status_code == 201
    question_id = question_response.json()["id"]

    publish_response = client.post(
        f"/api/v1/assignments/{assignment_id}/publish",
        headers=auth_header(teacher_token),
    )
    assert publish_response.status_code == 200, publish_response.text

    submit_response = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={
            "question_id": question_id,
            "code": "def add(a, b):\n    return a + b\n",
        },
    )
    assert submit_response.status_code == 201, submit_response.text
    submission_id = submit_response.json()["id"]
    assert submit_response.json()["status"] == "queued"

    # 验证 DB 状态：enqueue_job 已将 pending→queued
    with db_session_factory() as db:
        from app.models import Submission
        sub = db.get(Submission, submission_id)
        assert sub is not None
        assert sub.grading_status == "queued", f"应为 queued: {sub.grading_status}"

    with db_session_factory() as db:
        settings = test_settings
        settings.judge_use_docker = True
        # mock Docker 子进程调用为本地 pytest
        with patch('app.worker.judge_worker._run_docker_pytest') as mock_docker:
            mock_docker.return_value = ('...', '', 0, 100)
            process_submission(db, redis_client, settings, submission_id)

    result_response = client.get(
        f"/api/v1/judge/submissions/{submission_id}/result",
        headers=auth_header(student_token),
    )
    assert result_response.status_code == 200
    assert result_response.json()["status"] == "accepted"
    assert result_response.json()["score"] == 100


def test_worker_marks_wrong_answer(client, db_session_factory, redis_client, test_settings):
    create_user(db_session_factory, "teacher", "teacher")
    create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "teacher")
    student_token, _ = login(client, "student")

    course_id = create_course_db(
        db_session_factory,
        teacher_username="teacher",
        title="Python",
        status="published",
        visibility="public",
    )
    client.post(f"/api/v1/courses/{course_id}/enroll", headers=auth_header(student_token))
    assignment_id = create_assignment_db(
        db_session_factory, course_id=course_id, teacher_username="teacher",
        title="A1", status="draft",
    )
    _grant_student(db_session_factory, course_id)
    question_id = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        headers=auth_header(teacher_token),
        json={
            "title": "Add",
            "function_name": "add",
            "signature": "def add(a, b)",
            "starter_code": "def add(a, b):\n    return 0\n",
            "public_cases": [],
            "hidden_tests": "def test_add():\n    assert user_code.add(1, 2) == 3\n",
            "grading_mode": "legacy",
        },
    ).json()["id"]
    publish_resp = client.post(
        f"/api/v1/assignments/{assignment_id}/publish", headers=auth_header(teacher_token)
    )
    assert publish_resp.status_code == 200, publish_resp.text
    submission_id = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_id, "code": "def add(a, b):\n    return 0\n"},
    ).json()["id"]

    with db_session_factory() as db:
        settings = test_settings
        settings.judge_use_docker = True
        with patch('app.worker.judge_worker._run_docker_pytest') as mock_docker:
            mock_docker.return_value = ('...', 'assert error', 1, 50)
            process_submission(db, redis_client, settings, submission_id)

    response = client.get(
        f"/api/v1/judge/submissions/{submission_id}/result",
        headers=auth_header(student_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "wrong_answer"
    assert response.json()["score"] == 0


def test_assignment_list_returns_is_submitted_for_student(client, db_session_factory):
    """任务中心数据源：学生作业列表返回 is_submitted（全部题目都有提交才算已交，与 dashboard 待办语义互补）"""
    create_user(db_session_factory, "teacher", "teacher")
    create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "teacher")
    student_token, _ = login(client, "student")

    course_id = create_course_db(
        db_session_factory,
        teacher_username="teacher",
        title="机器学习基础",
        status="published",
        visibility="public",
    )
    client.post(f"/api/v1/courses/{course_id}/enroll", headers=auth_header(student_token))

    assignment_id = create_assignment_db(
        db_session_factory, course_id=course_id, teacher_username="teacher",
        title="函数作业", status="draft",
    )
    _grant_student(db_session_factory, course_id)
    question_ids = []
    for i in range(2):
        resp = client.post(
            f"/api/v1/assignments/{assignment_id}/questions",
            headers=auth_header(teacher_token),
            json={
                "title": f"题目{i + 1}",
                "function_name": "add",
                "hidden_tests": "def test_add():\n    assert user_code.add(1, 2) == 3\n",
                "grading_mode": "legacy",
            },
        )
        assert resp.status_code == 201, resp.text
        question_ids.append(resp.json()["id"])
    publish_resp = client.post(
        f"/api/v1/assignments/{assignment_id}/publish", headers=auth_header(teacher_token)
    )
    assert publish_resp.status_code == 200, publish_resp.text

    def student_items():
        resp = client.get("/api/v1/assignments", headers=auth_header(student_token))
        assert resp.status_code == 200, resp.text
        return resp.json()["items"]

    # 一题未交：整体未提交
    assert student_items()[0]["is_submitted"] is False

    # 只交一题：仍视为未提交（全部题目提交才算已交）
    resp = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_ids[0], "code": "def add(a, b):\n    return a + b\n"},
    )
    assert resp.status_code == 201, resp.text
    assert student_items()[0]["is_submitted"] is False

    # 全部题目已交：已提交
    resp = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_ids[1], "code": "def add(a, b):\n    return a + b\n"},
    )
    assert resp.status_code == 201, resp.text
    assert student_items()[0]["is_submitted"] is True

    # 零题作业永远未提交（API 已禁止零题发布；此处以领域 fixture 覆盖存量数据语义）
    noq_id = create_assignment_db(
        db_session_factory,
        course_id=course_id,
        teacher_username="teacher",
        title="无题作业",
        status="published",
    )
    _grant_student(db_session_factory, course_id)
    by_id = {it["id"]: it["is_submitted"] for it in student_items()}
    assert by_id[noq_id] is False

    # 教师视角不计算学生提交状态：默认 False
    teacher_items = client.get("/api/v1/assignments", headers=auth_header(teacher_token)).json()["items"]
    assert teacher_items[0]["is_submitted"] is False
