"""课程可见范围：三种 visibility / 白名单管理 / 学生候选 / 权限隔离回归"""
from __future__ import annotations

from datetime import date, timedelta

from conftest import auth_header, create_course_db, create_user, login
from sqlalchemy import select

from app.models import AcademicTerm, Course, CourseTeachingClass, TeachingClass, TeachingClassStudent, User
from app.services.roster_service import sync_course_class_enrollments
from app.services.time_utils import utc_now

API = "/api/v1"


def _token(client, db_session_factory, username, role="teacher"):
    create_user(db_session_factory, username, role)
    token, _ = login(client, username)
    return token


def _create_course(client, token, title="可见范围课程", status="draft", visibility="private", **extra):
    payload = {"title": title, "description": "desc", "status": status, "visibility": visibility, **extra}
    resp = client.post(f"{API}/courses", headers=auth_header(token), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_students(client, db_session_factory, *names):
    """批量创建 active 学生，返回 {name: token}"""
    tokens = {}
    for name in names:
        tokens[name] = _token(client, db_session_factory, name, "student")
    return tokens


def _enroll(client, token, course_id):
    return client.post(f"{API}/courses/{course_id}/enroll", headers=auth_header(token))


def _chapters(client, token, course_id):
    return client.get(f"{API}/courses/{course_id}/chapters", headers=auth_header(token))


# ═══════════════════════════════════════════════════════════════
# Schema：三种可见范围
# ═══════════════════════════════════════════════════════════════


def test_create_accepts_all_visibilities(client, db_session_factory):
    token = _token(client, db_session_factory, "t-vis")
    for vis in ("private", "class", "public", "whitelist"):
        course = _create_course(client, token, title=f"课程-{vis}", visibility=vis)
        assert course["visibility"] == vis


def test_update_accepts_all_visibilities(client, db_session_factory):
    token = _token(client, db_session_factory, "t-upd")
    course_id = _create_course(client, token)["id"]
    for vis in ("private", "class", "public", "whitelist"):
        resp = client.patch(
            f"{API}/courses/{course_id}", headers=auth_header(token), json={"visibility": vis}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["visibility"] == vis


def test_invalid_visibility_rejected(client, db_session_factory):
    token = _token(client, db_session_factory, "t-inv")
    resp = client.post(
        f"{API}/courses", headers=auth_header(token),
        json={"title": "非法范围", "visibility": "everyone"},
    )
    assert resp.status_code == 422, resp.text
    course_id = _create_course(client, token)["id"]
    resp = client.patch(
        f"{API}/courses/{course_id}", headers=auth_header(token), json={"visibility": "friends"}
    )
    assert resp.status_code == 422, resp.text


def test_visibility_null_rejected(client, db_session_factory):
    token = _token(client, db_session_factory, "t-null")
    course_id = _create_course(client, token)["id"]
    resp = client.patch(
        f"{API}/courses/{course_id}", headers=auth_header(token), json={"visibility": None}
    )
    assert resp.status_code == 422, resp.text


def test_create_defaults_to_class_visibility(client, db_session_factory):
    token = _token(client, db_session_factory, "t-def")
    resp = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "默认教学班可见课程"},
    )
    assert resp.status_code == 201, resp.text
    course = resp.json()
    assert course["visibility"] == "class"


