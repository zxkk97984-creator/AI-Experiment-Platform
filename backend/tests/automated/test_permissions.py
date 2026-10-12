"""后端权限与错误协议 RED 测试"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import models
from conftest import auth_header, create_assignment_db, create_course_db, create_user, grant_teaching_fixture, login

API = "/api/v1"


def _grant(db_session_factory, cid, student_names=()):
    with db_session_factory() as db:
        students = list(db.scalars(select(models.User.id).where(models.User.username.in_(student_names))))
        offering = grant_teaching_fixture(db, db.get(models.Course, cid), student_ids=students)
        db.commit()
        return offering.id


def _setup_full(client, db_session_factory, course_status="published"):
    """创建教师+两个学生（一个 enrolled）+ 课程+章节+assignment+exam"""
    create_user(db_session_factory, "t_own", "teacher")
    create_user(db_session_factory, "s_yes", "student")
    create_user(db_session_factory, "s_no", "student")
    admin = create_user(db_session_factory, "perms_admin", "admin")
    t_tok, _ = login(client, "t_own")
    s_yes_tok, _ = login(client, "s_yes")
    s_no_tok, _ = login(client, "s_no")
    admin_tok, _ = login(client, admin.username)

    cid = create_course_db(
        db_session_factory,
        teacher_username="t_own",
        title="PermTest",
        status=course_status,
        visibility="public",
    )
    s_yes_id = client.get(f"{API}/auth/me", headers=auth_header(s_yes_tok)).json()["id"]
    with db_session_factory() as db:
        offering = grant_teaching_fixture(db, db.get(models.Course, cid), student_ids=[s_yes_id])
        db.commit()
        offering_id = offering.id
    # 管理员授课授权决定学生范围；草稿课程使用 whitelist_only 检查发布受众。
    audience_payload = {}
    if course_status == "published":
        client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_yes_tok))
    else:
        audience_payload = {
            "audience_mode": "whitelist_only",
            "whitelist_student_ids": [s_yes_id],
        }
    ch = client.post(f"{API}/courses/{cid}/chapters", headers=auth_header(t_tok), json={"title": "Ch"})
    chid = ch.json()["id"]
    le = client.post(f"{API}/chapters/{chid}/lessons", headers=auth_header(t_tok), json={
        "title": "Lesson", "content_type": "markdown", "content": "test", "status": "published",
    })
    a = client.post(f"{API}/assignments", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "A", "status": "draft", **audience_payload,
    })
    aid = a.json()["id"]
    _grant(db_session_factory, cid, ["s_yes"])
    q = client.post(f"{API}/assignments/{aid}/questions", headers=auth_header(t_tok), json={
        "title": "Q", "function_name": "f", "hidden_tests": "SECRET", "grading_mode": "legacy",
    })
    qid = q.json()["id"]
    pub = client.post(f"{API}/assignments/{aid}/publish", headers=auth_header(t_tok))
    assert pub.status_code == 200, pub.text
    now = datetime.now(UTC)
    e = client.post(f"{API}/exams", headers=auth_header(t_tok), json={
        "course_id": cid,
        "title": "Exam",
        "duration_minutes": 30,
        "start_at": (now - timedelta(minutes=5)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
        **audience_payload,
    })
    eid = e.json()["id"]
    _grant(db_session_factory, cid, ["s_yes"])
    # 添加一道选择题满足 validate_publish 要求，然后发布
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice",
        "prompt": "Q1?",
        "options": {"A": "a", "B": "b"},
        "correct_answer": {"correct": ["A"]},
        "points": 1,
    })
    client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok), json={
        "status": "published",
    })
    return {"t_tok": t_tok, "s_yes_tok": s_yes_tok, "s_no_tok": s_no_tok,
            "cid": cid, "chid": chid, "lid": le.json()["id"], "aid": aid, "qid": qid, "eid": eid,
            "s_yes_id": s_yes_id, "admin_tok": admin_tok, "offering_id": offering_id}


# ═══════════════════════════════════════════════════════════════
# 全面资源图测试：enrolled student 成功 → un-enrolled 全拒绝
# ═══════════════════════════════════════════════════════════════

def test_enrolled_student_access_all_succeeds(client, db_session_factory):
    d = _setup_full(client, db_session_factory)
    tok = d["s_yes_tok"]
    # course, chapters, lessons
    for path in [f"/courses/{d['cid']}", f"/courses/{d['cid']}/chapters"]:
        r = client.get(f"{API}{path}", headers=auth_header(tok))
        assert r.status_code == 200, f"enrolled GET {path}: {r.status_code}"
    # assignment + questions
    r = client.get(f"{API}/assignments?course_id={d['cid']}", headers=auth_header(tok))
    assert r.status_code == 200
    r = client.get(f"{API}/assignments/{d['aid']}/questions", headers=auth_header(tok))
    assert r.status_code == 200
    # exam start
    r = client.post(f"{API}/exams/{d['eid']}/start", headers=auth_header(tok))
    assert r.status_code == 201
    # judge submit
    r = client.post(f"{API}/judge/submissions", headers=auth_header(tok), json={
        "question_id": d["qid"], "code": "def f(): pass",
    })
    assert r.status_code == 201
    # course catalog
    r = client.get(f"{API}/courses", headers=auth_header(tok))
    assert r.status_code == 200


def test_unenrolled_student_rejected_all_content(client, db_session_factory):
    d = _setup_full(client, db_session_factory)
    tok = d["s_no_tok"]
    # public 内容属性不能授予未分配学生元数据或内容权限。
    r = client.get(f"{API}/courses/{d['cid']}", headers=auth_header(tok))
    assert r.status_code == 403, f"un-enrolled GET course meta: {r.status_code}"
    r = client.get(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(tok))
    assert r.status_code == 403, f"un-enrolled GET chapters: {r.status_code}"
    r = client.get(f"{API}/assignments?course_id={d['cid']}", headers=auth_header(tok))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 0  # un-enrolled sees empty list
    r = client.get(f"{API}/exams/{d['eid']}", headers=auth_header(tok))
    assert r.status_code == 404
    r = client.post(f"{API}/exams/{d['eid']}/start", headers=auth_header(tok))
    assert r.status_code == 404
    r = client.post(f"{API}/judge/submissions", headers=auth_header(tok), json={
        "question_id": d["qid"], "code": "def f(): pass",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "QUESTION_NOT_FOUND"
    sample = client.post(f"{API}/judge/questions/{d['qid']}/sample-run", headers=auth_header(tok), json={
        "question_id": d["qid"], "code": "def f(): pass",
    })
    assert sample.status_code == 404
    assert sample.json()["detail"]["code"] == "QUESTION_NOT_FOUND"
    with db_session_factory() as db:
        assert db.query(models.Submission).count() == 0
    # 目录同样不泄露未分配课程。
    r = client.get(f"{API}/courses", headers=auth_header(tok))
    assert all(item["id"] != d["cid"] for item in r.json()["items"])


def test_dropped_student_rejected_all(client, db_session_factory):
    d = _setup_full(client, db_session_factory)
    tok = d["s_yes_tok"]
    # 先确认可访问
    r = client.get(f"{API}/courses/{d['cid']}", headers=auth_header(tok))
    assert r.status_code == 200
    # 退课
    revoked = client.delete(f"{API}/courses/{d['cid']}/students/{d['s_yes_id']}", headers=auth_header(d["admin_tok"]))
    assert revoked.status_code == 204
    # 授课授权撤销后元数据与内容权限立即回收。
    r = client.get(f"{API}/courses/{d['cid']}", headers=auth_header(tok))
    assert r.status_code == 403, f"dropped meta: {r.status_code}"
    r = client.get(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(tok))
    assert r.status_code == 403, f"dropped chapters: {r.status_code}"
    r = client.post(f"{API}/judge/submissions", headers=auth_header(tok), json={
        "question_id": d["qid"], "code": "def f(): pass",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "QUESTION_NOT_FOUND"
    with db_session_factory() as db:
        assert db.query(models.Submission).count() == 0


def test_draft_course_rejected(client, db_session_factory):
    d = _setup_full(client, db_session_factory, course_status="draft")
    tok = d["s_yes_tok"]
    r = client.get(f"{API}/courses/{d['cid']}", headers=auth_header(tok))
    assert r.status_code == 403, f"draft course: {r.status_code}"


def test_draft_assignment_rejected(client, db_session_factory):
    d = _setup_full(client, db_session_factory)
    tok = d["s_yes_tok"]
    # 把 assignment 改为 draft（取消发布）
    t_tok = d["t_tok"]
    unpub = client.post(f"{API}/assignments/{d['aid']}/unpublish", headers=auth_header(t_tok))
    assert unpub.status_code == 200, unpub.text
    r = client.get(f"{API}/assignments/{d['aid']}", headers=auth_header(tok))
    assert r.status_code == 403


def test_draft_exam_rejected(client, db_session_factory):
    d = _setup_full(client, db_session_factory)
    tok = d["s_yes_tok"]
    t_tok = d["t_tok"]
    client.patch(f"{API}/exams/{d['eid']}", headers=auth_header(t_tok), json={"status": "draft"})
    r = client.post(f"{API}/exams/{d['eid']}/start", headers=auth_header(tok))
    assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════
# Teacher 跨课程拒绝
# ═══════════════════════════════════════════════════════════════

def test_teacher_b_cannot_manage_teacher_a_resources(client, db_session_factory):
    create_user(db_session_factory, "ta_r", "teacher")
    create_user(db_session_factory, "tb_r", "teacher")
    create_user(db_session_factory, "stu_r", "student")
    ta_tok, _ = login(client, "ta_r")
    tb_tok, _ = login(client, "tb_r")

    cid = create_course_db(db_session_factory, teacher_username="ta_r", title="TA Only", status="published")
    # 为 stu_r 建立选课，作为 ta 课程作业/考试发布的 all_enrolled 受众
    with db_session_factory() as db:
        stu = db.scalar(select(models.User).where(models.User.username == "stu_r"))
        db.add(models.CourseEnrollment(
            course_id=cid,
            student_id=stu.id,
            status="enrolled",
            origin="manual",
        ))
        db.commit()
    _grant(db_session_factory, cid, ["stu_r"])
    ch = client.post(f"{API}/courses/{cid}/chapters", headers=auth_header(ta_tok), json={"title": "Ch"})
    chid = ch.json()["id"]
    le = client.post(f"{API}/chapters/{chid}/lessons", headers=auth_header(ta_tok), json={
        "title": "L", "content_type": "markdown",
    })
    a = client.post(f"{API}/assignments", headers=auth_header(ta_tok), json={
        "course_id": cid, "title": "A", "status": "draft",
    })
    aid = a.json()["id"]
    _grant(db_session_factory, cid, ["stu_r"])
    q = client.post(f"{API}/assignments/{aid}/questions", headers=auth_header(ta_tok), json={
        "title": "Q", "function_name": "f", "hidden_tests": "def test(): pass", "grading_mode": "legacy",
    })
    pub = client.post(f"{API}/assignments/{aid}/publish", headers=auth_header(ta_tok))
    assert pub.status_code == 200, pub.text
    e = client.post(f"{API}/exams", headers=auth_header(ta_tok), json={
        "course_id": cid, "title": "E", "duration_minutes": 30,
    })
    eid = e.json()["id"]
    _grant(db_session_factory, cid, ["stu_r"])
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(ta_tok), json={
        "question_type": "single_choice", "prompt": "Q1?", "options": {"A": "a", "B": "b"},
        "correct_answer": {"correct": ["A"]}, "points": 1,
    })
    client.patch(f"{API}/exams/{eid}", headers=auth_header(ta_tok), json={"status": "published"})

    # tb 全拒绝
    for path in [f"/courses/{cid}", f"/courses/{cid}/chapters"]:
        r = client.get(f"{API}{path}", headers=auth_header(tb_tok))
        assert r.status_code == 403, f"tb GET {path}: {r.status_code}"

    # tb 不能改 ta 的课程
    r = client.patch(f"{API}/courses/{cid}", headers=auth_header(tb_tok), json={"title": "H"})
    assert r.status_code == 403

    # tb 列表不含 ta 的 assignment
    r = client.get(f"{API}/assignments", headers=auth_header(tb_tok))
    ids = [i["id"] for i in r.json().get("items", [])]
    assert aid not in ids

    # tb 列表不含 ta 的 exam
    r = client.get(f"{API}/exams", headers=auth_header(tb_tok))
    ids = [i["id"] for i in r.json().get("items", [])]
    assert eid not in ids

    # tb 不能访问 ta 的 assignment detail
    r = client.get(f"{API}/assignments/{aid}", headers=auth_header(tb_tok))
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════
# 错误协议
# ═══════════════════════════════════════════════════════════════

def test_error_string_http_exception_has_detail_structure(client, db_session_factory):
    """字符串 HTTPException → detail:{code,message,fields}"""
    create_user(db_session_factory, "s_err", "student")
    s_tok, _ = login(client, "s_err")
    r = client.get(f"{API}/courses/9999999", headers=auth_header(s_tok))
    assert r.status_code == 404
    body = r.json()
    assert "detail" in body
    d = body["detail"]
    assert set(d.keys()) == {"code", "message", "fields"}
    assert d["code"] is not None and d["code"] != ""
    assert isinstance(d["fields"], dict)


def test_error_internal_exception_500(client):
    """普通 Exception → 500 INTERNAL_ERROR 三字段 + raise_server_exceptions=False"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import create_app as _create_app

    app = _create_app()
    @app.get("/api/v1/test-runtime-error")
    def _raise():
        raise RuntimeError("boom")

    tc = TestClient(app, raise_server_exceptions=False)
    try:
        r = tc.get("/api/v1/test-runtime-error")
    finally:
        tc.close()
    assert r.status_code == 500
    d = r.json()["detail"]
    assert set(d.keys()) == {"code", "message", "fields"}
    assert d["code"] == "INTERNAL_ERROR"


