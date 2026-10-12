"""考试系统测试"""
import datetime, pytest
from datetime import timezone, timedelta
from conftest import auth_header, create_course_db, create_user, login
from teaching_test_support import grant_course_students
from conftest import grant_teaching_fixture
API = "/api/v1"

def _h(token): return auth_header(token)

def _setup(client, db_session_factory):
    create_user(db_session_factory, "e_t", "teacher")
    create_user(db_session_factory, "e_s", "student")
    t_tok, _ = login(client, "e_t")
    s_tok, _ = login(client, "e_s")
    cid = create_course_db(db_session_factory, teacher_username="e_t", title="C", status="published", visibility="public")
    offering_id = grant_course_students(db_session_factory, cid, ["e_s"])
    now = datetime.datetime.now(timezone.utc)
    e = client.post(f"{API}/exams", headers=_h(t_tok), json={"course_id":cid,"offering_id":offering_id,"title":"E","duration_minutes":60,"start_at":(now-timedelta(hours=1)).isoformat(),"end_at":(now+timedelta(hours=1)).isoformat()})
    eid = e.json()["id"]
    grant_course_students(db_session_factory, cid, ["e_s"])
    q1 = client.post(f"{API}/exams/{eid}/questions", headers=_h(t_tok), json={"question_type":"single_choice","prompt":"Q1","correct_answer":{"correct":["A"]},"points":10,"order_index":0,"options":{"A":"选项A","B":"选项B"}})
    assert q1.status_code == 201, q1.text
    q2 = client.post(f"{API}/exams/{eid}/questions", headers=_h(t_tok), json={"question_type":"code","prompt":"Q2","points":20,"order_index":1,"hidden_tests":"assert True","correct_answer":{},"grading_mode":"legacy"})
    assert q2.status_code == 201, q2.text
    return {"t_tok":t_tok,"s_tok":s_tok,"cid":cid,"offering_id":offering_id,"eid":eid,"q1_id":q1.json()["id"],"q2_id":q2.json()["id"]}

def test_question_crud(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    r = client.get(f"{API}/exams/{ctx['eid']}/questions", headers=_h(ctx['t_tok']))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2
    teacher_choice = r.json()["items"][0]
    teacher_code = r.json()["items"][1]
    assert teacher_choice["correct_answer"] == {"correct": ["A"]}
    assert teacher_code["hidden_tests"] == "assert True"


def test_student_question_response_never_leaks_private_fields(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))

    r = client.get(f"{API}/exams/{ctx['eid']}/questions", headers=_h(ctx['s_tok']))
    assert r.status_code == 200
    for question in r.json()["items"]:
        assert "correct_answer" not in question
        assert "hidden_tests" not in question
        assert "reference_solution" not in question
        assert "test_groups" not in question

def test_publish_locks_questions(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status":"published"})
    r = client.post(f"{API}/exams/{ctx['eid']}/questions", headers=_h(ctx['t_tok']), json={"question_type":"single_choice","prompt":"L","correct_answer":{"correct":["A"]},"points":5})
    assert r.status_code == 403

def test_start_and_submit(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status":"published"})
    r = client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    assert r.status_code == 201
    r = client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']), json={"selected_options":["A"]})
    assert r.status_code == 201
    r = client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    assert r.status_code == 201
    r = client.get(f"{API}/exams/{ctx['eid']}/my-grade", headers=_h(ctx['s_tok']))
    assert r.status_code == 200
    assert r.json()["status"] in ("graded","grading")

def test_my_grade_does_not_leak_system_error(client, db_session_factory):
    """P1: my-grade 不向学生泄露原始 system_error（内部错误/配置细节）"""
    from app.models import ExamAnswer, ExamSubmission
    from sqlalchemy import select
    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status":"published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q2_id']}", headers=_h(ctx['s_tok']), json={"code_answer":"def f(): pass"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))

    # 手动把答案置为 system_error（模拟内部错误）
    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        ans = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id,
            ExamAnswer.question_id == ctx["q2_id"],
        ))
        ans.grading_status = "system_error"
        ans.score = None
        ans.last_error = "Docker 内部路径 /tmp/secret-key /var/run/xxx 堆栈"
        ans.system_error = "Docker 内部路径 /tmp/secret-key /var/run/xxx 堆栈"
        db.commit()

    r = client.get(f"{API}/exams/{ctx['eid']}/my-grade", headers=_h(ctx['s_tok']))
    assert r.status_code == 200
    body = r.text
    # 不泄露内部错误原文、路径、堆栈
    assert "Docker" not in body, "不得泄露内部错误原文"
    assert "/tmp/" not in body, "不得泄露内部路径"
    assert "secret-key" not in body, "不得泄露疑似密钥"
    assert "Traceback" not in body, "不得泄露堆栈"
    # 只暴露通用状态
    answers = r.json()["answers"]
    sys_ans = next(a for a in answers if a["question_id"] == ctx["q2_id"])
    assert sys_ans["grading_status"] == "system_error"
    assert "评分遇到系统问题" in (sys_ans.get("system_error") or ""), \
        "应返回安全通用状态而非原始错误"


