"""公告系统测试：模型持久化、权限矩阵、可见性、过期隐藏、幂等已读。"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import auth_header, create_user, grant_teaching_fixture, login


# ── 辅助函数 ───────────────────────────────────────────────────


def _make_course(db_session_factory, teacher, title="测试课程"):
    from app.models import Course

    with db_session_factory() as db:
        course = Course(title=title, status="published", teacher_id=teacher.id)
        db.add(course)
        db.commit()
        db.refresh(course)
        return course


def _enroll(db_session_factory, student, course):
    from app.models import CourseEnrollment

    with db_session_factory() as db:
        if student.role == "student":
            # 正常学生可见性来自管理员授课授权。
            from app.models import Course
            grant_teaching_fixture(db, db.get(Course, course.id), student_ids=[student.id])
        else:
            # 越权回归仍保留异常教师选课投影，证明它不能授予权限。
            db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        db.commit()


def _publish_course_notice(client, teacher_headers, course, **overrides):
    payload = {
        "title": "实验课机房调整",
        "content": "本周实验课调整到 A302。",
        "priority": "important",
        "scope": "course",
        "course_id": course.id if course else None,
    }
    payload.update(overrides)
    return client.post("/api/v1/announcements", json=payload, headers=teacher_headers)


def _publish_global(client, headers, **overrides):
    payload = {
        "title": "平台维护",
        "content": "今晚 22:00 维护。",
        "scope": "global",
    }
    payload.update(overrides)
    return client.post("/api/v1/announcements", json=payload, headers=headers)


# ── 模型层 ─────────────────────────────────────────────────────


def test_announcement_models_persist_read_receipt(db_session_factory):
    from sqlalchemy.exc import IntegrityError

    from app.models import Announcement, AnnouncementRead

    admin = create_user(db_session_factory, "notice-admin", "admin")
    student = create_user(db_session_factory, "notice-student", "student")
    with db_session_factory() as db:
        notice = Announcement(
            title="平台维护",
            content="今晚 22:00 维护。",
            priority="important",
            scope="global",
            author_id=admin.id,
        )
        db.add(notice)
        db.flush()
        db.add(AnnouncementRead(announcement_id=notice.id, user_id=student.id))
        db.commit()
        assert notice.course_id is None
        assert notice.expires_at is None

        # 同一用户对同一公告只能有一条已读记录
        db.add(AnnouncementRead(announcement_id=notice.id, user_id=student.id))
        with pytest.raises(IntegrityError):
            db.commit()


# ── 发布权限矩阵 ───────────────────────────────────────────────


def test_student_cannot_publish(client, db_session_factory):
    student = create_user(db_session_factory, "pub-student", "student")
    teacher = create_user(db_session_factory, "pub-course-owner", "teacher")
    course = _make_course(db_session_factory, teacher)
    token, _ = login(client, "pub-student")
    resp = _publish_course_notice(client, auth_header(token), course)
    assert resp.status_code == 403


def test_teacher_cannot_publish_global(client, db_session_factory):
    teacher = create_user(db_session_factory, "pub-teacher-global", "teacher")
    token, _ = login(client, "pub-teacher-global")
    resp = _publish_global(client, auth_header(token))
    assert resp.status_code == 403


def test_teacher_cannot_publish_other_teachers_course(client, db_session_factory):
    owner = create_user(db_session_factory, "pub-owner", "teacher")
    course = _make_course(db_session_factory, owner)
    other = create_user(db_session_factory, "pub-other", "teacher")
    token, _ = login(client, "pub-other")
    resp = _publish_course_notice(client, auth_header(token), course)
    assert resp.status_code == 403


def test_teacher_can_publish_to_owned_course(client, db_session_factory):
    teacher = create_user(db_session_factory, "pub-owner-ok", "teacher")
    course = _make_course(db_session_factory, teacher)
    token, _ = login(client, "pub-owner-ok")
    resp = _publish_course_notice(client, auth_header(token), course)
    assert resp.status_code == 201
    body = resp.json()
    assert body["scope"] == "course"
    assert body["course_id"] == course.id
    assert body["author_name"] == "pub-owner-ok"
    assert body["title"] == "实验课机房调整"


def test_admin_can_publish_global(client, db_session_factory):
    admin = create_user(db_session_factory, "pub-admin", "admin")
    token, _ = login(client, "pub-admin")
    resp = _publish_global(client, auth_header(token))
    assert resp.status_code == 201
    assert resp.json()["scope"] == "global"


def test_publish_requires_course_id_for_course_scope(client, db_session_factory):
    teacher = create_user(db_session_factory, "pub-no-course", "teacher")
    token, _ = login(client, "pub-no-course")
    resp = _publish_course_notice(client, auth_header(token), None, course_id=None)
    assert resp.status_code == 422


def test_publish_rejects_global_payload_with_course_id(client, db_session_factory):
    admin = create_user(db_session_factory, "pub-global-course", "admin")
    teacher = create_user(db_session_factory, "pub-global-course-owner", "teacher")
    course = _make_course(db_session_factory, teacher)
    token, _ = login(client, "pub-global-course")
    resp = _publish_global(client, auth_header(token), course_id=course.id)
    assert resp.status_code == 422


def test_publish_rejects_naive_expires_at(client, db_session_factory):
    """不带时区的 expires_at 必须以 422 拒绝，而不是触发 500"""
    admin = create_user(db_session_factory, "pub-naive-expiry", "admin")
    token, _ = login(client, "pub-naive-expiry")
    naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    resp = _publish_global(client, auth_header(token), expires_at=naive.isoformat())
    assert resp.status_code == 422


def test_publish_rejects_past_expiry(client, db_session_factory):
    admin = create_user(db_session_factory, "pub-past-expiry", "admin")
    token, _ = login(client, "pub-past-expiry")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    resp = _publish_global(client, auth_header(token), expires_at=past)
    assert resp.status_code == 422


# ── 可见性与幂等已读 ───────────────────────────────────────────


def test_enrolled_student_can_read_course_notice_idempotently(client, db_session_factory):
    teacher = create_user(db_session_factory, "vis-teacher", "teacher")
    course = _make_course(db_session_factory, teacher)
    student = create_user(db_session_factory, "vis-student", "student")
    _enroll(db_session_factory, student, course)
    token, _ = login(client, "vis-teacher")
    resp = _publish_course_notice(client, auth_header(token), course)
    assert resp.status_code == 201
    notice_id = resp.json()["id"]

    student_token, _ = login(client, "vis-student")
    first = client.post(
        f"/api/v1/announcements/{notice_id}/read", headers=auth_header(student_token)
    )
    assert first.status_code == 204
    # 幂等：重复标记仍返回 204
    again = client.post(
        f"/api/v1/announcements/{notice_id}/read", headers=auth_header(student_token)
    )
    assert again.status_code == 204


def test_non_enrolled_student_cannot_read_course_notice(client, db_session_factory):
    teacher = create_user(db_session_factory, "vis-teacher2", "teacher")
    course = _make_course(db_session_factory, teacher)
    stranger = create_user(db_session_factory, "vis-stranger", "student")
    token, _ = login(client, "vis-teacher2")
    resp = _publish_course_notice(client, auth_header(token), course)
    notice_id = resp.json()["id"]

    stranger_token, _ = login(client, "vis-stranger")
    read = client.post(
        f"/api/v1/announcements/{notice_id}/read", headers=auth_header(stranger_token)
    )
    assert read.status_code == 404


def test_teacher_cannot_see_course_notice_via_stray_enrollment(client, db_session_factory):
    """教师即使被异常写入他课 enrollment，也不可见该课程公告（角色分支精确）"""
    owner = create_user(db_session_factory, "leak-owner", "teacher")
    course = _make_course(db_session_factory, owner)
    other = create_user(db_session_factory, "leak-other", "teacher")
    # 异常数据：把其他教师写入课程 enrollment
    _enroll(db_session_factory, other, course)
    token, _ = login(client, "leak-owner")
    resp = _publish_course_notice(client, auth_header(token), course)
    notice_id = resp.json()["id"]

    other_token, _ = login(client, "leak-other")
    read = client.post(
        f"/api/v1/announcements/{notice_id}/read", headers=auth_header(other_token)
    )
    assert read.status_code == 404


def test_teacher_cannot_read_other_teachers_course_notice(client, db_session_factory):
    owner = create_user(db_session_factory, "vis-owner", "teacher")
    course = _make_course(db_session_factory, owner)
    other = create_user(db_session_factory, "vis-other-teacher", "teacher")
    token, _ = login(client, "vis-owner")
    resp = _publish_course_notice(client, auth_header(token), course)
    notice_id = resp.json()["id"]

    other_token, _ = login(client, "vis-other-teacher")
    read = client.post(
        f"/api/v1/announcements/{notice_id}/read", headers=auth_header(other_token)
    )
    assert read.status_code == 404


def test_global_notice_visible_to_student(client, db_session_factory):
    admin = create_user(db_session_factory, "vis-global-admin", "admin")
    student = create_user(db_session_factory, "vis-global-student", "student")
    token, _ = login(client, "vis-global-admin")
    resp = _publish_global(client, auth_header(token))
    notice_id = resp.json()["id"]

    student_token, _ = login(client, "vis-global-student")
    read = client.post(
        f"/api/v1/announcements/{notice_id}/read", headers=auth_header(student_token)
    )
    assert read.status_code == 204


def test_unsupported_persisted_role_cannot_login(client, db_session_factory):
    """历史非法角色不能通过认证进入任何业务流程。"""
    create_user(db_session_factory, "legacy-role-user", "legacy")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "legacy-role-user", "password": "Passw0rd!"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_NOT_SUPPORTED"


def test_expired_notice_hidden(client, db_session_factory):
    from app.models import Announcement

    admin = create_user(db_session_factory, "exp-admin", "admin")
    student = create_user(db_session_factory, "exp-student", "student")
    with db_session_factory() as db:
        notice = Announcement(
            title="已过期公告",
            content="不应可见。",
            scope="global",
            author_id=admin.id,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(notice)
        db.commit()
        db.refresh(notice)
        expired_id = notice.id

    student_token, _ = login(client, "exp-student")
    read = client.post(
        f"/api/v1/announcements/{expired_id}/read", headers=auth_header(student_token)
    )
    assert read.status_code == 404