def test_error_business_detail_not_double_nested(client, db_session_factory):
    """业务 api_error 不出现 detail.detail 双层"""
    r = client.post(f"{API}/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
    body = r.json()
    assert "detail" in body
    assert not isinstance(body["detail"], dict) or "detail" not in body["detail"]


def test_error_unsupported_role_cannot_enter_business_apis(client, db_session_factory):
    """历史非法角色不能进入业务 API；有效角色权限不被影响。"""
    create_user(db_session_factory, "legacy_x2", "legacy")
    create_user(db_session_factory, "t_x2", "teacher")
    create_user(db_session_factory, "s_x2", "student")
    create_user(db_session_factory, "admin_x2", "admin")
    t_tok, _ = login(client, "t_x2")
    s_tok, _ = login(client, "s_x2")
    admin_tok, _ = login(client, "admin_x2")

    cid = create_course_db(
        db_session_factory, teacher_username="t_x2", title="DevBlocked2",
        status="published", visibility="public",
    )
    _grant(db_session_factory, cid, ["s_x2"])
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))
    # 创建真实 assignment 和 exam
    a = client.post(f"{API}/assignments", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "A1", "status": "draft",
    })
    aid = a.json()["id"]
    _grant(db_session_factory, cid, ["s_x2"])
    q = client.post(f"{API}/assignments/{aid}/questions", headers=auth_header(t_tok), json={
        "title": "Q1", "function_name": "f", "hidden_tests": "def test(): pass", "grading_mode": "legacy",
    })
    pub = client.post(f"{API}/assignments/{aid}/publish", headers=auth_header(t_tok))
    assert pub.status_code == 200, pub.text
    now = datetime.now(UTC)
    e = client.post(f"{API}/exams", headers=auth_header(t_tok), json={
        "course_id": cid,
        "title": "E1",
        "duration_minutes": 30,
        "start_at": (now - timedelta(minutes=5)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    })
    eid = e.json()["id"]
    _grant(db_session_factory, cid, ["s_x2"])
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q1?", "options": {"A": "a", "B": "b"},
        "correct_answer": {"correct": ["A"]}, "points": 1,
    })
    client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok), json={"status": "published"})

    # student 能看到
    r = client.get(f"{API}/assignments", headers=auth_header(s_tok))
    assert any(i["id"] == aid for i in r.json()["items"]), "student should see assignment"
    r = client.get(f"{API}/exams", headers=auth_header(s_tok))
    assert any(i["id"] == eid for i in r.json()["items"]), "student should see exam"

    # admin 仍然能看到全部资源，非法角色处理不能误伤管理员
    r = client.get(f"{API}/assignments", headers=auth_header(admin_tok))
    assert any(i["id"] == aid for i in r.json()["items"]), "admin should see assignment"
    r = client.get(f"{API}/exams", headers=auth_header(admin_tok))
    assert any(i["id"] == eid for i in r.json()["items"]), "admin should see exam"

    legacy_login = client.post(
        f"{API}/auth/login",
        json={"username": "legacy_x2", "password": "Passw0rd!"},
    )
    assert legacy_login.status_code == 403
    assert legacy_login.json()["detail"]["code"] == "ROLE_NOT_SUPPORTED"


