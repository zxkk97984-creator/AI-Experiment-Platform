"""TASK-018（F-06）：服务端学习进度。

- 幂等 start/complete/revert；打开课时只记 in_progress，不自动完成
- 完成显式操作、可撤回；跨设备（新客户端）读到同一服务端事实
- 选课鉴权；课程进度聚合（total/completed/percent/next_lesson_id）
"""
from conftest import auth_header, create_user, grant_teaching_fixture, login
from sqlalchemy import select

from app.models import (
    Chapter,
    Course,
    CourseEnrollment,
    Lesson,
    LessonProgress,
)

API = "/api/v1"


def _setup_course(db_session_factory, *, teacher_name="lp-teacher", course_title="进度课"):
    teacher = create_user(db_session_factory, teacher_name, "teacher")
    with db_session_factory() as db:
        course = Course(
            title=course_title, description="d", status="published",
            visibility="public", default_score=100, teacher_id=teacher.id,
        )
        db.add(course)
        db.flush()
        lessons = []
        for i in range(3):
            chapter = Chapter(course_id=course.id, title=f"章{i}", order_index=i)
            db.add(chapter)
            db.flush()
            lesson = Lesson(
                chapter_id=chapter.id, title=f"课时{i}",
                content_type="markdown", content=f"内容 {i}", order_index=0,
                status="published",
            )
            db.add(lesson)
            db.flush()
            lessons.append(lesson)
        db.commit()
        return course.id, [lesson.id for lesson in lessons]


def _enroll(db_session_factory, course_id, student_name):
    student = create_user(db_session_factory, student_name, "student")
    with db_session_factory() as db:
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[student.id])
        db.commit()
        return student.id


def _rows(db_session_factory, lesson_id, student_id):
    with db_session_factory() as db:
        return db.scalars(
            select(LessonProgress).where(
                LessonProgress.lesson_id == lesson_id,
                LessonProgress.student_id == student_id,
            )
        ).all()


def test_start_is_idempotent_single_row(client, db_session_factory):
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-stu")
    token, _ = login(client, "lp-stu")

    r1 = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(token))
    r2 = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(token))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["status"] == "in_progress"

    with db_session_factory() as db:
        student_id = db.scalar(select(CourseEnrollment.student_id).where(CourseEnrollment.course_id == course_id))
    rows = _rows(db_session_factory, lesson_ids[0], student_id)
    assert len(rows) == 1
    assert rows[0].status == "in_progress"


def test_open_does_not_increase_completed(client, db_session_factory):
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-open")
    token, _ = login(client, "lp-open")

    for lid in lesson_ids:
        resp = client.post(f"{API}/lessons/{lid}/progress/start", headers=auth_header(token))
        assert resp.status_code == 200

    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token))
    assert progress.status_code == 200
    body = progress.json()
    assert body["completed"] == 0
    assert body["percent"] == 0
    assert body["next_lesson_id"] == lesson_ids[0]
    assert all(item["status"] == "in_progress" for item in body["items"])


def test_complete_is_explicit_and_idempotent(client, db_session_factory):
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-cpl")
    token, _ = login(client, "lp-cpl")

    r1 = client.post(f"{API}/lessons/{lesson_ids[1]}/progress/complete", headers=auth_header(token))
    r2 = client.post(f"{API}/lessons/{lesson_ids[1]}/progress/complete", headers=auth_header(token))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["status"] == "completed"

    with db_session_factory() as db:
        student_id = db.scalar(select(CourseEnrollment.student_id).where(CourseEnrollment.course_id == course_id))
    assert len(_rows(db_session_factory, lesson_ids[1], student_id)) == 1

    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token)).json()
    assert progress["completed"] == 1
    assert progress["percent"] == 33
    assert progress["next_lesson_id"] == lesson_ids[0]  # 第一个未完成


def test_start_after_complete_keeps_completed(client, db_session_factory):
    """打开已完成的课时不会降级为 in_progress。"""
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-keep")
    token, _ = login(client, "lp-keep")

    client.post(f"{API}/lessons/{lesson_ids[0]}/progress/complete", headers=auth_header(token))
    resp = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token)).json()
    assert progress["completed"] == 1


def test_revert_completed_to_in_progress(client, db_session_factory):
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-revert")
    token, _ = login(client, "lp-revert")

    client.post(f"{API}/lessons/{lesson_ids[2]}/progress/complete", headers=auth_header(token))
    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token)).json()
    assert progress["completed"] == 1

    resp = client.post(f"{API}/lessons/{lesson_ids[2]}/progress/revert", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"
    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token)).json()
    assert progress["completed"] == 0
    assert progress["percent"] == 0

    # revert 幂等：对未开始的课时 revert 也只是 in_progress
    resp = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/revert", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


def test_course_progress_full_completion(client, db_session_factory):
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-full")
    token, _ = login(client, "lp-full")

    for lid in lesson_ids:
        client.post(f"{API}/lessons/{lid}/progress/complete", headers=auth_header(token))
    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token)).json()
    assert progress["total"] == 3
    assert progress["completed"] == 3
    assert progress["percent"] == 100
    assert progress["next_lesson_id"] is None


def test_cross_device_consistency(client, db_session_factory):
    """显式操作后所有设备（新会话 token）读到同一服务端事实。"""
    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-cross")
    token, _ = login(client, "lp-cross")

    client.post(f"{API}/lessons/{lesson_ids[0]}/progress/complete", headers=auth_header(token))

    # 模拟另一台设备：新登录会话（新 token）查询
    token2, _ = login(client, "lp-cross")
    progress = client.get(f"{API}/courses/{course_id}/progress", headers=auth_header(token2)).json()
    assert progress["completed"] == 1
    assert progress["percent"] == 33


def test_enrollment_required(client, db_session_factory):
    """未选课学生 403；教师 403（仅学生可记录）；不存在课时 404。"""
    course_id, lesson_ids = _setup_course(db_session_factory)
    create_user(db_session_factory, "lp-outsider", "student")
    token, _ = login(client, "lp-outsider")
    resp = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(token))
    assert resp.status_code == 403

    create_user(db_session_factory, "lp-teacher2", "teacher")
    ttoken, _ = login(client, "lp-teacher2")
    resp = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(ttoken))
    assert resp.status_code == 403

    _enroll(db_session_factory, course_id, "lp-404")
    stoken, _ = login(client, "lp-404")
    resp = client.post(f"{API}/lessons/999999/progress/start", headers=auth_header(stoken))
    assert resp.status_code == 404


def test_last_accessed_updates_on_open(client, db_session_factory):
    import time

    course_id, lesson_ids = _setup_course(db_session_factory)
    _enroll(db_session_factory, course_id, "lp-touch")
    token, _ = login(client, "lp-touch")

    r1 = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(token)).json()
    time.sleep(0.02)
    r2 = client.post(f"{API}/lessons/{lesson_ids[0]}/progress/start", headers=auth_header(token)).json()
    assert r2["last_accessed_at"] >= r1["last_accessed_at"]