def test_teacher_grades(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status":"published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']), json={"selected_options":["A"]})
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q2_id']}", headers=_h(ctx['s_tok']), json={"code_answer":"def answer(): return True"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    r = client.get(f"{API}/exams/{ctx['eid']}/grades", headers=_h(ctx['t_tok']))
    assert r.status_code == 200
    body = r.json()
    assert body["exam"]["title"] == "E"
    assert body["exam"]["question_count"] == 2
    assert body["summary"]["expected_count"] == 1
    assert body["summary"]["submitted_count"] == 1
    assert len(body["distribution"]) == 5
    assert body["items"][0]["student_name"]
    assert body["items"][0]["student_number"] == "e_s"
    assert body["items"][0]["submission_id"] is not None

    detail = client.get(
        f"{API}/exams/{ctx['eid']}/grades/{body['items'][0]['submission_id']}",
        headers=_h(ctx['t_tok']),
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["student"]["number"] == "e_s"
    assert len(detail_body["answers"]) == 2
    assert all("correct_answer" not in answer for answer in detail_body["answers"])
    assert all("hidden_tests" not in answer for answer in detail_body["answers"])

    listing = client.get(f"{API}/exams", headers=_h(ctx['t_tok']))
    listed_exam = next(item for item in listing.json()["items"] if item["id"] == ctx["eid"])
    assert listed_exam["course_title"] == "C"
    assert listed_exam["question_count"] == 2
    assert listed_exam["participant_count"] == 1


def test_teacher_grades_use_server_pagination_and_search(client, db_session_factory):
    from app.models import CourseEnrollment

    ctx = _setup(client, db_session_factory)
    extra = create_user(db_session_factory, "e_extra", "student", real_name="额外学生")
    with db_session_factory() as db:
        from app.models import Course
        grant_teaching_fixture(db, db.get(Course, ctx["cid"]), [extra.id])
        db.commit()

    response = client.get(
        f"{API}/exams/{ctx['eid']}/grades",
        headers=_h(ctx["t_tok"]),
        params={"page": 2, "page_size": 1, "sort": "name"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 2
    assert body["page_size"] == 1
    assert body["total"] == 2
    assert len(body["items"]) == 1

    searched = client.get(
        f"{API}/exams/{ctx['eid']}/grades",
        headers=_h(ctx["t_tok"]),
        params={"q": "额外", "page": 1, "page_size": 20},
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["total"] == 1
    assert [item["student_name"] for item in searched.json()["items"]] == ["额外学生"]


@pytest.mark.parametrize(
    ("status_filter", "start_delta", "end_delta"),
    [
        ("scheduled", timedelta(hours=1), timedelta(hours=2)),
        ("ready", timedelta(hours=-1), timedelta(hours=1)),
        ("missed", timedelta(hours=-2), timedelta(hours=-1)),
    ],
)
def test_teacher_grades_status_filters_join_the_requested_exam(
    client, db_session_factory, status_filter, start_delta, end_delta
):
    """缺考/待考筛选只能基于当前 exam，不能被其他考试的时间行放大。"""
    from app.models import Exam

    ctx = _setup(client, db_session_factory)
    now = datetime.datetime.now(timezone.utc)
    start_at = now + start_delta
    end_at = now + end_delta
    with db_session_factory() as db:
        exam = db.get(Exam, ctx["eid"])
        exam.start_at = start_at
        exam.end_at = end_at
        # 旧实现未连接 Exam，会把这个同类时间行也笛卡尔拼进结果。
        db.add(Exam(
            course_id=ctx["cid"],
            title=f"noise-{status_filter}",
            status="draft",
            duration_minutes=60,
            start_at=start_at,
            end_at=end_at,
        ))
        db.commit()

    response = client.get(
        f"{API}/exams/{ctx['eid']}/grades",
        headers=_h(ctx["t_tok"]),
        params={"status": status_filter, "page": 1, "page_size": 20},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == status_filter


def test_teacher_grades_summary_aggregates_without_materializing_all_students(
    client, db_session_factory
):
    """摘要应由聚合 SQL 计算，成绩页只物化请求页的学生行。"""
    from sqlalchemy import event

    from app.models import CourseEnrollment, ExamSubmission

    ctx = _setup(client, db_session_factory)
    extra_student_ids = []
    for index in range(8):
        extra = create_user(
            db_session_factory,
            f"e_bulk_{index}",
            "student",
            real_name=f"批量学生{index}",
        )
        extra_student_ids.append(extra.id)
        with db_session_factory() as db:
            from app.models import Course
            grant_teaching_fixture(db, db.get(Course, ctx["cid"]), [extra.id])
            db.commit()

    with db_session_factory() as db:
        db.add_all([
            ExamSubmission(
                exam_id=ctx["eid"], student_id=extra_student_ids[0],
                status="graded", score=95,
            ),
            ExamSubmission(
                exam_id=ctx["eid"], student_id=extra_student_ids[1],
                status="graded", score=85,
            ),
            ExamSubmission(
                exam_id=ctx["eid"], student_id=extra_student_ids[2],
                status="submitted", score=55,
            ),
        ])
        db.commit()

    engine = db_session_factory.kw["bind"]
    statements = []

    def capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        response = client.get(
            f"{API}/exams/{ctx['eid']}/grades",
            headers=_h(ctx["t_tok"]),
            params={"page": 1, "page_size": 1},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["summary"]
    assert summary["expected_count"] == 9
    assert summary["submitted_count"] == 3
    assert summary["graded_count"] == 3
    assert summary["average_score"] == 78.3
    assert summary["highest_score"] == 95.0
    assert summary["pass_rate"] == 66.7
    assert summary["excellent_rate"] == 33.3
    assert summary["status_counts"] == {
        "scheduled": 0,
        "ready": 6,
        "in_progress": 0,
        "submitted": 1,
        "grading": 0,
        "graded": 2,
        "review_required": 0,
        "missed": 0,
    }
    assert [bucket["count"] for bucket in body["distribution"]] == [1, 1, 0, 0, 1]
    assert len(body["items"]) == 1
    assert any(
        statement.lstrip().startswith("select count(")
        and "avg(" in statement
        for statement in statements
    ), "成绩摘要必须由 SQL 聚合查询返回"
    unbounded_student_selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select users.")
        and "from users" in statement
        and "exam_submissions" in statement
    ]
    assert unbounded_student_selects, "应至少执行一次成绩页学生查询"
    assert all(" limit " in statement for statement in unbounded_student_selects), (
        "成绩页不应执行无 limit 的全班 ORM 行查询"
    )


def test_teacher_grade_detail_manual_score_override(client, db_session_factory):
    """教师成绩详情可逐题改分：0~本题满分，超限拒绝，并同步重算总分。"""
    from app.models import ExamAnswer, ExamSubmission
    from sqlalchemy import select

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']), json={"selected_options": ["A"]})
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q2_id']}", headers=_h(ctx['s_tok']), json={"code_answer": "def answer(): return True"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))

    # 先把提交固定为已完成，避免异步判题影响测试确定性
    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id)).all()
        by_qid = {a.question_id: a for a in answers}
        by_qid[ctx["q1_id"]].score = 10
        by_qid[ctx["q1_id"]].grading_status = "completed"
        by_qid[ctx["q2_id"]].score = 20
        by_qid[ctx["q2_id"]].grading_status = "completed"
        sub.status = "graded"
        sub.score = 30
        db.commit()
        sub_id, q1_answer_id = sub.id, by_qid[ctx["q1_id"]].id

    detail_url = f"{API}/exams/{ctx['eid']}/grades/{sub_id}"
    score_url = f"{detail_url}/answers/{q1_answer_id}/score"

    # 缺少理由：422，不允许修改
    r = client.patch(score_url, headers=_h(ctx['t_tok']), json={"score": 7.5})
    assert r.status_code == 422, r.text

    # 缺少结果修订号：422，避免旧教师页面静默覆盖新成绩
    r = client.patch(score_url, headers=_h(ctx['t_tok']), json={"score": 7.5, "reason": "学生部分正确"})
    assert r.status_code == 422, r.text

    # 超上限：422，不改动原分数
    r = client.patch(score_url, headers=_h(ctx['t_tok']), json={"score": 10.5, "reason": "超出上限测试", "expected_result_revision": 1})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SCORE_OUT_OF_RANGE"

    # 合法改分：10 → 7.5，父级总分同步为 27.5，并记录教师改分理由
    r = client.patch(score_url, headers=_h(ctx['t_tok']), json={"score": 7.5, "reason": "学生部分正确", "expected_result_revision": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    changed = next(a for a in body["answers"] if a["id"] == q1_answer_id)
    assert changed["score"] == 7.5
    assert changed["manual_score_reason"] == "学生部分正确"
    assert changed["manual_score_at"] is not None
    assert changed["result_revision"] == 2
    assert body["submission"]["score"] == 27.5
    assert body["analysis"]["objective_score"] == 7.5

    # 审计与分数处于同一事务且记录操作者、旧分和修订号。
    from app.models import ManualScoreAudit
    with db_session_factory() as db:
        audit = db.scalar(select(ManualScoreAudit).where(
            ManualScoreAudit.target_kind == "exam_answer",
            ManualScoreAudit.target_id == q1_answer_id,
        ))
        assert audit is not None
        assert audit.score == 7.5
        assert audit.previous_score == 10
        assert audit.result_revision == 2
        assert audit.actor_role == "teacher"

    stale = client.patch(score_url, headers=_h(ctx['t_tok']), json={
        "score": 6, "reason": "过期页面尝试覆盖", "expected_result_revision": 1,
    })
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "RESULT_REVISION_CONFLICT"

    # 详情页返回教师题目解析字段，但逐题 answers 仍不泄露私有字段
    assert body["questions"]
    assert all("correct_answer" not in a for a in body["answers"])
    assert all("correct_answer" in q for q in body["questions"])

    # 学生无权改分
    r = client.patch(score_url, headers=_h(ctx['s_tok']), json={"score": 6, "reason": "学生无权限测试", "expected_result_revision": 2})
    assert r.status_code == 403

    # 发布讲评后，学生端可以看到教师改分理由和改后得分
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={
        "show_score_after_grading": True,
        "show_questions_after_review": True,
        "show_answers_after_review": True,
    })
    with db_session_factory() as db:
        from app.models import Exam
        exam = db.get(Exam, ctx["eid"])
        exam.review_released_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()

    r = client.get(f"{API}/exams/{ctx['eid']}/session", headers=_h(ctx['s_tok']))
    assert r.status_code == 200, r.text
    saved = next(a for a in r.json()["saved_answers"] if a["question_id"] == ctx["q1_id"])
    assert saved["score"] == 7.5
    assert saved["manual_score_reason"] == "学生部分正确"


def test_exam_points_are_frozen_for_display_scoring_and_manual_cap(client, db_session_factory):
    """题目更新后，进行中的提交、自动客观评分和教师改分仍使用开考快照。"""
    from sqlalchemy import select
    from app.models import ExamAnswer, ExamQuestion, ExamSubmission

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    with db_session_factory() as db:
        db.get(ExamQuestion, ctx["q1_id"]).points = 4
        db.get(ExamQuestion, ctx["q2_id"]).points = 30
        db.commit()

    active = client.get(f"{API}/exams/{ctx['eid']}/session", headers=_h(ctx['s_tok']))
    assert active.status_code == 200, active.text
    assert {q["id"]: q["points"] for q in active.json()["questions"]} == {
        ctx["q1_id"]: 10.0, ctx["q2_id"]: 20.0,
    }

    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']),
               json={"selected_options": ["A"]})
    submitted = client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    assert submitted.status_code == 201, submitted.text
    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id)).all()
        q1 = next(answer for answer in answers if answer.question_id == ctx["q1_id"])
        q2 = next(answer for answer in answers if answer.question_id == ctx["q2_id"])
        assert q1.score == 10.0, "正确答案仍应按开考时10分评分，而不是当前4分"
        assert q2.score == 0.0, "未作答题必须进入正式结果并记0分"
        assert q1.result_revision == 1
        sub_id, answer_id, revision = sub.id, q1.id, q1.result_revision

    detail = client.get(f"{API}/exams/{ctx['eid']}/grades/{sub_id}", headers=_h(ctx['t_tok']))
    assert detail.status_code == 200, detail.text
    assert {q["id"]: q["points"] for q in detail.json()["questions"]} == {
        ctx["q1_id"]: 10.0, ctx["q2_id"]: 20.0,
    }
    # 9分超过当前配置4分，但低于冻结满分10分，必须接受。
    changed = client.patch(
        f"{API}/exams/{ctx['eid']}/grades/{sub_id}/answers/{answer_id}/score",
        headers=_h(ctx['t_tok']),
        json={"score": 9, "reason": "按开考时分值复核", "expected_result_revision": revision},
    )
    assert changed.status_code == 200, changed.text
    assert next(a for a in changed.json()["answers"] if a["id"] == answer_id)["score"] == 9.0


def test_exam_manual_score_rolls_back_when_audit_insert_fails(client, db_session_factory):
    """审计写入失败时逐题成绩、修订号和人工状态必须整体回滚。"""
    from sqlalchemy import event, select
    from app.models import ExamAnswer, ExamSubmission, ManualScoreAudit

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']),
               json={"selected_options": ["A"]})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        answer = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == ctx["q1_id"]))
        sub.status = "graded"
        answer.score = 10.0
        answer.grading_status = "completed"
        db.commit()
        sub_id, answer_id, revision = sub.id, answer.id, answer.result_revision

    def reject_audit_insert(_mapper, _connection, target):
        if target.target_id == answer_id:
            raise RuntimeError("synthetic audit storage failure")

    event.listen(ManualScoreAudit, "before_insert", reject_audit_insert)
    try:
        try:
            response = client.patch(
                f"{API}/exams/{ctx['eid']}/grades/{sub_id}/answers/{answer_id}/score",
                headers=_h(ctx['t_tok']),
                json={"score": 8, "reason": "审计失败回滚", "expected_result_revision": revision},
            )
            assert response.status_code >= 500, response.text
        except RuntimeError as exc:
            assert "synthetic audit storage failure" in str(exc)
    finally:
        event.remove(ManualScoreAudit, "before_insert", reject_audit_insert)

    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.score == 10.0
        assert answer.result_revision == revision
        assert answer.manual_score_reason is None
        assert db.scalar(select(ManualScoreAudit).where(
            ManualScoreAudit.target_kind == "exam_answer",
            ManualScoreAudit.target_id == answer_id,
        )) is None