# ═══════════════════════════════════════════════════════════════
# 补充测试
# ═══════════════════════════════════════════════════════════════

def test_draft_exam_get_rejected_for_enrolled_student(client, db_session_factory):
    """enrolled student GET draft exam → 403"""
    d = _setup_full(client, db_session_factory)
    t_tok = d["t_tok"]
    tok = d["s_yes_tok"]
    client.patch(f"{API}/exams/{d['eid']}", headers=auth_header(t_tok), json={"status": "draft"})
    r = client.get(f"{API}/exams/{d['eid']}", headers=auth_header(tok))
    assert r.status_code == 403, f"draft exam GET: {r.status_code}"


def test_unenrolled_submit_rejected_no_db_side_effect(client, db_session_factory):
    """未分配学生看不到考试（404）且不创建 ExamSubmission/ExamGrade。"""
    d = _setup_full(client, db_session_factory)
    tok = d["s_no_tok"]
    with db_session_factory() as db:
        before_sub = db.query(models.ExamSubmission).count()
        before_grade = db.query(models.ExamGrade).count()
    r = client.post(f"{API}/exams/{d['eid']}/submit", headers=auth_header(tok), json={"expected_write_generation": 1, "score": 100})
    assert r.status_code == 404, f"submit: {r.status_code}"
    with db_session_factory() as db:
        assert db.query(models.ExamSubmission).count() == before_sub
        assert db.query(models.ExamGrade).count() == before_grade


