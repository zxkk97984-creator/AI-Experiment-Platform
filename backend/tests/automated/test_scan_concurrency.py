"""双 scanner 并发——每个 stale job 同一轮最多一次 CAS 成功、最多一次 rpush、统计准确"""
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models import CodeGrade, ExamAnswer, ExamQuestion, ExamSubmission, Submission
from app.services.judge_queue import claim_job, enqueue_job, requeue_stale_jobs


def _setup_stale_queued_submission(db_session_factory):
    """创建 queued 超时的作业提交"""
    with db_session_factory() as db:
        from app.models import Assignment, Course, JudgeQuestion, User
        teacher = User(username="cc_t", real_name="CCT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="cc_s", real_name="CCS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="CCC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        assignment = Assignment(course_id=course.id, title="CCA", status="published")
        db.add(assignment); db.flush()
        q = JudgeQuestion(assignment_id=assignment.id, title="CCQ", function_name="f",
                          hidden_tests="assert True", public_cases=[])
        db.add(q); db.flush()
        sub = Submission(question_id=q.id, student_id=student.id,
                         code="def f(): pass", status="queued", grading_status="pending")
        db.add(sub); db.commit()
        return sub.id


def _setup_stale_running_ai_grade(db_session_factory):
    """创建 running 超时的 CodeGrade（作业路径）"""
    from app.services.ai_grading_queue import STALE_RUNNING_SECONDS
    with db_session_factory() as db:
        from app.models import Assignment, Course, JudgeQuestion, QuestionRubric, User
        teacher = User(username="ca_t", real_name="CAT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="ca_s", real_name="CAS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="CAC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        assignment = Assignment(course_id=course.id, title="CAA", status="published")
        db.add(assignment); db.flush()
        q = JudgeQuestion(assignment_id=assignment.id, title="CAQ", function_name="f",
                          hidden_tests="assert True", public_cases=[],
                          grading_mode="active",
                          test_groups=[{"id": "F1", "name": "F", "dimension": "F",
                                        "max_score": 60, "tests": "def test(): pass"}])
        db.add(q); db.flush()
        rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                             source_hash="h", source_snapshot={}, rubric_json={},
                             model_name="m", locked_at=datetime.now(timezone.utc))
        db.add(rub); db.flush()
        sub = Submission(question_id=q.id, student_id=student.id,
                         code="def f(): pass", status="running", grading_status="running")
        db.add(sub); db.commit()
        cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                       status="running", functional_score=60, robustness_score=10,
                       # 明确的过去时间：按实际阈值回拨（原先硬编码 900s 小于 1800s 阈值，
                       # 根本没进入恢复分支），避免依赖"0 秒立即过期"的边界。
                       started_at=datetime.now(timezone.utc)
                       - timedelta(seconds=STALE_RUNNING_SECONDS + 600))
        db.add(cg); db.commit()
        return cg.id


class TestConcurrentJudgeRecovery:
    """两个实例并发 requeue_stale_jobs：同一 stale job 一轮最多一次推送"""

    def test_stale_queued_repushed_once(self, db_session_factory):
        sid = _setup_stale_queued_submission(db_session_factory)

        # 入队并让 queued_at 超时
        with db_session_factory() as db:
            enqueue_job(db, job_type="assignment", object_id=sid)
            sub = db.get(Submission, sid)
            sub.queued_at = datetime.now(timezone.utc) - timedelta(seconds=300)
            db.commit()

        results = []
        barrier = threading.Barrier(2, timeout=5)
        errors = []
        # patch 栈是进程级的、非线程局部：两线程各自 with patch 同一目标时，
        # rpush 会随机落到对方 mock 上导致计数错位（MySQL 时序下已复现）。
        # 改为外层单一 patch + 共享 mock，双线程同时可见。
        shared_redis = MagicMock()

        def do_scan(instance):
            try:
                with db_session_factory() as db:
                    barrier.wait()
                    stats = requeue_stale_jobs(db, job_type="assignment",
                                               stale_queued_seconds=120)
                    results.append((instance, stats))
            except Exception as e:
                errors.append((instance, str(e)))

        with patch("app.services.judge_queue._get_redis", return_value=shared_redis):
            t1 = threading.Thread(target=do_scan, args=("s1",))
            t2 = threading.Thread(target=do_scan, args=("s2",))
            t1.start(); t2.start()
            t1.join(timeout=10); t2.join(timeout=10)

        assert len(errors) == 0, f"并发恢复出错: {errors}"
        total = sum(s["queued_repushed"] for _, s in results)
        assert total == 1, f"同一轮最多一次重新推送: {results}"
        assert shared_redis.rpush.call_count == 1, \
            f"rpush 最多一次: {shared_redis.rpush.call_count}"