def test_missing_exam_point_snapshot_blocks_manual_score_without_live_fallback(client, db_session_factory):
    from sqlalchemy import select
    from app.models import ExamAnswer, ExamSubmission

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']),
               json={"selected_options": ["A"]})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        answer = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == ctx["q1_id"]))
        mapping = dict(sub.grading_version_map)
        mapping[str(ctx["q1_id"])] = {"version_id": None}
        sub.grading_version_map = mapping
        db.commit()
        sub_id, answer_id, revision = sub.id, answer.id, answer.result_revision

    detail = client.get(f"{API}/exams/{ctx['eid']}/grades/{sub_id}", headers=_h(ctx['t_tok']))
    assert detail.status_code == 200, detail.text
    assert detail.json()["submission"]["points_snapshot_complete"] is False
    assert next(q for q in detail.json()["questions"] if q["id"] == ctx["q1_id"])["points"] is None
    changed = client.patch(
        f"{API}/exams/{ctx['eid']}/grades/{sub_id}/answers/{answer_id}/score",
        headers=_h(ctx['t_tok']),
        json={"score": 8, "reason": "需要基线校正", "expected_result_revision": revision},
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "FROZEN_POINTS_MISSING"


def test_teacher_grade_detail_can_score_unanswered_question(client, db_session_factory):
    """整题未作答（无 ExamAnswer 行）时，教师也可按题目直接给分。"""
    from app.models import ExamAnswer, ExamSubmission
    from sqlalchemy import select

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']), json={"selected_options": ["A"]})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))

    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        q1 = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == ctx["q1_id"]
        ))
        q2 = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == ctx["q2_id"]
        ))
        q1.score = 10
        q1.grading_status = "completed"
        q2.score = None
        q2.grading_status = "system_error"
        sub.status = "graded"
        sub.score = 10
        db.commit()
        sub_id = sub.id

    # 交卷时系统为未作答题建立正式 0 分行，教师仍可按 question_id 改分。
    r = client.patch(
        f"{API}/exams/{ctx['eid']}/grades/{sub_id}/questions/{ctx['q2_id']}/score",
        headers=_h(ctx['t_tok']),
        json={"score": 15, "reason": "整题未作答给分", "expected_result_revision": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submission"]["score"] == 25
    assert len(body["answers"]) == 2
    assert next(a for a in body["answers"] if a["question_id"] == ctx["q2_id"])["score"] == 15

    # 超上限仍被拒绝
    r = client.patch(
        f"{API}/exams/{ctx['eid']}/grades/{sub_id}/questions/{ctx['q2_id']}/score",
        headers=_h(ctx['t_tok']),
        json={"score": 20.1, "reason": "超出上限测试", "expected_result_revision": 2},
    )
    assert r.status_code == 422


def test_review_required_waits_until_every_question_scored(client, db_session_factory):
    """待复核提交不能因为只改了已有答题记录就提前汇总。"""
    from app.models import ExamAnswer, ExamSubmission
    from sqlalchemy import select

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']), json={"selected_options": ["A"]})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))

    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        q1 = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == ctx["q1_id"]
        ))
        q2 = db.scalar(select(ExamAnswer).where(
            ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == ctx["q2_id"]
        ))
        q1.score = 10
        q1.grading_status = "completed"
        q2.score = None
        q2.grading_status = "system_error"
        sub.status = "review_required"
        sub.review_reason = "存在未完成评分"
        db.commit()
        sub_id, q1_id = sub.id, q1.id

    r = client.patch(
        f"{API}/exams/{ctx['eid']}/grades/{sub_id}/answers/{q1_id}/score",
        headers=_h(ctx['t_tok']),
        json={"score": 9, "reason": "调整客观题得分", "expected_result_revision": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["submission"]["status"] == "review_required", "q2 尚未给分，不能提前汇总"

    r = client.patch(
        f"{API}/exams/{ctx['eid']}/grades/{sub_id}/questions/{ctx['q2_id']}/score",
        headers=_h(ctx['t_tok']),
        json={"score": 20, "reason": "编程题人工给分", "expected_result_revision": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submission"]["status"] == "graded"
    assert body["submission"]["score"] == 29
    assert body["submission"]["review_reason"] is None


def test_p0_maybe_finalize_checks_running_not_just_pending(client, db_session_factory):
    """P0 回归：finalize_if_ready 检查包含 running/queued 状态，不会提前汇总"""
    from app.services.exam_grading import finalize_if_ready, FinalizeOutcome
    from datetime import datetime, timezone
    from app.models import Course, Exam, ExamAnswer, ExamQuestion, ExamSubmission, User

    with db_session_factory() as db:
        teacher = User(username="p0_t", real_name="P0T", role="teacher", status="active",
                       password_hash="x")
        student = User(username="p0_s", real_name="P0S", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="P0C", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="P0E", status="published", duration_minutes=60)
        db.add(exam); db.flush()
        q1 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q1",
                          correct_answer={}, points=10, hidden_tests="assert True")
        q2 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q2",
                          correct_answer={}, points=20, hidden_tests="assert True")
        db.add_all([q1, q2]); db.flush()
        sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
        db.add(sub); db.flush()
        ans1 = ExamAnswer(submission_id=sub.id, question_id=q1.id,
                          code_answer="def a(): pass", grading_status="completed", score=10.0)
        ans2 = ExamAnswer(submission_id=sub.id, question_id=q2.id,
                          code_answer="def b(): pass", grading_status="running")
        db.add_all([ans1, ans2]); db.commit()
        sub_id = sub.id

    # 第一次测试：存在 running 答案 → waiting，不汇总
    with db_session_factory() as db:
        r = finalize_if_ready(sub_id, db)
        assert r.outcome == FinalizeOutcome.WAITING, f"running 答案应 waiting: {r}"
        sub_check = db.get(ExamSubmission, sub_id)
        assert sub_check.status == "grading", "不应提前汇总"

    # 第二次测试：全部 completed → 应汇总
    with db_session_factory() as db:
        ans2 = db.get(ExamAnswer, ans2.id)
        ans2.grading_status = "completed"
        ans2.score = 20.0
        db.commit()

    with db_session_factory() as db:
        r2 = finalize_if_ready(sub_id, db)
        assert r2.outcome == FinalizeOutcome.GRADED
        sub_check = db.get(ExamSubmission, sub_id)
        assert sub_check.status == "graded"
        assert sub_check.score == 30.0


def test_p1_resubmit_idempotent(client, db_session_factory):
    """P1 回归：重复交卷返回当前状态，不报 403"""
    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    # 第一次交卷
    r1 = client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    assert r1.status_code == 201
    # 第二次交卷（幂等）
    r2 = client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))
    assert r2.status_code in (200, 201), f"重复交卷不应 403: {r2.status_code}"


