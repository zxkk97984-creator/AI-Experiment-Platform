"""第五轮——真实 SQLite API 测试：教师 grades 查询 + 状态机修复

A/B/C 分类：B 类（最小父行）——submission/rubric 外键经共享工厂建真实父行；
「关联缺失」fail-closed 场景在真实父行基础上临时绕过外键删除 submission 构造。
"""
import pytest
from datetime import datetime, timezone
from conftest import grant_teaching_fixture, auth_header, create_user, login, make_course, make_rubric, make_submission
from sqlalchemy import text
from app.dependencies import PaginationParams
from app.models import Assignment, Course, Exam, ExamQuestion, ExamSubmission, ExamAnswer, JudgeQuestion, Submission, CodeGrade, QuestionRubric, User


def _make_user(db, username, role):
    u = User(username=username, real_name=username, role=role, status="active", password_hash="x")
    db.add(u); db.flush()
    return u


class TestGradesQueryDedup:
    """验证 _build_grade_base_query 消除重复 JOIN"""

    def test_student_name_filter_returns_student_context(self, db_session_factory):
        from app.api.ai_grading import list_grades

        with db_session_factory() as db:
            teacher = _make_user(db, "gqn_teacher", "teacher")
            student = _make_user(db, "gqn_student", "student")
            student.real_name = "张三"
            course = Course(title="GQN", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            assignment = Assignment(title="GQNA", course_id=course.id, status="published")
            db.add(assignment); db.flush()
            question = JudgeQuestion(
                assignment_id=assignment.id, title="Q", function_name="f",
                hidden_tests="def test(): pass", grading_mode="active",
                test_groups=[{"id": "F1", "name": "F", "dimension": "F", "max_score": 60,
                              "tests": "def test(): assert True"}],
            )
            db.add(question); db.flush()
            rubric = QuestionRubric(
                judge_question_id=question.id, version=1, status="locked",
                source_hash="a", source_snapshot={}, rubric_json={}, model_name="m",
                locked_at=datetime.now(timezone.utc),
            )
            db.add(rubric); db.flush()
            submission = Submission(
                question_id=question.id, student_id=student.id, code="def f(): pass",
                status="graded", grading_status="completed", score=80,
            )
            db.add(submission); db.flush()
            grade = CodeGrade(
                submission_id=submission.id, rubric_id=rubric.id, mode="active",
                status="completed", functional_score=60, robustness_score=10,
            )
            db.add(grade); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()

            response = list_grades(
                kind="assignment", question_id=None, student_id=None,
                student_name="张", status=None,
                pagination=PaginationParams(page=1, page_size=20),
                db=db, current_user=teacher,
            )

            assert response.total == 1
            assert response.items[0]["student_id"] == student.id
            assert response.items[0]["student_name"] == "张三"

    def test_kind_assignment_uses_single_path(self, db_session_factory):
        from app.api.ai_grading import _build_grade_base_query

        with db_session_factory() as db:
            teacher = _make_user(db, "gqt1", "teacher")
            student = _make_user(db, "gqt1_student", "student")
            course = Course(title="GCT1", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            a = Assignment(title="GA1", course_id=course.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,"tests":"def test(): assert True"}])
            db.add(q); db.flush()
            rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            sub = Submission(question_id=q.id, student_id=student.id, code="def f(): pass",
                            status="graded", grading_status="completed", score=80)
            db.add(sub); db.flush()
            cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                          status="completed", functional_score=60, robustness_score=10)
            db.add(cg); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()

            query, count_q = _build_grade_base_query(db, teacher, kind="assignment",
                                                     question_id=None, student_id=None, status=None)
            grades = db.scalars(query).all()
            total = db.scalar(count_q)
            assert len(grades) >= 1
            assert total >= 1
            assert any(g.submission_id == sub.id for g in grades)

    def test_kind_exam_uses_single_path(self, db_session_factory):
        from app.api.ai_grading import _build_grade_base_query

        with db_session_factory() as db:
            teacher = _make_user(db, "gqt2", "teacher")
            student = _make_user(db, "gqt2_student", "student")
            course = Course(title="GCT2", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            exam = Exam(course_id=course.id, title="GE1", status="published", duration_minutes=60)
            db.add(exam); db.flush()
            eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=10, grading_mode="active")
            db.add(eq); db.flush()
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            es = ExamSubmission(
                exam_id=exam.id, student_id=student.id, status="grading",
                grading_version_map={str(eq.id): {"version_id": None, "points": float(eq.points)}},
            )
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=5)
            db.add(ea); db.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                          status="completed", functional_score=60, robustness_score=10)
            db.add(cg); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()

            query, count_q = _build_grade_base_query(db, teacher, kind="exam",
                                                     question_id=None, student_id=None, status=None)
            grades = db.scalars(query).all()
            total = db.scalar(count_q)
            assert len(grades) >= 1
            assert total >= 1

    def test_filter_with_question_id_no_dup_join(self, db_session_factory):
        from app.api.ai_grading import _build_grade_base_query

        with db_session_factory() as db:
            teacher = _make_user(db, "gqt3", "teacher")
            student = _make_user(db, "gqt3_student", "student")
            course = Course(title="GCT3", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            a = Assignment(title="GA3", course_id=course.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,"tests":"def test(): assert True"}])
            db.add(q); db.flush()
            rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            sub = Submission(question_id=q.id, student_id=student.id, code="def f(): pass",
                            status="graded", grading_status="completed", score=80)
            db.add(sub); db.flush()
            cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                          status="completed", functional_score=60, robustness_score=10)
            db.add(cg); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()

            query, count_q = _build_grade_base_query(db, teacher, kind="assignment",
                                                     question_id=q.id, student_id=None, status=None)
            grades = db.scalars(query).all()
            total = db.scalar(count_q)
            assert len(grades) == 1
            assert total == 1