class TestAIFinalizeOnReviewRequired:
    """active AI 终态（fail_ai_grade / worker review_required）→ 父级当场 review_required"""

    def _seed_active_exam_codegrade(self, db_session_factory, cg_status="running"):
        """建考试 active 路径：答案 completed(score=None) + running CodeGrade"""
        from app.models import Course, Exam, QuestionRubric, User
        with db_session_factory() as db:
            teacher = User(username="af_t", real_name="AFT", role="teacher", status="active",
                           password_hash="x")
            student = User(username="af_s", real_name="AFS", role="student", status="active",
                           password_hash="x")
            db.add_all([teacher, student]); db.flush()
            course = Course(title="AFC", status="published", teacher_id=teacher.id)
            db.add(course); db.flush()
            exam = Exam(course_id=course.id, title="AFE", status="published", duration_minutes=60)
            db.add(exam); db.flush()
            eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                              correct_answer={}, points=10, grading_mode="active",
                              test_groups=[{"id": "F1", "name": "F", "dimension": "F",
                                            "max_score": 60, "tests": "def test(): pass"}])
            db.add(eq); db.flush()
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                 source_hash="h", source_snapshot={}, rubric_json={},
                                 model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
            db.add(sub); db.flush()
            ans = ExamAnswer(submission_id=sub.id, question_id=eq.id,
                             code_answer="def f(): pass", grading_status="completed", score=None)
            db.add(ans); db.flush()
            cg = CodeGrade(exam_answer_id=ans.id, rubric_id=rub.id, mode="active",
                           status=cg_status, functional_score=60, robustness_score=10)
            db.add(cg); db.commit()
            return {"submission_id": sub.id, "cg_id": cg.id}

    def test_fail_ai_grade_review_required_triggers_parent(self, db_session_factory):
        """fail_ai_grade 达上限转 review_required → 父级当场转 review_required"""
        from app.services.ai_grading_queue import claim_ai_grade, fail_ai_grade

        ctx = self._seed_active_exam_codegrade(db_session_factory, cg_status="queued")

        with db_session_factory() as db:
            cg = db.get(CodeGrade, ctx["cg_id"])
            cg.attempt_count = 3  # 已达上限
            db.commit()

        with db_session_factory() as db:
            # 失败写入受认领栅栏保护：必须先认领（真实 Worker 序列）
            assert claim_ai_grade(db, ctx["cg_id"])
            fail_ai_grade(db, MagicMock(), ctx["cg_id"], "AI 服务超时",
                          retryable=True, max_attempts=3)
            db.expire_all()
            cg = db.get(CodeGrade, ctx["cg_id"])
            assert cg.status == "review_required"
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "review_required", \
                f"fail_ai_grade 终态应立即转父 review_required: {sub.status}"
            assert sub.review_required_at is not None

    def test_worker_failure_triggers_parent_review_required(self, db_session_factory):
        """worker 处理不可恢复失败 → fail_ai_grade 终态 → 父级当场 review_required"""
        from app.services.ai_client import AIServiceError
        from app.worker.judge_worker import process_ai_grade

        ctx = self._seed_active_exam_codegrade(db_session_factory, cg_status="queued")

        settings = MagicMock()
        with db_session_factory() as db:
            # grade_code_submission 在 process_ai_grade 内 import，patch 其来源模块
            with patch("app.services.ai_grading_service.grade_code_submission",
                       side_effect=AIServiceError("AI_UNAVAILABLE", "AI 服务不可用", retryable=False)):
                result = process_ai_grade(db, MagicMock(), settings, ctx["cg_id"])
                assert result is None  # 失败路径返回 None

            db.expire_all()
            cg = db.get(CodeGrade, ctx["cg_id"])
            assert cg.status == "review_required", \
                f"不可恢复失败应转 review_required: {cg.status}"
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "review_required", \
                f"worker 失败终态应触发父转换: {sub.status}"


class TestConcurrentAIRecovery:
    """两个实例并发 recover_stale_ai_grades：running 恢复最多一次 rpush"""

    def test_stale_running_recovered_once(self, db_session_factory):
        from app.services.ai_grading_queue import recover_stale_ai_grades

        cg_id = _setup_stale_running_ai_grade(db_session_factory)
        results = []
        push_counts = []
        barrier = threading.Barrier(2, timeout=5)
        errors = []

        def do_recover(instance):
            try:
                with db_session_factory() as db:
                    mock_r = MagicMock()
                    barrier.wait()
                    stats = recover_stale_ai_grades(db, mock_r)
                    results.append((instance, stats))
                    push_counts.append(mock_r.rpush.call_count)
            except Exception as e:
                errors.append((instance, str(e)))

        t1 = threading.Thread(target=do_recover, args=("w1",))
        t2 = threading.Thread(target=do_recover, args=("w2",))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert len(errors) == 0, f"并发 AI 恢复出错: {errors}"
        total_running = sum(s["running"] for _, s in results)
        assert total_running == 1, f"running 恢复最多一次: {results}"
        assert sum(push_counts) == 1, f"AI rpush 最多一次: {push_counts}"

        with db_session_factory() as db:
            cg = db.get(CodeGrade, cg_id)
            assert cg.status == "queued"