def test_exam_list_returns_is_submitted_for_student(client, db_session_factory):
    """任务中心数据源：学生考试列表返回 is_submitted（submitted/grading/graded 任一状态都算已考，与 dashboard 待办语义互补）"""
    from app.models import ExamSubmission, User
    from sqlalchemy import select

    ctx = _setup(client, db_session_factory)
    t_tok, s_tok, cid, eid = ctx["t_tok"], ctx["s_tok"], ctx["cid"], ctx["eid"]

    # 额外两个考试，用于覆盖 grading / graded 两种交后状态（eid 走真实 start→submit 流程）
    extra_ids = []
    now = datetime.datetime.now(timezone.utc)
    for title in ("E-grading", "E-graded"):
        extra_id = client.post(
            f"{API}/exams", headers=_h(t_tok),
            json={"course_id": cid, "offering_id": ctx["offering_id"], "title": title, "duration_minutes": 60,
                  "start_at": (now - timedelta(hours=1)).isoformat(),
                  "end_at": (now + timedelta(hours=1)).isoformat()},
        ).json()["id"]
        r = client.post(
            f"{API}/exams/{extra_id}/questions", headers=_h(t_tok),
            json={"question_type": "single_choice", "prompt": "Q", "correct_answer": {"correct": ["A"]},
                  "points": 10, "options": {"A": "选项A", "B": "选项B"}},
        )
        assert r.status_code == 201, r.text
        extra_ids.append(extra_id)
    grant_course_students(db_session_factory, cid, ["e_s"])
    grading_eid, graded_eid = extra_ids

    for exam_id in [eid, grading_eid, graded_eid]:
        r = client.patch(f"{API}/exams/{exam_id}", headers=_h(t_tok), json={"status": "published"})
        assert r.status_code == 200, r.text

    def student_items():
        r = client.get(f"{API}/exams", headers=_h(s_tok))
        assert r.status_code == 200, r.text
        return {it["id"]: it["is_submitted"] for it in r.json()["items"]}

    # 未开始考试：未提交
    assert student_items()[eid] is False

    # 只开始不交卷（started）：仍视为未提交（started 不算已考，与 dashboard 待办语义一致）
    r = client.post(f"{API}/exams/{eid}/start", headers=_h(s_tok))
    assert r.status_code == 201, r.text
    assert student_items()[eid] is False

    # 交卷后（真实流程，状态为 submitted/grading 之一）：已提交
    r = client.post(f"{API}/exams/{eid}/submit", headers=_h(s_tok))
    assert r.status_code == 201, r.text
    assert student_items()[eid] is True

    # 手动构造 grading / graded 提交记录：同样视为已提交
    with db_session_factory() as db:
        student = db.scalar(select(User).where(User.username == "e_s"))
        for exam_id, status in ((grading_eid, "grading"), (graded_eid, "graded")):
            db.add(ExamSubmission(exam_id=exam_id, student_id=student.id, status=status))
        db.commit()
    by_id = student_items()
    assert by_id[grading_eid] is True
    assert by_id[graded_eid] is True

    # 教师视角不计算学生提交状态：默认 False
    r = client.get(f"{API}/exams", headers=_h(t_tok))
    assert r.status_code == 200, r.text
    teacher_items = {it["id"]: it["is_submitted"] for it in r.json()["items"]}
    assert all(v is False for v in teacher_items.values()), teacher_items