def test_class_visibility_allows_class_members_and_teacher_added_students(client, db_session_factory):
    teacher_token = _token(client, db_session_factory, "t-class-scope")
    member_token = _token(client, db_session_factory, "stu-class-member", "student")
    outsider_token = _token(client, db_session_factory, "stu-class-outsider", "student")

    with db_session_factory() as db:
        member = db.scalar(select(User).where(User.username == "stu-class-member"))
        term = AcademicTerm(
            code="term-class-scope",
            name="教学班可见测试学期",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 1, 31),
            status="active",
        )
        teaching_class = TeachingClass(
            academic_term=term,
            code="CLASS-SCOPE-1",
            name="教学班可见测试班",
            status="active",
        )
        db.add_all([term, teaching_class])
        db.flush()
        db.add(TeachingClassStudent(teaching_class_id=teaching_class.id, student_id=member.id, status="active"))
        db.commit()
        term_id = term.id
        class_id = teaching_class.id

    course_id = create_course_db(
        db_session_factory,
        teacher_username="t-class-scope",
        title="教学班可见课程",
        status="published",
        visibility="class",
        academic_term_id=term_id,
    )
    with db_session_factory() as db:
        db.add(CourseTeachingClass(course_id=course_id, teaching_class_id=class_id))
        db.commit()
        course = db.get(Course, course_id)
        sync_course_class_enrollments(db, course)
        db.commit()

    member_list = client.get(f"{API}/courses", headers=auth_header(member_token))
    assert member_list.status_code == 200, member_list.text
    assert any(item["id"] == course_id for item in member_list.json()["items"])

    outsider_list = client.get(f"{API}/courses", headers=auth_header(outsider_token))
    assert outsider_list.status_code == 200, outsider_list.text
    assert all(item["id"] != course_id for item in outsider_list.json()["items"])

    member_detail = client.get(f"{API}/courses/{course_id}", headers=auth_header(member_token))
    assert member_detail.status_code == 200, member_detail.text
    assert member_detail.json()["is_enrolled"] is True

    outsider_detail = client.get(f"{API}/courses/{course_id}", headers=auth_header(outsider_token))
    assert outsider_detail.status_code == 403, outsider_detail.text
    outsider_enroll = client.post(f"{API}/courses/{course_id}/enroll", headers=auth_header(outsider_token))
    assert outsider_enroll.status_code == 403, outsider_enroll.text

    # 教师可把非本班学生作为例外手动加入；加入后应能发现课程、查看详情和访问课程内容。
    outsider_id = _student_id(client, outsider_token)
    manual_add = client.post(
        f"{API}/courses/{course_id}/students",
        headers=auth_header(teacher_token),
        json={"student_id": outsider_id},
    )
    assert manual_add.status_code == 201, manual_add.text
    assert manual_add.json()["enrollment_origin"] == "manual"

    outsider_list = client.get(f"{API}/courses", headers=auth_header(outsider_token))
    assert any(item["id"] == course_id for item in outsider_list.json()["items"])

    outsider_detail = client.get(f"{API}/courses/{course_id}", headers=auth_header(outsider_token))
    assert outsider_detail.status_code == 200, outsider_detail.text
    assert outsider_detail.json()["is_enrolled"] is True
    assert outsider_detail.json()["enrollment_origin"] == "manual"
    assert _chapters(client, outsider_token, course_id).status_code == 200

    # 教师移除例外学生后立即撤销访问权限。
    manual_remove = client.delete(
        f"{API}/courses/{course_id}/students/{outsider_id}",
        headers=auth_header(teacher_token),
    )
    assert manual_remove.status_code == 204, manual_remove.text
    assert client.get(f"{API}/courses/{course_id}", headers=auth_header(outsider_token)).status_code == 403


# ═══════════════════════════════════════════════════════════════
# 学生候选列表 GET /users/students
# ═══════════════════════════════════════════════════════════════


def _seed_users(db_session_factory):
    """教师/管理员/active学生/disabled学生各一"""
    create_user(db_session_factory, "stu_a", "student", real_name="张三")
    create_user(db_session_factory, "stu_b", "student", real_name="李四")
    create_user(db_session_factory, "stu_off", "student")
    create_user(db_session_factory, "t_other", "teacher")
    # disabled 学生
    with db_session_factory() as db:
        from sqlalchemy import select
        from app.models import User
        u = db.scalar(select(User).where(User.username == "stu_off"))
        u.status = "disabled"
        db.commit()


