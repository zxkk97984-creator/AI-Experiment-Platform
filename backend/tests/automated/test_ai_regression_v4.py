"""第六轮回归测试——7 项阻断修复的独立验证

A/B/C 分类：B 类（最小父行）——student_id/exam_id 等外键经共享工厂
make_student / make_exam / make_assignment 建真实父行。
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from conftest import grant_teaching_fixture, make_assignment, make_course, make_exam, make_student, grant_judge_claim


# ═══════════════════════════════════════════════════════════════
# 1. grading_mode 默认 active + Literal 拒绝非法值
# ═══════════════════════════════════════════════════════════════

class TestGradingModeDefault:
    """新建代码题默认 active，显式 legacy 保持 legacy，非法值被 Literal 拒绝"""

    def test_assignment_create_defaults_to_active(self, client, db_session_factory):
        """创建作业题不传 grading_mode → 默认 active"""
        from conftest import create_user, login, auth_header
        create_user(db_session_factory, "gmd_t", "teacher")
        tok, _ = login(client, "gmd_t")

        cid = client.post("/api/v1/courses", headers=auth_header(tok),
                          json={"title": "GD"}).json()["id"]
        aid = client.post("/api/v1/assignments", headers=auth_header(tok),
                          json={"title": "GA", "course_id": cid}).json()["id"]

        resp = client.post(f"/api/v1/assignments/{aid}/questions", headers=auth_header(tok), json={
            "title": "Q", "function_name": "f", "hidden_tests": "def test(): assert True",
            "public_cases": [], "signature": "def f()",
        })
        assert resp.status_code in (200, 201), f"创建失败: {resp.status_code} {resp.text}"
        qid = resp.json()["id"]

        # 验证实际 grading_mode
        cfg = client.get(f"/api/v1/ai-grading/questions/assignment/{qid}/config",
                         headers=auth_header(tok))
        assert cfg.status_code == 200
        assert cfg.json()["grading_mode"] == "active", \
            f"新建题应为 active: {cfg.json()}"

    def test_assignment_explicit_legacy_stays_legacy(self, client, db_session_factory):
        """显式传 legacy → 保持 legacy"""
        from conftest import create_user, login, auth_header
        create_user(db_session_factory, "gel_t", "teacher")
        tok, _ = login(client, "gel_t")

        cid = client.post("/api/v1/courses", headers=auth_header(tok),
                          json={"title": "GL"}).json()["id"]
        aid = client.post("/api/v1/assignments", headers=auth_header(tok),
                          json={"title": "GA", "course_id": cid}).json()["id"]

        resp = client.post(f"/api/v1/assignments/{aid}/questions", headers=auth_header(tok), json={
            "title": "Q", "function_name": "f", "hidden_tests": "def test(): pass",
            "public_cases": [], "signature": "def f()", "grading_mode": "legacy",
        })
        assert resp.status_code in (200, 201)
        qid = resp.json()["id"]

        cfg = client.get(f"/api/v1/ai-grading/questions/assignment/{qid}/config",
                         headers=auth_header(tok))
        assert cfg.json()["grading_mode"] == "legacy"

    def test_exam_code_question_defaults_to_active(self, client, db_session_factory):
        """考试编程题不传 grading_mode → 默认 active"""
        from conftest import create_user, login, auth_header
        create_user(db_session_factory, "ecd_t", "teacher")
        create_user(db_session_factory, "ecd_s", "student")
        tok, _ = login(client, "ecd_t")

        cid = client.post("/api/v1/courses", headers=auth_header(tok),
                          json={"title": "EC"}).json()["id"]
        now = datetime.now(timezone.utc)
        eid = client.post("/api/v1/exams", headers=auth_header(tok), json={
            "course_id": cid, "title": "EE", "duration_minutes": 60,
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        }).json()["id"]

        resp = client.post(f"/api/v1/exams/{eid}/questions", headers=auth_header(tok), json={
            "question_type": "code", "prompt": "Q", "points": 10,
            "hidden_tests": "def test(): assert True", "correct_answer": {},
        })
        assert resp.status_code == 201
        qid = resp.json()["id"]

        cfg = client.get(f"/api/v1/ai-grading/questions/exam/{qid}/config",
                         headers=auth_header(tok))
        assert cfg.json()["grading_mode"] == "active", \
            f"考试编程题应为 active: {cfg.json()}"

    def test_exam_choice_question_stays_legacy(self, client, db_session_factory):
        """选择题始终 legacy（不触发代码题默认 active 逻辑）"""
        from conftest import create_user, login, auth_header
        create_user(db_session_factory, "ech_t", "teacher")
        tok, _ = login(client, "ech_t")

        cid = client.post("/api/v1/courses", headers=auth_header(tok),
                          json={"title": "ECH"}).json()["id"]
        now = datetime.now(timezone.utc)
        eid = client.post("/api/v1/exams", headers=auth_header(tok), json={
            "course_id": cid, "title": "ECH", "duration_minutes": 60,
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        }).json()["id"]
        resp = client.post(f"/api/v1/exams/{eid}/questions", headers=auth_header(tok), json={
            "question_type": "single_choice", "prompt": "Q", "points": 5,
            "correct_answer": {"correct": ["A"]}, "options": {"A": "1", "B": "2"},
        })
        assert resp.status_code == 201
        qid = resp.json()["id"]

        cfg = client.get(f"/api/v1/ai-grading/questions/exam/{qid}/config",
                         headers=auth_header(tok))
        # 选择题 grading_mode 保持 DB 默认 legacy
        assert cfg.json()["grading_mode"] == "legacy"

    def test_invalid_grading_mode_rejected_by_literal(self, client, db_session_factory):
        """非法的 grading_mode 被 Literal 类型拒绝（422）"""
        from conftest import create_user, login, auth_header
        create_user(db_session_factory, "igr_t", "teacher")
        tok, _ = login(client, "igr_t")

        cid = client.post("/api/v1/courses", headers=auth_header(tok),
                          json={"title": "IG"}).json()["id"]
        aid = client.post("/api/v1/assignments", headers=auth_header(tok),
                          json={"title": "GA", "course_id": cid}).json()["id"]

        resp = client.post(f"/api/v1/assignments/{aid}/questions", headers=auth_header(tok), json={
            "title": "Q", "function_name": "f", "hidden_tests": "def test(): pass",
            "public_cases": [], "signature": "def f()", "grading_mode": "invalid_mode",
        })
        assert resp.status_code == 422, f"非法模式应 422: {resp.status_code} {resp.text}"


# ═══════════════════════════════════════════════════════════════
# 2. Docker 异常不扣分 + requeue_stale_jobs score=None
# ═══════════════════════════════════════════════════════════════

class TestSystemErrorNoScore:
    """基础设施异常→score=None，不 finalize，不创建错误 CodeGrade"""

    def test_legacy_docker_exception_score_none(self, db_session_factory):
        """legacy 作业 Docker 异常→score=None（不扣分）"""
        from app.models import JudgeQuestion, Submission
        from app.worker.judge_worker import _legacy_judge_submission, _make_work_dir
        from app.config import Settings
        from pathlib import Path
        import tempfile

        assignment_id = make_assignment(db_session_factory)
        student = make_student(db_session_factory)
        with db_session_factory() as db:
            q = JudgeQuestion(assignment_id=assignment_id, title="Q", function_name="f",
                            hidden_tests="def test(): assert True",
                            grading_mode="legacy", test_groups=[])
            db.add(q); db.flush()
            sub = Submission(question_id=q.id, student_id=student.id, code="def f(): pass",
                           status="queued", grading_status="running")
            db.add(sub); db.commit()

            settings = Settings(_env_file=None, judge_use_docker=True)
            tmp = tempfile.TemporaryDirectory(prefix="dai-test-")
            wd = Path(tmp.name)
            (wd / "user_code.py").write_text("def f(): pass")

            grant_judge_claim(db, Submission, sub.id)
            with patch("app.worker.judge_worker._run_docker_pytest",
                      side_effect=Exception("Docker daemon not available")):
                result = _legacy_judge_submission(db, None, settings, sub, q, wd, wd, 5, 256)

            assert result.status == "system_error"
            assert result.score is None, f"Docker 异常不应扣分: {result.score}"
            tmp.cleanup()

    def test_shadow_exam_docker_exception_score_none(self, db_session_factory):
        """shadow 考试 Docker 异常→score=None, fail_job retryable"""
        from app.models import ExamQuestion, ExamSubmission, ExamAnswer
        from app.worker.judge_worker import process_exam_answer
        from app.config import Settings
        import tempfile
        from pathlib import Path

        exam_id = make_exam(db_session_factory)
        student = make_student(db_session_factory)
        with db_session_factory() as db:
            eq = ExamQuestion(exam_id=exam_id, question_type="code", prompt="Q",
                            correct_answer={"test_file": ""}, points=10,
                            grading_mode="shadow",
                            test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,
                                         "tests":"def test(): assert True"}])
            db.add(eq); db.flush()
            es = ExamSubmission(exam_id=exam_id, student_id=student.id, status="grading")
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="def f(): pass",
                          grading_status="queued")
            db.add(ea); db.commit()
            aid = ea.id

            settings = Settings(_env_file=None, judge_use_docker=True, judge_timeout_seconds=5)

            # Mock Docker 异常（shadow 路径 legacy test 阶段）
            with patch("app.worker.judge_worker._run_docker_pytest",
                      side_effect=Exception("Docker crash")):
                result = process_exam_answer(db, None, settings, aid)

            assert result is not None
            # Docker 异常→ fail_job retryable，score=None
            assert result.score is None, f"shadow 考试 Docker 异常不应扣分: {result.score}"
            assert result.system_error is not None

    def test_requeue_stale_jobs_max_retries_score_none(self, db_session_factory):
        """超过最大重试→system_error 但 score=None"""
        from app.models import Submission
        from app.services.judge_queue import requeue_stale_jobs, MAX_ATTEMPTS

        assignment_id = make_assignment(db_session_factory)
        student = make_student(db_session_factory)
        with db_session_factory() as db:
            from app.models import JudgeQuestion
            q = JudgeQuestion(assignment_id=assignment_id, title="Q", function_name="f",
                            hidden_tests="def test(): assert True",
                            grading_mode="legacy", test_groups=[])
            db.add(q); db.flush()
            sub = Submission(question_id=q.id, student_id=student.id, code="def f(): pass",
                           status="pending", grading_status="pending",
                           attempt_count=MAX_ATTEMPTS)
            db.add(sub); db.commit()

            stats = requeue_stale_jobs(db, job_type="assignment", stale_pending_seconds=0)
            assert stats["max_retries_reached"] >= 1

            # 验证 score=None（不扣分）
            db.refresh(sub)
            assert sub.grading_status == "system_error"
            assert sub.score is None, f"max retries 不应扣分: {sub.score}"


# ═══════════════════════════════════════════════════════════════
# 3. override_grade scaled_score 始终重算
# ═══════════════════════════════════════════════════════════════

class TestOverrideScaledScore:
    """教师覆盖→scaled_score 始终按 ExamQuestion.points 重算"""

    def test_override_final_score_preserves_raw_total(self, client, db_session_factory):
        """教师只改最终分时，原始分仍保持 AI 分项合计"""
        from conftest import create_user, login, auth_header
        teacher = create_user(db_session_factory, "orp_t", "teacher")
        student = create_user(db_session_factory, "orp_s", "student")

        with db_session_factory() as db:
            from app.models import Course, Exam, ExamQuestion, ExamSubmission, ExamAnswer, CodeGrade, QuestionRubric
            course = Course(title="ORP", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            exam = Exam(course_id=course.id, title="ORP exam", status="published", duration_minutes=60)
            db.add(exam); db.flush()
            eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=20, grading_mode="active")
            db.add(eq); db.flush()
            now = datetime.now(timezone.utc)
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="orp", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=now)
            db.add(rub); db.flush()
            es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=4.8)
            db.add(ea); db.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                          status="review_required", functional_score=0, robustness_score=0,
                          algorithm_score=14, quality_score=10, raw_total=24,
                          final_score_100=24, scaled_score=4.8)
            db.add(cg)
            grant_teaching_fixture(db, course, student_ids=[student.id], bind_tasks=True)
            db.commit()
            grade_id = cg.id

        tok, _ = login(client, "orp_t")
        resp = client.post(f"/api/v1/ai-grading/grades/{grade_id}/override",
                          headers=auth_header(tok),
                          json={"reason": "调整最终分", "final_score_100": 100})
        assert resp.status_code == 200, f"覆盖应成功: {resp.status_code} {resp.text}"

        with db_session_factory() as db:
            cg_check = db.get(CodeGrade, grade_id)
            assert cg_check.raw_total == 24
            assert cg_check.final_score_100 == 100

    def test_override_final_score_recalculates_scaled(self, client, db_session_factory):
        """传 final_score_100 时 scaled_score 被正确重算"""
        from conftest import create_user, login, auth_header
        teacher = create_user(db_session_factory, "oss_t", "teacher")
        student = create_user(db_session_factory, "oss_s", "student")

        with db_session_factory() as db:
            from app.models import Course, Exam, ExamQuestion, ExamSubmission, ExamAnswer, CodeGrade, QuestionRubric
            course = Course(title="COS", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            exam = Exam(course_id=course.id, title="EOS", status="published", duration_minutes=60)
            db.add(exam); db.flush()
            eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=30, grading_mode="active")
            db.add(eq); db.flush()
            now = datetime.now(timezone.utc)
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=now)
            db.add(rub); db.flush()
            es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=15)  # 原来的折算分
            db.add(ea); db.flush()
            # 旧分数: raw=60, scaled=18 (60/100*30)
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                          status="review_required", functional_score=60, robustness_score=10,
                          algorithm_score=10, quality_score=5, raw_total=85,
                          final_score_100=85, scaled_score=25.5)
            db.add(cg)
            grant_teaching_fixture(db, course, student_ids=[student.id], bind_tasks=True)
            db.commit()
            gid = cg.id

        tok, _ = login(client, "oss_t")
        # 覆盖最终分为 100
        resp = client.post(f"/api/v1/ai-grading/grades/{gid}/override",
                          headers=auth_header(tok),
                          json={"reason": "重算验证", "final_score_100": 100})
        assert resp.status_code == 200, f"覆盖应成功: {resp.status_code} {resp.text}"

        with db_session_factory() as db:
            cg_check = db.get(CodeGrade, gid)
            # 30 分题，100% → scaled = 30
            assert cg_check.final_score_100 == 100
            assert cg_check.scaled_score == 30.0, \
                f"scaled 应为 30 (100/100*30): {cg_check.scaled_score}"
            # ExamAnswer.score 也更新为 scaled_score
            ea_check = db.get(ExamAnswer, cg_check.exam_answer_id)
            assert ea_check.score == 30.0

    def test_override_changes_scaled_from_old_value(self, client, db_session_factory):
        """原分与新分不同，断言 scaled 确实变了"""
        from conftest import create_user, login, auth_header
        teacher = create_user(db_session_factory, "osc2_t", "teacher")
        student = create_user(db_session_factory, "osc2_s", "student")

        with db_session_factory() as db:
            from app.models import Course, Exam, ExamQuestion, ExamSubmission, ExamAnswer, CodeGrade, QuestionRubric
            course = Course(title="CO2", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            exam = Exam(course_id=course.id, title="EO2", status="published", duration_minutes=60)
            db.add(exam); db.flush()
            eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=20, grading_mode="active")
            db.add(eq); db.flush()
            now = datetime.now(timezone.utc)
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=now)
            db.add(rub); db.flush()
            es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=10)  # 旧折算分=10
            db.add(ea); db.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                          status="review_required", functional_score=60, robustness_score=10,
                          algorithm_score=10, quality_score=5, raw_total=85,
                          final_score_100=50, scaled_score=10.0)  # 50/100*20=10
            db.add(cg)
            grant_teaching_fixture(db, course, student_ids=[student.id], bind_tasks=True)
            db.commit()
            gid = cg.id

        tok, _ = login(client, "osc2_t")
        # 改到 90%
        resp = client.post(f"/api/v1/ai-grading/grades/{gid}/override",
                          headers=auth_header(tok),
                          json={"reason": "改到90%", "final_score_100": 90})
        assert resp.status_code == 200

        with db_session_factory() as db:
            cg_check = db.get(CodeGrade, gid)
            assert cg_check.scaled_score == 18.0, \
                f"scaled 应为 18 (90/100*20): {cg_check.scaled_score}"
            assert cg_check.scaled_score != 10.0, "scaled 必须从旧值 10 变为 18"


# ═══════════════════════════════════════════════════════════════
# 4. admin _build_grade_base_query 筛选正确 JOIN
# ═══════════════════════════════════════════════════════════════

class TestAdminGradeQuery:
    """admin 查询时按需 JOIN，不产生笛卡尔积或 SQL 错误"""

    def test_admin_assignment_filter_works(self, db_session_factory):
        """admin + kind=assignment + question_id 筛选"""
        from app.models import Assignment, Course, JudgeQuestion, Submission, CodeGrade, QuestionRubric, User
        from app.api.ai_grading import _build_grade_base_query

        student = make_student(db_session_factory, username="aqa_s")
        with db_session_factory() as db:
            admin = User(username="aqa", real_name="AQA", role="admin",
                        status="active", password_hash="x")
            db.add(admin); db.flush()
            c = Course(title="AC", status="published", teacher_id=admin.id)
            db.add(c); db.flush()
            a = Assignment(title="AA", course_id=c.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,
                                          "tests":"def test(): assert True"}])
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
            db.add(cg); db.commit()

            query, count_q = _build_grade_base_query(db, admin, kind="assignment",
                                                     question_id=q.id, student_id=None, status=None)
            grades = db.scalars(query).all()
            total = db.scalar(count_q)
            assert len(grades) == 1, f"应返回 1 条: {len(grades)}"
            assert total == 1

    def test_admin_exam_filter_works(self, db_session_factory):
        """admin + kind=exam + student_id 筛选"""
        from app.models import Course, Exam, ExamQuestion, ExamSubmission, ExamAnswer, CodeGrade, QuestionRubric, User
        from app.api.ai_grading import _build_grade_base_query

        s55 = make_student(db_session_factory, username="aqe_s")
        with db_session_factory() as db:
            admin = User(username="aqe", real_name="AQE", role="admin",
                        status="active", password_hash="x")
            db.add(admin); db.flush()
            c = Course(title="AEC", status="published", teacher_id=admin.id)
            db.add(c); db.flush()
            e = Exam(course_id=c.id, title="AE", status="published", duration_minutes=60)
            db.add(e); db.flush()
            eq = ExamQuestion(exam_id=e.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=10, grading_mode="active")
            db.add(eq); db.flush()
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            es = ExamSubmission(exam_id=e.id, student_id=s55.id, status="grading")
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=5)
            db.add(ea); db.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                          status="completed", functional_score=60, robustness_score=10)
            db.add(cg); db.commit()

            query, count_q = _build_grade_base_query(db, admin, kind="exam",
                                                     question_id=None, student_id=s55.id, status=None)
            grades = db.scalars(query).all()
            total = db.scalar(count_q)
            assert len(grades) == 1
            assert total == 1

    def test_admin_no_kind_question_filter(self, db_session_factory):
        """admin 无 kind + question_id 筛选不报错"""
        from app.models import Assignment, Course, JudgeQuestion, Submission, CodeGrade, QuestionRubric, User
        from app.api.ai_grading import _build_grade_base_query

        student = make_student(db_session_factory, username="ank_s")
        with db_session_factory() as db:
            admin = User(username="ank", real_name="ANK", role="admin",
                        status="active", password_hash="x")
            db.add(admin); db.flush()
            c = Course(title="ANKC", status="published", teacher_id=admin.id)
            db.add(c); db.flush()
            a = Assignment(title="ANKA", course_id=c.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,
                                          "tests":"def test(): assert True"}])
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
            db.add(cg); db.commit()

            # 无 kind 筛选：不应抛 SQL 错误
            query, count_q = _build_grade_base_query(db, admin, kind=None,
                                                     question_id=q.id, student_id=None, status=None)
            grades = db.scalars(query).all()
            assert len(grades) >= 1

    def test_admin_assignment_count_matches_items(self, db_session_factory):
        """admin count 与 items 数量一致"""
        from app.models import Assignment, Course, JudgeQuestion, Submission, CodeGrade, QuestionRubric, User
        from app.api.ai_grading import _build_grade_base_query

        students = [make_student(db_session_factory, username=f"acm_s{i}") for i in range(3)]
        with db_session_factory() as db:
            admin = User(username="acm", real_name="ACM", role="admin",
                        status="active", password_hash="x")
            db.add(admin); db.flush()
            c = Course(title="ACMC", status="published", teacher_id=admin.id)
            db.add(c); db.flush()
            a = Assignment(title="ACMA", course_id=c.id, status="published")
            db.add(a); db.flush()
            q = JudgeQuestion(assignment_id=a.id, title="Q", function_name="f",
                             hidden_tests="def test(): pass", grading_mode="active",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,
                                          "tests":"def test(): assert True"}])
            db.add(q); db.flush()
            rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()

            # 创建 3 个 submissions（3 个真实学生）
            for s in students:
                sub = Submission(question_id=q.id, student_id=s.id, code="def f(): pass",
                                status="graded", grading_status="completed", score=80)
                db.add(sub); db.flush()
                cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                              status="completed", functional_score=60, robustness_score=10)
                db.add(cg)
            db.commit()

            query, count_q = _build_grade_base_query(db, admin, kind="assignment",
                                                     question_id=None, student_id=None, status=None)
            grades = db.scalars(query).all()
            total = db.scalar(count_q)
            assert len(grades) == 3
            assert total == 3, f"count({total}) != items({len(grades)})"


# ═══════════════════════════════════════════════════════════════
# 7. 影子模式学生不可见 AI 数据 + system_error score=None
# ═══════════════════════════════════════════════════════════════

class TestShadowNoLeak:
    """shadow 学生不泄露 AI 数据"""

    def test_shadow_exam_my_grade_no_breakdown(self, client, db_session_factory):
        """shadow 考试 my-grade 不含 grading_breakdown"""
        from conftest import create_user, login, auth_header
        create_user(db_session_factory, "snl_t", "teacher")
        create_user(db_session_factory, "snl_s", "student")

        with db_session_factory() as db:
            from app.models import Course, CourseEnrollment, Exam, ExamQuestion, ExamSubmission, ExamAnswer, CodeGrade, QuestionRubric, User
            t = db.query(User).filter(User.username == "snl_t").first()
            s = db.query(User).filter(User.username == "snl_s").first()
            c = Course(title="SNL", status="published", teacher_id=t.id)
            db.add(c); db.flush()
            db.add(CourseEnrollment(course_id=c.id, student_id=s.id, status="enrolled"))
            e = Exam(course_id=c.id, title="SNL", status="published", duration_minutes=60)
            db.add(e); db.flush()
            eq = ExamQuestion(exam_id=e.id, question_type="code", prompt="t",
                             correct_answer={"test_file":""}, points=10, grading_mode="shadow",
                             test_groups=[{"id":"F1","name":"F","dimension":"F","max_score":60,
                                          "tests":"def test(): assert True"}])
            db.add(eq); db.flush()
            es = ExamSubmission(exam_id=e.id, student_id=s.id, status="graded", score=10)
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="completed", score=10)
            db.add(ea); db.flush()
            # shadow CodeGrade（教师可见，学生不可见）
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                source_hash="a", source_snapshot={}, rubric_json={},
                                model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="shadow",
                          status="completed", functional_score=60, robustness_score=10,
                          algorithm_score=18, quality_score=8,
                          ai_result={"student_feedback": {"strengths":["good"],"issues":[],"suggestions":[]}})
            db.add(cg)
            grant_teaching_fixture(db, c, student_ids=[s.id], bind_tasks=True)
            db.commit()

        tok, _ = login(client, "snl_s")
        resp = client.get(f"/api/v1/exams/{e.id}/my-grade", headers=auth_header(tok))
        assert resp.status_code == 200
        data = resp.json()

        # shadow 答案不应有 grading_breakdown
        for ans in data.get("answers", []):
            assert "grading_breakdown" not in ans, \
                f"shadow 答案泄露 AI 数据: {ans}"


class TestSystemErrorNotCounted:
    """system_error answer 不参与 finalize"""

    def test_system_error_blocks_exam_finalize(self, db_session_factory):
        """system_error 答案 → 父转 review_required，不按 0 分结算（公平性保留）"""
        from app.services.exam_grading import finalize_if_ready, FinalizeOutcome
        from app.models import ExamAnswer, ExamGrade, ExamSubmission

        with db_session_factory() as db:
            from app.models import Exam, ExamQuestion
            course_id = make_course(db_session_factory)
            student = make_student(db_session_factory)
            e = Exam(course_id=course_id, title="SE", status="published", duration_minutes=60)
            db.add(e); db.flush()
            eq = ExamQuestion(exam_id=e.id, question_type="code", prompt="t",
                             correct_answer={"test_file": ""}, points=10, grading_mode="legacy")
            db.add(eq); db.flush()
            es = ExamSubmission(exam_id=e.id, student_id=student.id, status="grading")
            db.add(es); db.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                           grading_status="system_error", score=None)  # system_error + score=None
            db.add(ea); db.commit()

            result = finalize_if_ready(es.id, db)
            assert result.outcome == FinalizeOutcome.REVIEW_REQUIRED, \
                f"system_error 答案应转 review_required: {result}"

            db.expire_all()  # finalize 用条件 UPDATE，不依赖会话同步
            es2 = db.get(ExamSubmission, es.id)
            assert es2.status == "review_required", "父应转 review_required"
            assert es2.review_required_at is not None

            # 公平性：不得创建 ExamGrade，不得按 0 分结算
            grades = db.query(ExamGrade).filter(ExamGrade.exam_id == e.id).all()
            assert len(grades) == 0, "system_error 不得创建 ExamGrade"
