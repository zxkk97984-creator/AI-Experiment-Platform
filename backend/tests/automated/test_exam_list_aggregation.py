"""考试列表聚合查询测试（TASK-022 / F-25）。

- 教师列表的题数/参与人数/应参加人数/最高分一次批量返回，payload 与逐项计算等价
- 列表 SQL 数不随 N 线性增长：20 场考试下每页 ≤5 次 SQL
- 空数据与多角色回归
"""
from datetime import timedelta

import pytest
import sqlalchemy as sa
from conftest import grant_teaching_fixture, auth_header, create_user, login
from sqlalchemy import select

from app.models import (
    Course,
    CourseEnrollment,
    Exam,
    ExamQuestion,
    ExamSubmission,
    User,
)
from app.services.time_utils import utc_now

API = "/api/v1"

PARTICIPANT_STATUSES = ("submitted", "grading", "graded", "review_required")


@pytest.fixture()
def exam_seed(db_session_factory):
    """创建教师+课程+20 场考试（各 2 题、2 选课、1 参与者）。"""
    teacher = create_user(db_session_factory, "agg-teacher", "teacher")
    students = [
        create_user(db_session_factory, f"agg-s{i}", "student") for i in range(2)
    ]
    now = utc_now()
    with db_session_factory() as db:
        course = Course(
            title="聚合课", description="d", status="published",
            visibility="class", default_score=100, teacher_id=teacher.id,
        )
        db.add(course)
        db.flush()
        for sid in students:
            db.add(CourseEnrollment(course_id=course.id, student_id=sid.id, status="enrolled"))
        db.flush()
        for i in range(20):
            exam = Exam(
                course_id=course.id, title=f"考试{i}", status="published",
                duration_minutes=60, start_at=now - timedelta(hours=2),
                end_at=now + timedelta(hours=2),
            )
            db.add(exam)
            db.flush()
            q1 = ExamQuestion(
                exam_id=exam.id, question_type="fill_blank", prompt=f"填空 [[blank:a]] {i}",
                correct_answer={"blanks": [{"id": "a", "accepted_answers": ["x"]}]},
                points=5, order_index=1,
            )
            q2 = ExamQuestion(
                exam_id=exam.id, question_type="fill_blank", prompt=f"填空 [[blank:b]] {i}",
                correct_answer={"blanks": [{"id": "b", "accepted_answers": ["y"]}]},
                points=7, order_index=2,
            )
            db.add_all([q1, q2])
            db.flush()  # IDs are needed for the frozen points snapshot below.
            if i % 2 == 0:
                db.add(ExamSubmission(
                    exam_id=exam.id, student_id=students[0].id, status="submitted",
                    started_at=now, submitted_at=now,
                    grading_version_map={
                        str(q1.id): {"version_id": None, "points": 5.0},
                        str(q2.id): {"version_id": None, "points": 7.0},
                    },
                ))
        grant_teaching_fixture(db, course, [u.id for u in students])
        db.commit()
        return course.id, teacher.id


def _expected_payload(db_session_factory, exam_id, course_id):
    with db_session_factory() as db:
        question_count = db.scalar(
            select(sa.func.count()).select_from(ExamQuestion).where(ExamQuestion.exam_id == exam_id)
        ) or 0
        participant_count = db.scalar(
            select(sa.func.count()).select_from(ExamSubmission).where(
                ExamSubmission.exam_id == exam_id,
                ExamSubmission.status.in_(PARTICIPANT_STATUSES),
            )
        ) or 0
        expected_count = db.scalar(
            select(sa.func.count()).select_from(CourseEnrollment).where(
                CourseEnrollment.course_id == course_id,
                CourseEnrollment.status == "enrolled",
            )
        ) or 0
        max_score = float(db.scalar(
            select(sa.func.sum(ExamQuestion.points)).where(ExamQuestion.exam_id == exam_id)
        ) or 0)
    return question_count, participant_count, expected_count, max_score