def test_students_endpoint_teacher_and_admin(client, db_session_factory):
    _seed_users(db_session_factory)
    # t_other 已在 _seed_users 创建
    create_user(db_session_factory, "admin1", "admin")
    for name in ("t_other", "admin1"):
        token, _ = login(client, name)
        resp = client.get(f"{API}/users/students", headers=auth_header(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        usernames = {item["username"] for item in data["items"]}
        assert usernames == {"stu_a", "stu_b"}


def test_students_endpoint_forbidden_for_student(client, db_session_factory):
    stu_tok = _token(client, db_session_factory, "s_only", "student")
    resp = client.get(f"{API}/users/students", headers=auth_header(stu_tok))
    assert resp.status_code == 403, resp.text


def test_students_endpoint_q_filters_username_and_real_name(client, db_session_factory):
    _seed_users(db_session_factory)
    token = _token(client, db_session_factory, "t_q", "teacher")
    resp = client.get(f"{API}/users/students", headers=auth_header(token), params={"q": "张三"})
    assert resp.status_code == 200
    usernames = {item["username"] for item in resp.json()["items"]}
    assert usernames == {"stu_a"}
    resp = client.get(f"{API}/users/students", headers=auth_header(token), params={"q": "stu_b"})
    usernames = {item["username"] for item in resp.json()["items"]}
    assert usernames == {"stu_b"}


def test_students_endpoint_pagination(client, db_session_factory):
    _seed_users(db_session_factory)
    token = _token(client, db_session_factory, "t_pg", "teacher")
    resp = client.get(f"{API}/users/students", headers=auth_header(token), params={"page": 1, "page_size": 1})
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["total"] == 2
    assert data["page_size"] == 1


# ═══════════════════════════════════════════════════════════════
# 白名单管理
# ═══════════════════════════════════════════════════════════════


def _setup_whitelist_course(client, db_session_factory):
    t_tok = _token(client, db_session_factory, "t_wl")
    s_tok = _token(client, db_session_factory, "s_wl", "student")
    cid = create_course_db(
        db_session_factory,
        teacher_username="t_wl",
        title="可见范围课程",
        status="published",
        visibility="whitelist",
    )
    return t_tok, s_tok, cid


def test_owner_crud_whitelist(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)

    # 初始为空
    resp = client.get(f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0

    # 添加
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist",
        headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["course_id"] == course_id
    assert resp.json()["student"]["id"] == s_id

    # 查询可见
    resp = client.get(f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok))
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["student"]["username"] == "s_wl"

    # 删除
    resp = client.delete(f"{API}/courses/{course_id}/whitelist/{s_id}", headers=auth_header(t_tok))
    assert resp.status_code == 204, resp.text
    resp = client.get(f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok))
    assert resp.json()["total"] == 0


def _student_id(client, token):
    resp = client.get(f"{API}/auth/me", headers=auth_header(token))
    return resp.json()["id"]


def test_admin_manages_any_course_whitelist(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)
    admin_tok = _token(client, db_session_factory, "admin_wl", "admin")
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist",
        headers=auth_header(admin_tok),
        json={"student_id": s_id},
    )
    assert resp.status_code == 201, resp.text
    resp = client.delete(f"{API}/courses/{course_id}/whitelist/{s_id}", headers=auth_header(admin_tok))
    assert resp.status_code == 204, resp.text


def test_whitelist_management_forbidden_for_non_owner(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    other_tok = _token(client, db_session_factory, "t_other_wl", "teacher")
    s_id = _student_id(client, s_tok)
    for tok in (other_tok, s_tok):
        resp = client.get(f"{API}/courses/{course_id}/whitelist", headers=auth_header(tok))
        assert resp.status_code == 403, f"GET whitelist: {resp.status_code}"
        resp = client.post(
            f"{API}/courses/{course_id}/whitelist",
            headers=auth_header(tok), json={"student_id": s_id},
        )
        assert resp.status_code == 403, f"POST whitelist: {resp.status_code}"
        resp = client.delete(f"{API}/courses/{course_id}/whitelist/{s_id}", headers=auth_header(tok))
        assert resp.status_code == 403, f"DELETE whitelist: {resp.status_code}"


def test_whitelist_add_validation(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)

    # 用户不存在
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": 999999},
    )
    assert resp.status_code == 404, resp.text

    # 非 student 用户
    other_tok = _token(client, db_session_factory, "t_ns", "teacher")
    other_id = _student_id(client, other_tok)
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": other_id},
    )
    assert resp.status_code == 400, resp.text

    # disabled 学生
    create_user(db_session_factory, "s_disabled_wl", "student")
    with db_session_factory() as db:
        from sqlalchemy import select
        from app.models import User
        u = db.scalar(select(User).where(User.username == "s_disabled_wl"))
        u.status = "disabled"
        db.commit()
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": u.id},
    )
    assert resp.status_code == 400, resp.text

    # 正常添加后再重复 → 409
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "WHITELIST_ENTRY_EXISTS"


