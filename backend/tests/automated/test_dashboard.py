"""角色首页聚合测试：真实数据、角色隔离、排序、路由、公告可见性。"""

from datetime import datetime, timedelta, timezone

from conftest import auth_header, create_user, grant_teaching_fixture, login, seed_basic_environment

# 冻结在模块导入时刻，供种子数据相对偏移；HTTP 端点内部 now 与它仅差毫秒级
NOW = datetime.now(timezone.utc)


def _build_graph(db_session_factory):
    """构造最小混合角色数据图：
    两个教师、一个只选教师 A 课程的学生、一个 pending/一个 completed 作业、
    一场 upcoming 考试、一条最新实验记录（含已复核与未复核提交）、
    一条需教师复核的 AI 评分、一条可见与一条不可见课程公告。
    """
    seed_basic_environment(db_session_factory)
    from app.models import (
        Announcement, Assignment, Chapter, CodeGrade, Course, CourseEnrollment,
        Exam, ExperimentRecord, ExperimentSubmission, JudgeQuestion, Lesson,
        NotebookTemplate, NotebookTemplateVersion, QuestionRubric, Submission,
    )

    teacher_a = create_user(db_session_factory, "dash-teacher-a", "teacher")
    teacher_b = create_user(db_session_factory, "dash-teacher-b", "teacher")
    student = create_user(db_session_factory, "dash-student", "student")

    with db_session_factory() as db:
        course_a = Course(title="机器学习导论", status="published", teacher_id=teacher_a.id)
        course_b = Course(title="深度学习实践", status="published", teacher_id=teacher_b.id)
        db.add_all([course_a, course_b])
        db.flush()
        db.add(CourseEnrollment(course_id=course_a.id, student_id=student.id, status="enrolled"))

        chapter = Chapter(course_id=course_a.id, title="第一章")
        db.add(chapter)
        db.flush()
        lesson = Lesson(chapter_id=chapter.id, title="决策树实验")
        db.add(lesson)
        db.flush()

        pending = Assignment(
            title="特征工程", status="published", course_id=course_a.id,
            due_at=NOW + timedelta(days=1),
        )
        done = Assignment(
            title="数据清洗", status="published", course_id=course_a.id,
            due_at=NOW - timedelta(days=1),
        )
        db.add_all([pending, done])
        db.flush()
        q_pending = JudgeQuestion(
            assignment_id=pending.id, title="特征工程题", function_name="featurize",
            hidden_tests="",
        )
        q_done = JudgeQuestion(
            assignment_id=done.id, title="清洗题", function_name="clean", hidden_tests="",
        )
        db.add_all([q_pending, q_done])
        db.flush()
        sub_done = Submission(
            question_id=q_done.id, student_id=student.id, code="pass",
            status="completed", grading_status="completed", score=88.0,
            finished_at=NOW - timedelta(hours=5),
        )
        db.add(sub_done)

        exam = Exam(
            title="期中考试", status="published", course_id=course_a.id,
            start_at=NOW + timedelta(days=2),
        )
        db.add(exam)

        tpl = NotebookTemplate(name="决策树模板", status="published", owner_id=teacher_a.id)
        db.add(tpl)
        db.flush()
        ver = NotebookTemplateVersion(
            template_id=tpl.id, version_number=1, sha256="x" * 64,
            cells=[], cell_order=[], notebook_metadata={}, published_by_id=teacher_a.id,
        )
        db.add(ver)
        db.flush()
        record = ExperimentRecord(
            lesson_id=lesson.id, template_version_id=ver.id, student_id=student.id,
            status="submitted", started_at=NOW - timedelta(days=2),
            submitted_at=NOW - timedelta(hours=3),
        )
        db.add(record)
        db.flush()
        reviewed = ExperimentSubmission(
            record_id=record.id, attempt_number=1, client_request_id="r1",
            cells_snapshot={}, submitted_at=NOW - timedelta(hours=3),
            score=92.0, feedback="特征选择解释清晰。",
            reviewed_at=NOW - timedelta(minutes=30), reviewed_by_id=teacher_a.id,
        )
        unreviewed = ExperimentSubmission(
            record_id=record.id, attempt_number=2, client_request_id="r2",
            cells_snapshot={}, submitted_at=NOW - timedelta(minutes=10),
        )
        db.add_all([reviewed, unreviewed])
        db.flush()

        rubric = QuestionRubric(
            judge_question_id=q_done.id, version=1, status="published",
            source_hash="y" * 64, source_snapshot={}, rubric_json={}, model_name="test",
        )
        db.add(rubric)
        db.flush()
        code_grade = CodeGrade(
            submission_id=sub_done.id, rubric_id=rubric.id, mode="ai",
            status="completed", needs_teacher_review=True, review_reason="分数低于阈值",
            finished_at=NOW - timedelta(hours=1),
        )
        db.add(code_grade)

        notice_visible = Announcement(
            title="机房调整", content="本周换机房。", scope="course",
            course_id=course_a.id, author_id=teacher_a.id,
        )
        notice_invisible = Announcement(
            title="B 课程公告", content="不可见。", scope="course",
            course_id=course_b.id, author_id=teacher_b.id,
        )
        db.add_all([notice_visible, notice_invisible])
        grant_teaching_fixture(db, course_a, student_ids=[student.id])
        db.commit()
        for obj in [course_a, course_b, lesson, pending, done, exam, record]:
            db.refresh(obj)

    return {
        "teacher_a": teacher_a, "teacher_b": teacher_b, "student": student,
        "course_a": course_a, "course_b": course_b, "lesson": lesson,
        "pending": pending, "done": done, "exam": exam, "record": record,
        "notice_visible": notice_visible, "notice_invisible": notice_invisible,
    }