def test_teacher_list_payload_matches_per_item_computation(client, db_session_factory, exam_seed):
    course_id, _ = exam_seed
    token, _ = login(client, "agg-teacher")
    resp = client.get(
        f"{API}/exams", headers=auth_header(token), params={"page": 1, "page_size": 100},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 20
    for item in items:
        qc, pc, ec, ms = _expected_payload(db_session_factory, item["id"], course_id)
        assert item["question_count"] == qc
        assert item["participant_count"] == pc
        assert item["expected_count"] == ec
        assert abs(item["max_score"] - ms) < 1e-6
        assert item["max_score"] == 12.0  # 5 + 7


def test_list_sql_budget_bounded(client, db_session_factory, exam_seed):
    """20 场考试列表 SQL 数 ≤5（不随 N 线性增长）。"""
    token, _ = login(client, "agg-teacher")
    with db_session_factory() as db:
        engine = db.get_bind()
    count = {"n": 0}

    def _counter(*args, **kwargs):
        count["n"] += 1

    sa.event.listen(engine, "before_cursor_execute", _counter)
    try:
        resp = client.get(
            f"{API}/exams", headers=auth_header(token), params={"page": 1, "page_size": 100},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) == 20
    finally:
        sa.event.remove(engine, "before_cursor_execute", _counter)
    assert count["n"] <= 6, f"SQL 次数 {count['n']} 超过预算 6（含一次发布范围缓存 UNION）"


def test_student_list_sql_budget_bounded(client, db_session_factory, exam_seed):
    """学生视图同样使用批量最高分，SQL 数 ≤5。"""
    token, _ = login(client, "agg-s0")
    with db_session_factory() as db:
        engine = db.get_bind()
    count = {"n": 0}

    def _counter(*args, **kwargs):
        count["n"] += 1

    sa.event.listen(engine, "before_cursor_execute", _counter)
    try:
        resp = client.get(
            f"{API}/exams", headers=auth_header(token), params={"page": 1, "page_size": 100},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 20
        # 学生口径：max_score 与教师一致
        assert all(abs(item["max_score"] - 12.0) < 1e-6 for item in items)
    finally:
        sa.event.remove(engine, "before_cursor_execute", _counter)
    assert count["n"] <= 6, f"SQL 次数 {count['n']} 超过预算 6（含一次发布范围缓存 UNION）"


def test_empty_list_ok(client, db_session_factory):
    create_user(db_session_factory, "agg-empty-t", "teacher")
    token, _ = login(client, "agg-empty-t")
    resp = client.get(f"{API}/exams", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


def test_teacher_exam_list_filters_and_sorts_before_pagination(
    client, db_session_factory, exam_seed
):
    course_id, _ = exam_seed
    now = utc_now()
    with db_session_factory() as db:
        exams = db.scalars(select(Exam).order_by(Exam.id)).all()
        exams[0].title = "目标草稿考试"
        exams[0].status = "draft"
        exams[1].title = "排序-A"
        exams[2].title = "排序-Z"
        exams[2].end_at = now - timedelta(minutes=1)
        db.commit()

    token, _ = login(client, "agg-teacher")
    headers = auth_header(token)

    filtered = client.get(
        f"{API}/exams",
        headers=headers,
        params={
            "q": "目标草稿",
            "status": "draft",
            "course_id": course_id,
            "sort": "title_desc",
            "page": 1,
            "page_size": 10,
        },
    )
    assert filtered.status_code == 200, filtered.text
    filtered_body = filtered.json()
    assert filtered_body["total"] == 1
    assert [item["title"] for item in filtered_body["items"]] == ["目标草稿考试"]

    ended = client.get(
        f"{API}/exams",
        headers=headers,
        params={"status": "ended", "page": 1, "page_size": 10},
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["total"] == 1
    assert ended.json()["items"][0]["title"] == "排序-Z"

    sorted_response = client.get(
        f"{API}/exams",
        headers=headers,
        params={
            "q": "排序-",
            "course_id": course_id,
            "sort": "title_desc",
            "page": 1,
            "page_size": 10,
        },
    )
    assert sorted_response.status_code == 200, sorted_response.text
    assert [item["title"] for item in sorted_response.json()["items"]] == [
        "排序-Z",
        "排序-A",
    ]