def test_manual_score_rejects_inflight_and_invalidates_old_claim(client, db_session_factory):
    """人工改分与自动判题使用一致的版本与认领失效规则（批次一 2.2）。"""
    from sqlalchemy import select, update

    from app.models import ExamAnswer, ExamSubmission
    from app.services.judge_queue import complete_job

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q1_id']}", headers=_h(ctx['s_tok']),
               json={"selected_options": ["A"]})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))

    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id,
                                                   ExamAnswer.question_id == ctx["q1_id"]))
        answer.score = 10
        answer.grading_status = "completed"
        sub.status = "graded"
        sub.score = 10
        db.commit()
        sub_id, answer_id = sub.id, answer.id
    url = f"{API}/exams/{ctx['eid']}/grades/{sub_id}/answers/{answer_id}/score"

    # 在途判题（running + 认领）：拒绝人工改分，避免与新结果竞争
    with db_session_factory() as db:
        db.execute(update(ExamAnswer).where(ExamAnswer.id == answer_id)
                   .values(grading_status="running", claim_token="old-token"))
        db.commit()
    r = client.patch(url, headers=_h(ctx['t_tok']), json={"score": 6, "reason": "在途改分", "expected_result_revision": 0})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "GRADING_IN_PROGRESS"

    # 终态回到 completed（旧代次残留）后可改分；改分必须使旧认领失效
    with db_session_factory() as db:
        db.execute(update(ExamAnswer).where(ExamAnswer.id == answer_id)
                   .values(grading_status="completed", claim_token="old-token", result_revision=1))
        db.commit()
    r = client.patch(url, headers=_h(ctx['t_tok']), json={"score": 6, "reason": "人工复核改分", "expected_result_revision": 1})
    assert r.status_code == 200, r.text

    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.score == 6.0
        assert answer.claim_token is None, "人工改分必须释放旧代次"
        assert answer.result_revision == 2
        # 旧代次的迟到判题写入被栅栏拒绝，且不得改写人工结论
        db.info.setdefault("judge_claims", {})[("exam", answer_id)] = "old-token"
        assert complete_job(db, job_type="exam", object_id=answer_id, score=3.0,
                            result_details={"stale": True}) is False
    with db_session_factory() as db:
        assert db.get(ExamAnswer, answer_id).score == 6.0