# ── 学生首页 ───────────────────────────────────────────────────


def test_student_dashboard_real_data(client, db_session_factory):
    g = _build_graph(db_session_factory)
    token, _ = login(client, "dash-student")
    resp = client.get("/api/v1/dashboard/student", headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()

    # 真实计数
    assert body["summary"]["course_count"] == 1
    assert body["summary"]["pending_assignment_count"] == 1
    assert body["summary"]["upcoming_exam_count"] == 1

    # 优先项：待交作业排第一，路由正确
    assert body["priority_items"][0]["kind"] == "assignment"
    assert body["priority_items"][0]["id"] == g["pending"].id
    assert body["priority_items"][0]["route"] == f"/student/assignments/{g['pending'].id}"

    # 续学：最近实验记录
    assert body["continue_learning"]["kind"] == "lesson_experiment"
    assert body["continue_learning"]["route"] == (
        f"/student/courses/{g['course_a'].id}/notebook/{g['lesson'].id}"
    )

    # 课程快照：真实计数而非编造完成率
    assert body["courses"][0]["pending_assignment_count"] == 1
    assert body["courses"][0]["upcoming_exam_count"] == 1
    assert body["courses"][0]["route"] == f"/student/courses/{g['course_a'].id}"

    # 最新反馈：真实评分与评语
    assert len(body["recent_feedback"]) >= 1
    assert body["recent_feedback"][0]["score"] == 92.0
    assert body["recent_feedback"][0]["feedback"] == "特征选择解释清晰。"

    # 公告隔离：只可见本课程公告
    assert {a["id"] for a in body["announcements"]} == {g["notice_visible"].id}
    assert body["summary"]["unread_announcement_count"] == 1


def test_student_dashboard_excludes_self_enrollment_hidden_by_class_visibility(
    client, db_session_factory,
):
    """首页课程、计数与学生课程列表必须使用同一套 class 可见性规则。"""
    from app.models import Course, CourseEnrollment

    teacher = create_user(db_session_factory, "dash-class-teacher", "teacher")
    student = create_user(db_session_factory, "dash-class-student", "student")
    with db_session_factory() as db:
        course = Course(
            title="已切换为教学班可见",
            status="published",
            visibility="class",
            teacher_id=teacher.id,
        )
        db.add(course)
        db.flush()
        db.add(CourseEnrollment(
            course_id=course.id,
            student_id=student.id,
            status="enrolled",
            origin="self",
        ))
        db.commit()

    token, _ = login(client, "dash-class-student")
    headers = auth_header(token)

    course_list = client.get("/api/v1/courses", headers=headers)
    assert course_list.status_code == 200
    assert course_list.json()["total"] == 0

    dashboard = client.get("/api/v1/dashboard/student", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["summary"]["course_count"] == 0
    assert dashboard.json()["courses"] == []


def test_priority_experiments_only_unsubmitted(client, db_session_factory):
    """待办实验仅显示 started（未提交）；submitted/graded 已交记录不进 priority_items"""
    from app.models import (
        ExperimentModule, ExperimentRecord, Lesson,
        NotebookTemplate, NotebookTemplateVersion,
    )

    g = _build_graph(db_session_factory)
    with db_session_factory() as db:
        tpl = NotebookTemplate(name="待办模板", status="published", owner_id=g["teacher_a"].id)
        db.add(tpl)
        db.flush()
        ver = NotebookTemplateVersion(
            template_id=tpl.id, version_number=1, sha256="z" * 64,
            cells=[], cell_order=[], notebook_metadata={}, published_by_id=g["teacher_a"].id,
        )
        db.add(ver)
        db.flush()
        lesson_started = Lesson(chapter_id=g["lesson"].chapter_id, title="未提交课时实验")
        lesson_graded = Lesson(chapter_id=g["lesson"].chapter_id, title="已评分课时实验")
        db.add_all([lesson_started, lesson_graded])
        db.flush()
        module_started = ExperimentModule(
            name="未提交模块实验", status="published", owner_id=g["teacher_a"].id,
        )
        module_submitted = ExperimentModule(
            name="已提交模块实验", status="published", owner_id=g["teacher_a"].id,
        )
        db.add_all([module_started, module_submitted])
        db.flush()
        rec_started = ExperimentRecord(
            lesson_id=lesson_started.id, template_version_id=ver.id,
            student_id=g["student"].id, status="started", started_at=NOW - timedelta(hours=1),
        )
        rec_graded = ExperimentRecord(
            lesson_id=lesson_graded.id, template_version_id=ver.id,
            student_id=g["student"].id, status="graded", started_at=NOW - timedelta(days=1),
            submitted_at=NOW - timedelta(hours=20),
        )
        rec_mod_started = ExperimentRecord(
            module_id=module_started.id, template_version_id=ver.id,
            student_id=g["student"].id, status="started", started_at=NOW - timedelta(minutes=30),
        )
        rec_mod_submitted = ExperimentRecord(
            module_id=module_submitted.id, template_version_id=ver.id,
            student_id=g["student"].id, status="submitted", started_at=NOW - timedelta(days=2),
            submitted_at=NOW - timedelta(hours=5),
        )
        db.add_all([rec_started, rec_graded, rec_mod_started, rec_mod_submitted])
        from app.models import Course
        grant_teaching_fixture(db, db.get(Course, g["course_a"].id), student_ids=[g["student"].id])
        db.commit()

    token, _ = login(client, "dash-student")
    body = client.get("/api/v1/dashboard/student", headers=auth_header(token)).json()
    exp_ids = [i["id"] for i in body["priority_items"] if i["kind"] == "experiment"]
    # 仅未提交（started）记录：课时 + 模块各一条；已提交/已评分（含 _build_graph 原记录）全部隐藏
    assert set(exp_ids) == {rec_started.id, rec_mod_started.id}


# ── 教师首页 ───────────────────────────────────────────────────


def test_teacher_dashboard_real_data(client, db_session_factory):
    g = _build_graph(db_session_factory)
    token, _ = login(client, "dash-teacher-a")
    resp = client.get("/api/v1/dashboard/teacher", headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()

    assert body["summary"]["course_count"] == 1
    assert body["summary"]["student_count"] == 1
    # 未复核实验提交 + AI 评分待复核
    assert body["summary"]["pending_review_count"] >= 2
    # 72 小时内截止的作业
    assert body["summary"]["upcoming_deadline_count"] == 1

    assert body["managed_courses"] == [{"id": g["course_a"].id, "title": "机器学习导论"}]
    assert all(item["course_id"] == g["course_a"].id for item in body["course_health"])

    # 工作队列含两类待复核
    kinds = {item["kind"] for item in body["work_items"]}
    assert "ai_review" in kinds
    assert "experiment_review" in kinds

    # 最近动态来自真实提交
    assert len(body["recent_activity"]) >= 1
    assert body["recent_activity"][0]["actor_name"] == "dash-student"
    assert body["recent_activity"][0]["route"].startswith("/teacher/submissions/")

    # 公告隔离：只见自己课程的公告
    assert {a["id"] for a in body["announcements"]} == {g["notice_visible"].id}


# ── 排序健壮性 ─────────────────────────────────────────────────


def test_dashboard_tolerates_null_grading_timestamps(client, db_session_factory):
    """finished_at/graded_at 为 None 时排序不抛 TypeError，端点正常返回"""
    from sqlalchemy import select

    from app.models import CodeGrade, JudgeQuestion, QuestionRubric, Submission

    g = _build_graph(db_session_factory)
    with db_session_factory() as db:
        q_done = db.scalar(
            select(JudgeQuestion).where(JudgeQuestion.assignment_id == g["done"].id)
        )
        sub2 = Submission(
            question_id=q_done.id, student_id=g["student"].id, code="y",
            status="completed", grading_status="completed", score=80.0, finished_at=None,
        )
        db.add(sub2)
        db.flush()
        rubric = db.scalar(
            select(QuestionRubric).where(QuestionRubric.judge_question_id == q_done.id)
        )
        db.add(CodeGrade(
            submission_id=sub2.id, rubric_id=rubric.id, mode="ai",
            status="completed", needs_teacher_review=True, finished_at=None,
        ))
        db.commit()

    student_token, _ = login(client, "dash-student")
    resp = client.get("/api/v1/dashboard/student", headers=auth_header(student_token))
    assert resp.status_code == 200
    teacher_token, _ = login(client, "dash-teacher-a")
    resp = client.get("/api/v1/dashboard/teacher", headers=auth_header(teacher_token))
    assert resp.status_code == 200


# ── 截止窗口与已提交语义 ───────────────────────────────────────


def test_teacher_deadline_window_covers_seven_days(client, db_session_factory):
    """5 天后截止的作业计入 summary 与工作队列（7 天窗口），urgency 仍为 normal"""
    from app.models import Assignment, Course, CourseEnrollment, JudgeQuestion

    teacher = create_user(db_session_factory, "dl-seven-teacher", "teacher")
    seed_basic_environment(db_session_factory)
    student = create_user(db_session_factory, "dl-seven-student", "student")
    with db_session_factory() as db:
        course = Course(title="七日窗口课", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        a = Assignment(
            title="五天后的作业", status="published", course_id=course.id,
            due_at=NOW + timedelta(days=5),
        )
        db.add(a)
        db.flush()
        db.add(JudgeQuestion(assignment_id=a.id, title="题一", function_name="f", hidden_tests=""))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    token, _ = login(client, "dl-seven-teacher")
    body = client.get("/api/v1/dashboard/teacher", headers=auth_header(token)).json()
    assert body["summary"]["upcoming_deadline_count"] == 1
    deadlines = [w for w in body["work_items"] if w["kind"] == "deadline"]
    assert len(deadlines) == 1
    # 5 天 > 72 小时：计入窗口但 urgency 为 normal
    assert deadlines[0]["urgency"] == "normal"


def test_deadline_submitted_requires_all_questions_completed(client, db_session_factory):
    """两题作业只答一题：不计入已提交学生数"""
    from app.models import Assignment, Course, CourseEnrollment, JudgeQuestion, Submission

    teacher = create_user(db_session_factory, "dl-partial-teacher", "teacher")
    student = create_user(db_session_factory, "dl-partial-student", "student")
    seed_basic_environment(db_session_factory)
    with db_session_factory() as db:
        course = Course(title="多选题课", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        a = Assignment(
            title="两题作业", status="published", course_id=course.id,
            due_at=NOW + timedelta(days=1),
        )
        db.add(a)
        db.flush()
        q1 = JudgeQuestion(assignment_id=a.id, title="题一", function_name="f1", hidden_tests="")
        q2 = JudgeQuestion(assignment_id=a.id, title="题二", function_name="f2", hidden_tests="")
        db.add_all([q1, q2])
        db.flush()
        db.add(Submission(
            question_id=q1.id, student_id=student.id, code="x",
            status="completed", grading_status="completed", score=50.0,
        ))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    token, _ = login(client, "dl-partial-teacher")
    body = client.get("/api/v1/dashboard/teacher", headers=auth_header(token)).json()
    deadlines = [w for w in body["work_items"] if w["kind"] == "deadline"]
    assert len(deadlines) == 1
    assert deadlines[0]["detail"] == "0/1 已提交"


def test_deadline_full_participation_omits_work_item(client, db_session_factory):
    """全员已提交：summary 计数保留，但工作队列省略该 deadline 项"""
    from app.models import Assignment, Course, CourseEnrollment, JudgeQuestion, Submission

    teacher = create_user(db_session_factory, "dl-full-teacher", "teacher")
    seed_basic_environment(db_session_factory)
    student = create_user(db_session_factory, "dl-full-student", "student")
    with db_session_factory() as db:
        course = Course(title="全答课", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        a = Assignment(
            title="两题全答作业", status="published", course_id=course.id,
            due_at=NOW + timedelta(days=1),
        )
        db.add(a)
        db.flush()
        q1 = JudgeQuestion(assignment_id=a.id, title="题一", function_name="f1", hidden_tests="")
        q2 = JudgeQuestion(assignment_id=a.id, title="题二", function_name="f2", hidden_tests="")
        db.add_all([q1, q2])
        db.flush()
        for q in (q1, q2):
            db.add(Submission(
                question_id=q.id, student_id=student.id, code="x",
                status="completed", grading_status="completed", score=50.0,
            ))
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

    token, _ = login(client, "dl-full-teacher")
    body = client.get("/api/v1/dashboard/teacher", headers=auth_header(token)).json()
    # 7 天 summary 计数保留
    assert body["summary"]["upcoming_deadline_count"] == 1
    # 全员已提交 → 工作队列无该 deadline 项
    assert all(w["kind"] != "deadline" for w in body["work_items"])


def test_deadline_submitted_counts_only_currently_enrolled(client, db_session_factory):
    """完成全部题目后退课的学生的提交不计入分子（也不计入分母）"""
    from sqlalchemy import select

    from app.models import Assignment, Course, CourseEnrollment, JudgeQuestion, Submission
    from app.models.teaching import OfferingEnrollment

    teacher = create_user(db_session_factory, "dl-withdrawn-teacher", "teacher")
    seed_basic_environment(db_session_factory)
    student = create_user(db_session_factory, "dl-withdrawn-student", "student")
    with db_session_factory() as db:
        course = Course(title="退课课", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        db.add(CourseEnrollment(course_id=course.id, student_id=student.id, status="enrolled"))
        a = Assignment(
            title="退课作业", status="published", course_id=course.id,
            due_at=NOW + timedelta(days=1),
        )
        db.add(a)
        db.flush()
        q = JudgeQuestion(assignment_id=a.id, title="题一", function_name="f", hidden_tests="")
        db.add(q)
        db.flush()
        db.add(Submission(
            question_id=q.id, student_id=student.id, code="x",
            status="completed", grading_status="completed", score=80.0,
        ))
        offering = grant_teaching_fixture(db, course, student_ids=[student.id])
        allocated = db.scalar(select(OfferingEnrollment).where(
            OfferingEnrollment.offering_id == offering.id,
            OfferingEnrollment.student_id == student.id,
        ))
        allocated.status, allocated.active_scope_key = "dropped", None
        enrollment = db.scalar(
            select(CourseEnrollment).where(
                CourseEnrollment.course_id == course.id,
                CourseEnrollment.student_id == student.id,
            )
        )
        enrollment.status = "withdrawn"
        db.commit()

    token, _ = login(client, "dl-withdrawn-teacher")
    body = client.get("/api/v1/dashboard/teacher", headers=auth_header(token)).json()
    deadlines = [w for w in body["work_items"] if w["kind"] == "deadline"]
    assert len(deadlines) == 1
    # 退课者既不进分子也不进分母
    assert deadlines[0]["detail"] == "0/0 已提交"


def test_student_feedback_excludes_non_enrolled_courses(client, db_session_factory):
    """学生对未选课课程的作业/课时实验反馈不出现在 recent_feedback 中"""
    from app.models import (
        Assignment, Chapter, ExperimentRecord, ExperimentSubmission,
        JudgeQuestion, Lesson, NotebookTemplate, NotebookTemplateVersion, Submission,
    )

    g = _build_graph(db_session_factory)
    with db_session_factory() as db:
        # course_b 属于 teacher_b，学生未选课
        chapter_b = Chapter(course_id=g["course_b"].id, title="B 章")
        db.add(chapter_b)
        db.flush()
        lesson_b = Lesson(chapter_id=chapter_b.id, title="B 课实验")
        db.add(lesson_b)
        db.flush()
        a_b = Assignment(
            title="B 课作业", status="published", course_id=g["course_b"].id,
            due_at=NOW + timedelta(days=10),
        )
        db.add(a_b)
        db.flush()
        q_b = JudgeQuestion(assignment_id=a_b.id, title="B 题", function_name="fb", hidden_tests="")
        db.add(q_b)
        db.flush()
        db.add(Submission(
            question_id=q_b.id, student_id=g["student"].id, code="z",
            status="completed", grading_status="completed", score=70.0,
            finished_at=NOW - timedelta(minutes=20),
        ))
        tpl_b = NotebookTemplate(name="B 模板", status="published", owner_id=g["teacher_b"].id)
        db.add(tpl_b)
        db.flush()
        ver_b = NotebookTemplateVersion(
            template_id=tpl_b.id, version_number=1, sha256="c" * 64,
            cells=[], cell_order=[], notebook_metadata={}, published_by_id=g["teacher_b"].id,
        )
        db.add(ver_b)
        db.flush()
        record_b = ExperimentRecord(
            lesson_id=lesson_b.id, template_version_id=ver_b.id,
            student_id=g["student"].id, status="submitted",
            submitted_at=NOW - timedelta(minutes=15),
        )
        db.add(record_b)
        db.flush()
        db.add(ExperimentSubmission(
            record_id=record_b.id, attempt_number=1, client_request_id="b-1",
            cells_snapshot={}, submitted_at=NOW - timedelta(minutes=15),
            score=66.0, feedback="B 课反馈",
            reviewed_at=NOW - timedelta(minutes=10), reviewed_by_id=g["teacher_b"].id,
        ))
        db.commit()

    token, _ = login(client, "dash-student")
    body = client.get("/api/v1/dashboard/student", headers=auth_header(token)).json()
    titles = [f["title"] for f in body["recent_feedback"]]
    assert "B 课作业" not in titles
    assert "B 课实验 反馈" not in titles
    # 原有 enrolled 课程的反馈仍在
    assert any("决策树实验" in t for t in titles)


# ── 角色隔离 ───────────────────────────────────────────────────


def test_dashboard_role_isolation(client, db_session_factory):
    _build_graph(db_session_factory)
    student_token, _ = login(client, "dash-student")
    teacher_token, _ = login(client, "dash-teacher-a")

    assert (
        client.get("/api/v1/dashboard/teacher", headers=auth_header(student_token)).status_code
        == 403
    )
    assert (
        client.get("/api/v1/dashboard/student", headers=auth_header(teacher_token)).status_code
        == 403
    )