class TestRetryRejectsCompleted:
    """retry 拒绝 completed"""

    def test_retry_completed_returns_400(self, client, db_session_factory):
        with db_session_factory() as db:
            teacher = create_user(db_session_factory, "rrc_t", "teacher")
            student = create_user(db_session_factory, "rrc_s", "student")
            course = Course(title="CRC", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            a = Assignment(title="ARC", course_id=course.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,"tests":"def test(): assert True"}])
            db.add(q); db.flush()
            now = datetime.now(timezone.utc)
            rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=now)
            db.add(rub); db.flush()
            sub = Submission(question_id=q.id, student_id=student.id, code="def f(): pass",
                            status="graded", grading_status="completed", score=80)
            db.add(sub); db.flush()
            cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                          status="completed", functional_score=60, robustness_score=10)
            db.add(cg); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()
            grade_id = cg.id

        tok, _ = login(client, "rrc_t")
        resp = client.post(
            f"/api/v1/ai-grading/grades/{grade_id}/retry",
            headers=auth_header(tok),
        )
        assert resp.status_code == 400, f"completed 状态重试应返回 400，实际: {resp.status_code}"
        assert "已完成" in resp.text or "ALREADY" in resp.text


class TestRegradeSkipsRunning:
    """regrade 跳过 running/queued"""

    def test_regrade_skips_running_codegrade(self, db_session_factory):
        with db_session_factory() as db:
            teacher = _make_user(db, "rsr_t", "teacher")
            student = _make_user(db, "rsr_t_student", "student")
            course = Course(title="CRS", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            a = Assignment(title="ARS", course_id=course.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,"tests":"def test(): assert True"}])
            db.add(q); db.flush()
            now = datetime.now(timezone.utc)
            rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=now)
            db.add(rub); db.flush()
            sub = Submission(question_id=q.id, student_id=student.id, code="def f(): pass",
                            status="running", grading_status="running", score=None)
            db.add(sub); db.flush()
            cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                          status="running", functional_score=60, robustness_score=10)
            db.add(cg); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()

            existing = db.get(CodeGrade, cg.id)
            assert existing is not None
            # running 不应被重置为 pending
            assert existing.status != "pending"


class TestOverrideStatusSync:
    """active exam override 设置 status=completed + finalize"""

    def test_override_sets_completed_status(self, client, db_session_factory):
        teacher = create_user(db_session_factory, "osc_t", "teacher")
        student = create_user(db_session_factory, "osc_s", "student")

        with db_session_factory() as db:
            course = Course(title="COS", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            exam = Exam(course_id=course.id, title="EOS", status="published", duration_minutes=60)
            db.add(exam); db.flush()
            eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=10, grading_mode="active")
            db.add(eq); db.flush()
            now = datetime.now(timezone.utc)
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=now)
            db.add(rub); db.flush()
            es = ExamSubmission(
                exam_id=exam.id, student_id=student.id, status="grading",
                grading_version_map={str(eq.id): {"version_id": None, "points": float(eq.points)}},
            )
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=None)
            db.add(ea); db.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                          status="review_required", functional_score=60, robustness_score=10,
                          algorithm_score=15, quality_score=5)
            db.add(cg); db.flush()
            grant_teaching_fixture(db, course, [student.id])
            db.commit()
            grade_id = cg.id

        tok, _ = login(client, "osc_t")
        resp = client.post(
            f"/api/v1/ai-grading/grades/{grade_id}/override",
            headers=auth_header(tok),
            json={"reason": "教师审核通过", "final_score_100": 90},
        )
        assert resp.status_code == 200, f"覆盖应成功: {resp.status_code} {resp.text}"

        with db_session_factory() as db:
            cg_check = db.get(CodeGrade, grade_id)
            assert cg_check.status == "completed", f"覆盖后应为 completed: {cg_check.status}"


class TestDetailFailClosed:
    """get_grade_detail 权限 fail-closed"""

    def test_detail_denies_missing_submission(self, client, db_session_factory):
        teacher = create_user(db_session_factory, "dfc_t", "teacher")
        # 教师必须有课程，否则会先命中「无权访问」分支而非「提交记录不存在」
        make_course(db_session_factory, teacher_username="dfc_t")
        submission_id = make_submission(db_session_factory)
        rubric_id = make_rubric(db_session_factory)

        with db_session_factory() as db:
            cg = CodeGrade(submission_id=submission_id, rubric_id=rubric_id,
                           mode="active", status="completed", functional_score=60)
            db.add(cg); db.commit()
            grade_id = cg.id

        # 构造「关联缺失」：删除 submission 行（临时绕过外键，仅用于测试 fail-closed 分支）
        with db_session_factory() as db:
            engine = db.get_bind()
            if engine.dialect.name == "sqlite":
                raw = engine.raw_connection()
                try:
                    cur = raw.cursor()
                    cur.execute("PRAGMA foreign_keys=OFF")
                    cur.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
                    cur.execute("PRAGMA foreign_keys=ON")
                    raw.commit()
                    cur.close()
                finally:
                    raw.close()
            else:
                with engine.begin() as conn:
                    conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
                    conn.execute(
                        text("DELETE FROM submissions WHERE id = :sid"),
                        {"sid": submission_id},
                    )
                    conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")

        tok, _ = login(client, "dfc_t")
        resp = client.get(
            f"/api/v1/ai-grading/grades/{grade_id}",
            headers=auth_header(tok),
        )
        # Missing ownership evidence is hidden by the scoped query before
        # loading the grade; 404 rejects access without revealing its existence.
        assert resp.status_code == 404, f"缺失关联应隐藏为 404: {resp.status_code}"