def test_teacher_ai_retry_releases_old_claim(client, db_session_factory):
    """受控重试（教师 AI 重试入口）必须使旧代次失效（批次一 2.2）。"""
    from sqlalchemy import select

    from app.models import (CodeGrade, ExamAnswer, ExamQuestion, ExamSubmission,
                            JudgeGradingVersion, QuestionRubric)

    ctx = _setup(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['eid']}", headers=_h(ctx['t_tok']), json={"status": "published"})
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx['s_tok']))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['q2_id']}", headers=_h(ctx['s_tok']),
               json={"code_answer": "print(1)"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx['s_tok']))

    with db_session_factory() as db:
        sub = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["eid"]))
        answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id,
                                                   ExamAnswer.question_id == ctx["q2_id"]))
        rubric = QuestionRubric(exam_question_id=ctx["q2_id"], status="locked", version=1,
                                source_hash="c" * 64, source_snapshot={},
                                rubric_json={"algorithm_criteria": []}, model_name="fake")
        db.add(rubric)
        db.flush()
        version = JudgeGradingVersion(
            exam_question_id=ctx["q2_id"], version=1, status="active",
            execution_mode="data_v1", grading_mode_snapshot="active", entry_function="answer",
            payload={"schema_version": 2, "execution_model": "isolated_case_v2",
                     "entry_function": "answer", "groups": [
                         {"id": "F1", "name": "F", "dimension": "F", "max_score": 60,
                          "cases": [{"id": "f1", "args": [1], "expected": 1}]},
                         {"id": "R1", "name": "R", "dimension": "R", "max_score": 10,
                          "cases": [{"id": "r1", "args": [2], "expected": 2}]},
                     ], "binary_cases": []},
            score_cap_rules=[],
            context_snapshot={"rubric_snapshot": {"algorithm_criteria": []}, "rubric_version": 1},
            rubric_id=rubric.id, source_kind="manual", source_digest="d" * 64,
            spec_digest="e" * 64,
        )
        db.add(version)
        db.flush()
        submission = db.get(ExamSubmission, sub.id)
        answer.grading_version_id = version.id
        submission.grading_version_map = {**(submission.grading_version_map or {}),
                                          str(ctx["q2_id"]): {"version_id": version.id, "points": 20.0}}
        grade = CodeGrade(exam_answer_id=answer.id, rubric_id=rubric.id, mode="active",
                          status="review_required", needs_teacher_review=True,
                          claim_token="old-token", result_revision=2,
                          deterministic_details={"source": {
                              "result_revision": answer.result_revision or 0,
                              "grading_version_id": version.id,
                          }})
        db.add(grade)
        db.commit()
        grade_id = grade.id

    r = client.post(f"{API}/ai-grading/grades/{grade_id}/retry", headers=_h(ctx['t_tok']))
    assert r.status_code == 200, r.text
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status in ("pending", "queued")
        assert grade.claim_token is None, "重试必须使旧代次失效"
        assert grade.result_revision == 3
        assert grade.attempt_count == 0