def test_dropped_submit_rejected_no_db_side_effect(client, db_session_factory):
    """授课授权撤销后 submit → 404 且无 DB 副作用。"""
    d = _setup_full(client, db_session_factory)
    tok = d["s_yes_tok"]
    revoked = client.delete(f"{API}/courses/{d['cid']}/students/{d['s_yes_id']}", headers=auth_header(d["admin_tok"]))
    assert revoked.status_code == 204
    with db_session_factory() as db:
        before_sub = db.query(models.ExamSubmission).count()
    r = client.post(f"{API}/exams/{d['eid']}/submit", headers=auth_header(tok), json={"expected_write_generation": 1, "score": 100})
    assert r.status_code == 404, f"dropped submit: {r.status_code}"
    with db_session_factory() as db:
        assert db.query(models.ExamSubmission).count() == before_sub


def test_draft_assignment_questions_rejected(client, db_session_factory):
    """draft assignment 的 GET questions → 403"""
    d = _setup_full(client, db_session_factory)
    t_tok = d["t_tok"]
    tok = d["s_yes_tok"]
    unpub = client.post(f"{API}/assignments/{d['aid']}/unpublish", headers=auth_header(t_tok))
    assert unpub.status_code == 200, unpub.text
    r = client.get(f"{API}/assignments/{d['aid']}/questions", headers=auth_header(tok))
    assert r.status_code == 403