def test_concurrent_duplicate_add_returns_409_no_duplicate_row(client, db_session_factory, monkeypatch):
    """预检查通过后并发插入：唯一约束兜底 → 409 + rollback，不产生两行或 500

    模拟竞态窗口：另一请求已插入该行，本请求的预检查因旧快照而“通过”，
    INSERT 触发唯一约束 → IntegrityError 必须转换为 409 而非 500。
    """
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)

    # 已存在一行（模拟另一请求已提交）
    with db_session_factory() as db:
        from app.models import CourseWhitelistStudent
        db.add(CourseWhitelistStudent(course_id=course_id, student_id=s_id))
        db.commit()

    # 模拟并发竞态：预检查看不到已提交行（如同两个事务同时通过预检查）
    monkeypatch.setattr("app.api.courses.is_student_whitelisted", lambda *a, **k: False)

    resp = client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "WHITELIST_ENTRY_EXISTS"

    with db_session_factory() as db:
        from sqlalchemy import func, select
        from app.models import CourseWhitelistStudent
        n = db.scalar(
            select(func.count()).select_from(CourseWhitelistStudent).where(
                CourseWhitelistStudent.course_id == course_id,
                CourseWhitelistStudent.student_id == s_id,
            )
        )
        assert n == 1


def test_whitelist_delete_missing_returns_404(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)
    resp = client.delete(f"{API}/courses/{course_id}/whitelist/{s_id}", headers=auth_header(t_tok))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "WHITELIST_ENTRY_NOT_FOUND"


def test_remove_whitelist_keeps_enrollment(client, db_session_factory):
    """移除白名单不删除/不修改 enrollment"""
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)
    client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    assert _enroll(client, s_tok, course_id).status_code == 201
    resp = client.delete(f"{API}/courses/{course_id}/whitelist/{s_id}", headers=auth_header(t_tok))
    assert resp.status_code == 204

    with db_session_factory() as db:
        from sqlalchemy import select
        from app.models import CourseEnrollment
        enr = db.scalar(select(CourseEnrollment).where(
            CourseEnrollment.course_id == course_id,
            CourseEnrollment.student_id == s_id,
        ))
        assert enr is not None
        assert enr.status == "enrolled"


# ═══════════════════════════════════════════════════════════════
# 可见性矩阵
# ═══════════════════════════════════════════════════════════════


def test_public_course_visible_enroll_then_content(client, db_session_factory):
    t_tok = _token(client, db_session_factory, "t_pub")
    s1 = _token(client, db_session_factory, "s_pub1", "student")
    s2 = _token(client, db_session_factory, "s_pub2", "student")
    cid = create_course_db(db_session_factory, teacher_username="t_pub", title="可见范围课程", status="published", visibility="public")
    _add_chapter_lesson(client, t_tok, cid)

    # 未选学生：list/get 可见、can_enroll=true、chapters 拒绝
    for tok in (s1, s2):
        resp = client.get(f"{API}/courses", headers=auth_header(tok))
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == cid
        assert items[0]["is_enrolled"] is False
        assert items[0]["can_enroll"] is True
        resp = client.get(f"{API}/courses/{cid}", headers=auth_header(tok))
        assert resp.status_code == 200
        assert resp.json()["can_enroll"] is True
        assert _chapters(client, tok, cid).status_code == 403

    # 选课后可读章节
    assert _enroll(client, s1, cid).status_code == 201
    resp = _chapters(client, s1, cid)
    assert resp.status_code == 200
    assert resp.json()["items"][0]["lessons"][0]["title"] == "课时一"
    resp = client.get(f"{API}/courses/{cid}", headers=auth_header(s1))
    assert resp.json()["is_enrolled"] is True
    assert resp.json()["can_enroll"] is False


def _add_chapter_lesson(client, token, course_id, lesson_title="课时一"):
    resp = client.post(
        f"{API}/courses/{course_id}/chapters", headers=auth_header(token),
        json={"title": "第一章", "order_index": 1},
    )
    assert resp.status_code == 201, resp.text
    chapter_id = resp.json()["id"]
    resp = client.post(
        f"{API}/chapters/{chapter_id}/lessons", headers=auth_header(token),
        json={"title": lesson_title, "content_type": "markdown", "content": "# 内容", "order_index": 1},
    )
    assert resp.status_code == 201, resp.text
    return chapter_id


