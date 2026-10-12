"""过期自动交卷并发、submitted 崩溃恢复、grading 扫描真实 metrics"""
import threading
from datetime import datetime, timedelta, timezone

from app.models import ExamAnswer, ExamGrade, ExamQuestion, ExamSubmission
from app.services.exam_service import scan_expired_exams, submit_exam
from conftest import create_user


def _seed_exam(db_session_factory, *, q1_type="single_choice", q2_type="code",
               q2_hidden_tests="assert True", sub_status="started", expired=False):
    """建一场考试：题1 选择题(10)、题2 代码题(20)，提交在指定状态"""
    with db_session_factory() as db:
        from app.models import Course, Exam, User
        teacher = User(username="sc_t", real_name="SCT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="sc_s", real_name="SCS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="SCC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="SCE", status="published", duration_minutes=60)
        db.add(exam); db.flush()
        q1 = ExamQuestion(exam_id=exam.id, question_type=q1_type, prompt="Q1",
                          correct_answer={"correct": ["A"]}, options={"A": "x", "B": "y"},
                          points=10, grading_mode="legacy")
        q2 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q2",
                          correct_answer={}, points=20, hidden_tests=q2_hidden_tests,
                          grading_mode="legacy")
        db.add_all([q1, q2]); db.flush()
        now = datetime.now(timezone.utc)
        sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status=sub_status,
                             started_at=now,
                             expires_at=now - timedelta(minutes=5) if expired else now + timedelta(minutes=30),
                             grading_version_map={
                                 str(q1.id): {"version_id": None, "points": 10.0},
                                 str(q2.id): {"version_id": None, "points": 20.0},
                             })
        if sub_status in ("submitted", "grading", "graded"):
            sub.submitted_at = now - timedelta(minutes=10)
        db.add(sub); db.flush()
        ans1 = ExamAnswer(submission_id=sub.id, question_id=q1.id,
                          selected_options=["A"], grading_status="pending")
        ans2 = ExamAnswer(submission_id=sub.id, question_id=q2.id,
                          code_answer="def f(): pass", grading_status="pending")
        db.add_all([ans1, ans2]); db.commit()
        return {"submission_id": sub.id, "exam_id": exam.id, "ans1_id": ans1.id, "ans2_id": ans2.id}


class TestExpiredClaimConcurrency:
    """双实例竞争：同一过期提交只有一个实例认领"""

    def test_concurrent_expired_claim_single_win(self, db_session_factory):
        ctx = _seed_exam(db_session_factory, expired=True, sub_status="started")
        results = []
        barrier = threading.Barrier(2, timeout=5)
        errors = []

        def do_scan(instance):
            try:
                with db_session_factory() as db:
                    barrier.wait()
                    m = scan_expired_exams(db, datetime.now(timezone.utc))
                    results.append((instance, m))
            except Exception as e:
                import traceback
                errors.append((instance, f"{e}\n{traceback.format_exc()}"))

        t1 = threading.Thread(target=do_scan, args=("api-1",))
        t2 = threading.Thread(target=do_scan, args=("api-2",))
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)

        assert len(errors) == 0, f"并发扫描出错: {errors}"
        total_claimed = sum(m["expired_claimed"] for _, m in results)
        total_auto = sum(m["auto_submitted"] for _, m in results)
        # 两个实例合计最多认领 1 次
        assert total_claimed == 1, f"同一过期提交只应被认领一次: {results}"
        assert total_auto == 1, f"自动交卷只应完成一次: {results}"

        with db_session_factory() as db:
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status in ("grading", "graded"), f"父应已自动交卷: {sub.status}"
            # 选择题已被评分
            ans1 = db.get(ExamAnswer, ctx["ans1_id"])
            assert ans1.grading_status == "completed"
            assert ans1.score == 10.0
            # 代码题已入队
            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            assert ans2.grading_status in ("queued", "pending", "completed")