def test_teacher_b_full_mutation_rejection(client, db_session_factory):
    """teacher B 不能 create question、patch/publish assignment、patch exam、view grades、read questions"""
    from app import models as m
    create_user(db_session_factory, "ta_m", "teacher")
    create_user(db_session_factory, "tb_m", "teacher")
    ta_tok, _ = login(client, "ta_m")
    tb_tok, _ = login(client, "tb_m")

    cid = create_course_db(db_session_factory, teacher_username="ta_m", title="TA Mut", status="published")
    aid = create_assignment_db(
        db_session_factory, course_id=cid, teacher_username="ta_m",
        title="A", status="published",
    )
    _grant(db_session_factory, cid)
    e = client.post(f"{API}/exams", headers=auth_header(ta_tok), json={"course_id": cid, "title": "E", "duration_minutes": 30})
    eid = e.json()["id"]
    _grant(db_session_factory, cid)

    # tb 不能 create question
    r = client.post(f"{API}/assignments/{aid}/questions", headers=auth_header(tb_tok), json={
        "title": "Q", "function_name": "f", "hidden_tests": "def test(): pass",
    })
    assert r.status_code == 404, f"create question: {r.status_code}"

    # tb 不能 patch assignment
    r = client.patch(f"{API}/assignments/{aid}", headers=auth_header(tb_tok), json={"title": "H"})
    assert r.status_code == 404, f"patch assignment: {r.status_code}"

    # tb 不能 publish assignment
    r = client.post(f"{API}/assignments/{aid}/publish", headers=auth_header(tb_tok))
    assert r.status_code == 404, f"publish: {r.status_code}"

    # tb 不能 patch exam
    r = client.patch(f"{API}/exams/{eid}", headers=auth_header(tb_tok), json={"title": "H"})
    assert r.status_code == 404, f"patch exam: {r.status_code}"

    # tb 不能 view grades
    r = client.get(f"{API}/exams/{eid}/grades", headers=auth_header(tb_tok))
    assert r.status_code == 404, f"grades: {r.status_code}"

    # tb 不能 read questions
    r = client.get(f"{API}/assignments/{aid}/questions", headers=auth_header(tb_tok))
    assert r.status_code == 404, f"questions: {r.status_code}"