def test_whitelist_member_visibility_and_enroll(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)
    client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    _add_chapter_lesson(client, t_tok, course_id)

    # 名单内学生：list/get 可见、未选课 chapters 拒绝
    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["is_enrolled"] is False
    assert items[0]["can_enroll"] is True
    resp = client.get(f"{API}/courses/{course_id}", headers=auth_header(s_tok))
    assert resp.status_code == 200
    assert _chapters(client, s_tok, course_id).status_code == 403

    # 选课后可读章节
    assert _enroll(client, s_tok, course_id).status_code == 201
    assert _chapters(client, s_tok, course_id).status_code == 200


def test_whitelist_non_member_denied_everywhere(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    outsider = _token(client, db_session_factory, "s_out", "student")
    _add_chapter_lesson(client, t_tok, course_id)

    resp = client.get(f"{API}/courses", headers=auth_header(outsider))
    assert resp.json()["total"] == 0
    resp = client.get(f"{API}/courses/{course_id}", headers=auth_header(outsider))
    assert resp.status_code == 403
    resp = _enroll(client, outsider, course_id)
    assert resp.status_code == 403
    resp = _chapters(client, outsider, course_id)
    assert resp.status_code == 403


def test_removed_from_whitelist_loses_access_immediately(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    s_id = _student_id(client, s_tok)
    client.post(
        f"{API}/courses/{course_id}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    assert _enroll(client, s_tok, course_id).status_code == 201

    resp = client.delete(f"{API}/courses/{course_id}/whitelist/{s_id}", headers=auth_header(t_tok))
    assert resp.status_code == 204

    # 已选学生被移出名单后立即失去目录、详情与内容权限
    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    assert resp.json()["total"] == 0
    resp = client.get(f"{API}/courses/{course_id}", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = _chapters(client, s_tok, course_id)
    assert resp.status_code == 403


def test_empty_whitelist_hidden_from_all_students(client, db_session_factory):
    t_tok, s_tok, course_id = _setup_whitelist_course(client, db_session_factory)
    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    assert resp.json()["total"] == 0
    assert client.get(f"{API}/courses/{course_id}", headers=auth_header(s_tok)).status_code == 403


def test_private_enrolled_student_keeps_access(client, db_session_factory):
    """存量 private 课程的已选学生继续访问（选课后改为 private 模拟存量课程）"""
    t_tok = _token(client, db_session_factory, "t_priv")
    s_tok = _token(client, db_session_factory, "s_priv", "student")
    cid = create_course_db(db_session_factory, teacher_username="t_priv", title="可见范围课程", status="published", visibility="public")
    _add_chapter_lesson(client, t_tok, cid)
    assert _enroll(client, s_tok, cid).status_code == 201
    resp = client.patch(
        f"{API}/courses/{cid}", headers=auth_header(t_tok),
        json={"visibility": "private"},
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    assert len(resp.json()["items"]) == 1
    resp = client.get(f"{API}/courses/{cid}", headers=auth_header(s_tok))
    assert resp.status_code == 200
    assert _chapters(client, s_tok, cid).status_code == 200


def test_private_dropped_student_cannot_use_history_to_reenroll(client, db_session_factory):
    """历史选课记录不授予重新进入私有课程的权限。"""
    t_tok = _token(client, db_session_factory, "t_prd")
    s_tok = _token(client, db_session_factory, "s_prd", "student")
    cid = create_course_db(db_session_factory, teacher_username="t_prd", title="可见范围课程", status="published", visibility="public")
    assert _enroll(client, s_tok, cid).status_code == 201
    resp = client.patch(
        f"{API}/courses/{cid}", headers=auth_header(t_tok),
        json={"visibility": "private"},
    )
    assert resp.status_code == 200, resp.text
    assert client.delete(
        f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok)
    ).status_code == 204
    # 退课后 private 课程从列表消失
    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    assert resp.json()["total"] == 0
    # Direct requests cannot bypass the current authorization decision.
    resp = _enroll(client, s_tok, cid)
    assert resp.status_code == 403
    assert _chapters(client, s_tok, cid).status_code == 403


def test_private_never_enrolled_student_denied(client, db_session_factory):
    t_tok = _token(client, db_session_factory, "t_pne")
    s_tok = _token(client, db_session_factory, "s_pne", "student")
    cid = create_course_db(db_session_factory, teacher_username="t_pne", title="可见范围课程", status="published", visibility="private")

    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    assert resp.json()["total"] == 0
    resp = client.get(f"{API}/courses/{cid}", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = _enroll(client, s_tok, cid)
    assert resp.status_code == 403


def test_draft_archived_hidden_from_students(client, db_session_factory):
    t_tok = _token(client, db_session_factory, "t_dar")
    s_tok = _token(client, db_session_factory, "s_dar", "student")
    for status in ("draft", "archived"):
        cid = create_course_db(db_session_factory, teacher_username="t_dar", title="可见范围课程", status=status, visibility="public")
        resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
        assert resp.json()["total"] == 0
        assert client.get(f"{API}/courses/{cid}", headers=auth_header(s_tok)).status_code == 403
        assert _enroll(client, s_tok, cid).status_code == 400


def test_owner_admin_access_draft_archived_any_visibility(client, db_session_factory):
    t_tok = _token(client, db_session_factory, "t_own2")
    admin_tok = _token(client, db_session_factory, "admin2", "admin")
    for status in ("draft", "archived", "published"):
        for vis in ("private", "public", "whitelist"):
            cid = create_course_db(db_session_factory, teacher_username="t_own2", title="可见范围课程", status=status, visibility=vis)
            for tok in (t_tok, admin_tok):
                resp = client.get(f"{API}/courses/{cid}", headers=auth_header(tok))
                assert resp.status_code == 200, f"{status}/{vis} for {tok[:8]}"
                assert _chapters(client, tok, cid).status_code == 200


def test_other_teacher_cannot_access_course(client, db_session_factory):
    t_tok = _token(client, db_session_factory, "t_ota")
    other_tok = _token(client, db_session_factory, "t_otb", "teacher")
    cid = create_course_db(db_session_factory, teacher_username="t_ota", title="可见范围课程", status="published", visibility="public")
    resp = client.get(f"{API}/courses/{cid}", headers=auth_header(other_tok))
    assert resp.status_code == 403
    resp = client.get(f"{API}/courses", headers=auth_header(other_tok))
    assert resp.json()["total"] == 0


def test_unenrolled_student_keeps_private_course_list_empty(client, db_session_factory):
    student_tok = _token(client, db_session_factory, "s_no_access", "student")
    create_course_db(
        db_session_factory,
        teacher_username="t_private_owner",
        title="可见范围课程",
        status="published",
        visibility="private",
    )
    resp = client.get(f"{API}/courses", headers=auth_header(student_tok))
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_student_list_total_matches_filtered_no_duplicates(client, db_session_factory):
    """混合可见范围：total 与过滤结果一致、无重复行"""
    t_tok = _token(client, db_session_factory, "t_mix")
    s_tok = _token(client, db_session_factory, "s_mix", "student")
    s_id = _student_id(client, s_tok)
    pub = create_course_db(db_session_factory, teacher_username="t_mix", title="公开课", status="published", visibility="public")
    wl = create_course_db(db_session_factory, teacher_username="t_mix", title="白名单课", status="published", visibility="whitelist")
    create_course_db(db_session_factory, teacher_username="t_mix", title="私密课", status="published", visibility="private")
    client.post(
        f"{API}/courses/{wl}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )
    _enroll(client, s_tok, pub)

    resp = client.get(f"{API}/courses", headers=auth_header(s_tok))
    data = resp.json()
    assert data["total"] == 2
    ids = [item["id"] for item in data["items"]]
    assert len(ids) == len(set(ids)) == 2
    by_id = {item["id"]: item for item in data["items"]}
    assert by_id[pub]["is_enrolled"] is True
    assert by_id[pub]["can_enroll"] is False
    assert by_id[wl]["is_enrolled"] is False
    assert by_id[wl]["can_enroll"] is True

    # 分页一页一条，total 不变
    resp = client.get(f"{API}/courses", headers=auth_header(s_tok), params={"page": 1, "page_size": 1})
    assert resp.json()["total"] == 2
    assert len(resp.json()["items"]) == 1


# ═══════════════════════════════════════════════════════════════
# 权限隔离：浏览元数据 ≠ 参加活动
# ═══════════════════════════════════════════════════════════════


def _setup_activity_course(client, db_session_factory, visibility):
    """published 课程 + 章节 + published 作业（含题目）+ published 考试（含选择题）"""
    t_tok = _token(client, db_session_factory, "t_act")
    cid = create_course_db(db_session_factory, teacher_username="t_act", title="可见范围课程", status="published", visibility=visibility)
    _add_chapter_lesson(client, t_tok, cid)

    # 当前后端发布作业/考试要求存在有效 audience。
    # public 课程：创建一个已选课成员作为 all_enrolled 受众；
    # whitelist 课程：创建一个白名单成员并让作业/考试按 whitelist_only 发布。
    member_tok = _token(client, db_session_factory, "s_act_member", "student")
    member_id = _student_id(client, member_tok)
    audience_payload = {}
    if visibility == "public":
        resp = _enroll(client, member_tok, cid)
        assert resp.status_code == 201, resp.text
    elif visibility == "whitelist":
        resp = client.post(
            f"{API}/courses/{cid}/whitelist", headers=auth_header(t_tok),
            json={"student_id": member_id},
        )
        assert resp.status_code == 201, resp.text
        audience_payload = {
            "audience_mode": "whitelist_only",
            "whitelist_student_ids": [member_id],
        }

    resp = client.post(
        f"{API}/assignments", headers=auth_header(t_tok),
        json={"course_id": cid, "title": "作业", "status": "draft", **audience_payload},
    )
    aid = resp.json()["id"]
    resp = client.post(
        f"{API}/assignments/{aid}/questions", headers=auth_header(t_tok),
        json={"title": "题", "function_name": "f", "hidden_tests": "SECRET", "grading_mode": "legacy"},
    )
    qid = resp.json()["id"]
    pub = client.post(f"{API}/assignments/{aid}/publish", headers=auth_header(t_tok))
    assert pub.status_code == 200, pub.text

    now = utc_now()
    resp = client.post(
        f"{API}/exams", headers=auth_header(t_tok),
        json={
            "course_id": cid, "title": "考试", "duration_minutes": 30,
            "start_at": (now - timedelta(minutes=5)).isoformat(),
            "end_at": (now + timedelta(minutes=30)).isoformat(),
            **audience_payload,
        },
    )
    eid = resp.json()["id"]
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q?", "options": {"A": "a", "B": "b"},
        "correct_answer": {"correct": ["A"]}, "points": 1,
    })
    resp = client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok), json={"status": "published"})
    assert resp.status_code == 200, resp.text
    return t_tok, cid, aid, qid, eid


def test_public_not_enrolled_cannot_participate(client, db_session_factory):
    t_tok, cid, aid, qid, eid = _setup_activity_course(client, db_session_factory, "public")
    s_tok = _token(client, db_session_factory, "s_pne2", "student")

    # 作业：可看列表，但不能看题目
    resp = client.get(f"{API}/assignments?course_id={cid}", headers=auth_header(s_tok))
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 0
    resp = client.get(f"{API}/assignments/{aid}", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = client.get(f"{API}/assignments/{aid}/questions", headers=auth_header(s_tok))
    assert resp.status_code == 403

    # 考试：不能开始/提交/看题目
    resp = client.get(f"{API}/exams/{eid}", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = client.post(f"{API}/exams/{eid}/start", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = client.post(f"{API}/exams/{eid}/submit", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = client.get(f"{API}/exams/{eid}/questions", headers=auth_header(s_tok))
    assert resp.status_code == 403

    # Judge：不能提交代码
    resp = client.post(f"{API}/judge/submissions", headers=auth_header(s_tok), json={
        "question_id": qid, "code": "def f(): pass",
    })
    assert resp.status_code == 403


def test_whitelist_member_not_enrolled_cannot_participate(client, db_session_factory):
    t_tok, cid, aid, qid, eid = _setup_activity_course(client, db_session_factory, "whitelist")
    s_tok = _token(client, db_session_factory, "s_wn2", "student")
    s_id = _student_id(client, s_tok)
    client.post(
        f"{API}/courses/{cid}/whitelist", headers=auth_header(t_tok),
        json={"student_id": s_id},
    )

    # 可见元数据（list/get 200），但未选课不能参加活动
    resp = client.get(f"{API}/courses/{cid}", headers=auth_header(s_tok))
    assert resp.status_code == 200
    resp = client.get(f"{API}/assignments/{aid}/questions", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = client.post(f"{API}/exams/{eid}/start", headers=auth_header(s_tok))
    assert resp.status_code == 403
    resp = client.post(f"{API}/judge/submissions", headers=auth_header(s_tok), json={
        "question_id": qid, "code": "def f(): pass",
    })
    assert resp.status_code == 403