class TestSubmittedRecovery:
    """submitted 崩溃恢复：准备函数幂等，只记一次真实转换"""

    def test_submitted_stale_recovery(self, db_session_factory):
        ctx = _seed_exam(db_session_factory, sub_status="submitted")

        with db_session_factory() as db:
            m = scan_expired_exams(db, datetime.now(timezone.utc))
            assert m["submitted_resumed"] == 1, f"应恢复 1 份 submitted: {m}"
            assert m["expired_claimed"] == 0

            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status in ("grading", "graded")

            ans1 = db.get(ExamAnswer, ctx["ans1_id"])
            assert ans1.grading_status == "completed"
            assert ans1.score == 10.0

        # 再次扫描：不再重复恢复
        with db_session_factory() as db:
            m2 = scan_expired_exams(db, datetime.now(timezone.utc))
            assert m2["submitted_resumed"] == 0, f"恢复应幂等: {m2}"


class TestScanMetrics:
    """grading 扫描只记录真实转换数，不记录候选数"""

    def test_graded_metric_on_complete(self, db_session_factory):
        ctx = _seed_exam(db_session_factory, sub_status="grading")
        with db_session_factory() as db:
            ans1 = db.get(ExamAnswer, ctx["ans1_id"])
            ans1.grading_status = "completed"; ans1.score = 10.0
            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            ans2.grading_status = "completed"; ans2.score = 20.0
            db.commit()

        with db_session_factory() as db:
            m = scan_expired_exams(db, datetime.now(timezone.utc))
            assert m["graded"] == 1, f"应记录 1 次真实 graded 转换: {m}"
            assert m["expired_claimed"] == 0

        with db_session_factory() as db:
            grades = db.query(ExamGrade).filter(ExamGrade.exam_id == ctx["exam_id"]).all()
            assert len(grades) == 1
            assert grades[0].score == 30.0

    def test_review_required_metric_on_system_error(self, db_session_factory):
        ctx = _seed_exam(db_session_factory, sub_status="grading")
        with db_session_factory() as db:
            ans1 = db.get(ExamAnswer, ctx["ans1_id"])
            ans1.grading_status = "completed"; ans1.score = 10.0
            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            ans2.grading_status = "system_error"; ans2.score = None
            db.commit()

        with db_session_factory() as db:
            m = scan_expired_exams(db, datetime.now(timezone.utc))
            assert m["review_required"] == 1, f"应记录 1 次 review_required 转换: {m}"

        with db_session_factory() as db:
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "review_required"

        # 再次扫描：已是终态，无重复转换
        with db_session_factory() as db:
            m2 = scan_expired_exams(db, datetime.now(timezone.utc))
            assert m2["review_required"] == 0, f"终态后不应重复计数: {m2}"

    def test_waiting_metric_no_transition(self, db_session_factory):
        """代码题仍在 pending：waiting 计数，父保持 grading，不误报自动交卷"""
        ctx = _seed_exam(db_session_factory, sub_status="grading")
        with db_session_factory() as db:
            ans1 = db.get(ExamAnswer, ctx["ans1_id"])
            ans1.grading_status = "completed"; ans1.score = 10.0
            db.commit()

        with db_session_factory() as db:
            m = scan_expired_exams(db, datetime.now(timezone.utc))
            assert m["waiting"] == 1, f"应记录 waiting: {m}"
            assert m["graded"] == 0
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "grading"

    def test_no_candidates_all_zero(self, db_session_factory):
        _seed_exam(db_session_factory, sub_status="started", expired=False)
        with db_session_factory() as db:
            m = scan_expired_exams(db, datetime.now(timezone.utc))
            assert all(v == 0 for v in m.values()), f"无候选应全 0: {m}"


class TestSubmitExamIdempotent:
    """手动交卷幂等：review_required 不自动重试，直接返回"""

    def test_submit_review_required_idempotent(self, db_session_factory):
        from app.models import Exam, User
        ctx = _seed_exam(db_session_factory, sub_status="review_required")

        with db_session_factory() as db:
            sub = db.get(ExamSubmission, ctx["submission_id"])
            exam = db.get(Exam, ctx["exam_id"])
            student = db.get(User, sub.student_id)

            returned = submit_exam(exam, student, db)
            assert returned.status == "review_required", "review_required 应幂等返回，不自动重试"
