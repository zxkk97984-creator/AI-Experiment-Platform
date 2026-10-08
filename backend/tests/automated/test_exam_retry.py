"""显式重试 review_required 考试提交——权限、配置前置校验、原子重置、幂等"""
from datetime import datetime, timezone

import pytest

from app.models import (
    CodeGrade, ExamAnswer, ExamGrade, ExamQuestion, ExamSubmission,
    QuestionRubric,
)
from app.services.exam_service import retry_exam_submission
from conftest import grant_teaching_fixture, auth_header, create_user, login


def _seed_review_required_submission(db_session_factory, *, q1_mode="legacy", q2_mode="legacy",
                                     with_rubric=False):
    """建一场考试：题1 completed(10)、题2 system_error(None)，父已 review_required"""
    with db_session_factory() as db:
        from app.models import Course, Exam, User
        teacher = User(username="rt_t", real_name="RTT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="rt_s", real_name="RTS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="RTC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="RTE", status="published", duration_minutes=60)
        db.add(exam); db.flush()
        q1 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q1",
                          correct_answer={}, points=10, hidden_tests="assert True",
                          grading_mode=q1_mode)
        q2 = ExamQuestion(exam_id=exam.id, question_type="code", prompt="Q2",
                          correct_answer={}, points=20, hidden_tests=None,
                          grading_mode=q2_mode)
        db.add_all([q1, q2]); db.flush()
        sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="review_required",
                             review_reason="存在系统错误答案",
                             review_required_at=datetime.now(timezone.utc))
        db.add(sub); db.flush()
        ans1 = ExamAnswer(submission_id=sub.id, question_id=q1.id,
                          code_answer="def a(): pass", grading_status="completed", score=10.0)
        ans2 = ExamAnswer(submission_id=sub.id, question_id=q2.id,
                          code_answer="def b(): pass", grading_status="system_error",
                          score=None, attempt_count=3, last_error="缺少隐藏测试",
                          system_error="缺少隐藏测试")
        db.add_all([ans1, ans2])
        rubric_id = None
        if with_rubric:
            rub = QuestionRubric(exam_question_id=q2.id, version=1, status="locked",
                                 source_hash="h", source_snapshot={}, rubric_json={},
                                 model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub); db.flush()
            rubric_id = rub.id
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
        return {"submission_id": sub.id, "exam_id": exam.id, "course_id": course.id,
                "ans1_id": ans1.id, "ans2_id": ans2.id, "q1_id": q1.id, "q2_id": q2.id,
                "teacher_id": teacher.id, "rubric_id": rubric_id}


def _make_actor(db_session_factory, username, role):
    from app.models import User
    with db_session_factory() as db:
        u = User(username=username, real_name=username, role=role, status="active",
                 password_hash="x")
        db.add(u); db.commit()
        return u


class TestRetryPreconditions:
    """配置前置校验：配置仍缺失时拒绝重试，不能形成第二轮无限失败"""

    def test_legacy_missing_hidden_tests_rejected(self, db_session_factory):
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        actor = _make_actor(db_session_factory, "rp_t", "teacher")

        with db_session_factory() as db:
            with pytest.raises(Exception) as exc:
                retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)
            assert "隐藏测试" in str(exc.value) or "CONFIG" in str(exc.value)

            # 状态未被改动
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "review_required"
            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            assert ans2.grading_status == "system_error"

    def test_shadow_active_missing_test_groups_rejected(self, db_session_factory):
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="active", q2_mode="active")
        actor = _make_actor(db_session_factory, "rp2_t", "teacher")

        with db_session_factory() as db:
            with pytest.raises(Exception) as exc:
                retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)
            assert "测试组" in str(exc.value) or "CONFIG" in str(exc.value)

    def test_shadow_active_missing_locked_rubric_rejected(self, db_session_factory):
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="active", q2_mode="active")
        actor = _make_actor(db_session_factory, "rp3_t", "teacher")

        with db_session_factory() as db:
            q2 = db.get(ExamQuestion, ctx["q2_id"])
            q2.test_groups = [{"id": "F1", "name": "F", "dimension": "F", "max_score": 60,
                               "tests": "def test(): assert True"}]
            db.commit()

            with pytest.raises(Exception) as exc:
                retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)
            assert "Rubric" in str(exc.value) or "CONFIG" in str(exc.value)


class TestRetryStateGuard:
    """状态守卫与原子重置"""

    def test_rejects_non_review_required(self, db_session_factory):
        ctx = _seed_review_required_submission(db_session_factory)
        actor = _make_actor(db_session_factory, "rs_t", "teacher")

        with db_session_factory() as db:
            sub = db.get(ExamSubmission, ctx["submission_id"])
            sub.status = "grading"  # 已被并发处理
            db.commit()

            with pytest.raises(Exception) as exc:
                retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)
            assert "review_required" in str(exc.value) or "状态" in str(exc.value)

    def test_rejects_non_system_error_answer(self, db_session_factory):
        ctx = _seed_review_required_submission(db_session_factory)
        actor = _make_actor(db_session_factory, "rs2_t", "teacher")

        with db_session_factory() as db:
            # ans1 是 completed，不能选中重试
            with pytest.raises(Exception) as exc:
                retry_exam_submission(ctx["submission_id"], [ctx["ans1_id"]], actor, db)
            assert "system_error" in str(exc.value) or "答案" in str(exc.value)

    def test_reset_selected_answer_and_parent(self, db_session_factory):
        """修复配置后：选中答案重置为 pending，父转 grading，review 字段清空"""
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        actor = _make_actor(db_session_factory, "rs3_t", "teacher")

        with db_session_factory() as db:
            # 修复配置（历史数据场景：教师补上 hidden_tests）
            q2 = db.get(ExamQuestion, ctx["q2_id"])
            q2.hidden_tests = "assert True"
            db.commit()

            sub = retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)

            assert sub.status == "grading"
            assert sub.review_reason is None
            assert sub.review_required_at is None

            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            # 重置为 pending 后服务立即统一入队 → queued，attempt 从 0 递增为 1
            assert ans2.grading_status == "queued", f"重试后应已入队: {ans2.grading_status}"
            assert ans2.attempt_count == 1
            assert ans2.queued_at is not None
            assert ans2.started_at is None
            assert ans2.finished_at is None
            assert ans2.last_error is None
            assert ans2.system_error is None
            assert ans2.result_details is None
            assert ans2.score is None  # 保持 NULL，等判题后定分

            # 未选中的答案不受影响
            ans1 = db.get(ExamAnswer, ctx["ans1_id"])
            assert ans1.grading_status == "completed"
            assert ans1.score == 10.0

    def test_retry_clears_stale_score_on_code_answer(self, db_session_factory):
        """code 分支重置必须显式清空 score：原 score 非 NULL 的 system_error 答案 → NULL"""
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        actor = _make_actor(db_session_factory, "rs5_t", "teacher")

        with db_session_factory() as db:
            q2 = db.get(ExamQuestion, ctx["q2_id"])
            q2.hidden_tests = "assert True"
            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            ans2.score = 15.0  # 历史数据残留旧分
            db.commit()

            retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)

            ans2b = db.get(ExamAnswer, ctx["ans2_id"])
            assert ans2b.grading_status == "queued"
            assert ans2b.score is None, f"重试必须清空旧分: {ans2b.score}"

    def test_shadow_active_missing_tests_code_rejected(self, db_session_factory):
        """test_groups 内缺 tests 代码：永久配置错误，拒绝重试且不得重置 attempt/status"""
        from app.models import QuestionRubric
        from datetime import datetime, timezone
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="active", q2_mode="active")
        actor = _make_actor(db_session_factory, "rp4_t", "teacher")

        with db_session_factory() as db:
            q2 = db.get(ExamQuestion, ctx["q2_id"])
            q2.test_groups = [{"id": "F1", "name": "F", "dimension": "F",
                               "max_score": 60, "tests": ""}]  # 缺 tests 代码
            # 先补 locked rubric，确保命中 tests 检查而非 rubric 检查
            rub = QuestionRubric(exam_question_id=q2.id, version=1, status="locked",
                                 source_hash="h", source_snapshot={}, rubric_json={},
                                 model_name="m", locked_at=datetime.now(timezone.utc))
            db.add(rub)
            db.commit()

            with pytest.raises(Exception) as exc:
                retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)
            assert "测试代码" in str(exc.value) or "CONFIG" in str(exc.value)

            # 状态未被重置：父仍 review_required、答案仍 system_error、attempt 不归零
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "review_required"
            assert sub.review_required_at is not None
            ans2 = db.get(ExamAnswer, ctx["ans2_id"])
            assert ans2.grading_status == "system_error"
            assert ans2.attempt_count == 3, f"不得重置 attempt: {ans2.attempt_count}"
            assert ans2.last_error == "缺少隐藏测试"

    def test_retry_then_finalize_grades(self, db_session_factory):
        """重试后答案判完 → 父级正常 graded"""
        from app.services.exam_grading import finalize_if_ready, FinalizeOutcome
        from app.worker.judge_worker import process_exam_answer
        from app.config import get_settings
        import fakeredis

        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        actor = _make_actor(db_session_factory, "rs4_t", "teacher")

        with db_session_factory() as db:
            q2 = db.get(ExamQuestion, ctx["q2_id"])
            q2.hidden_tests = "assert True"
            db.commit()
            retry_exam_submission(ctx["submission_id"], [ctx["ans2_id"]], actor, db)

        settings = get_settings()
        # retry 已自动入队（queued）；process_exam_answer 内部自行 claim 判题，
        # 完成后自动触发 finalize（legacy 路径）
        with db_session_factory() as db:
            with patch_docker_accepted():
                process_exam_answer(db, fakeredis.FakeStrictRedis(), settings, ctx["ans2_id"])

            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "graded", f"判题后应自动 graded: {sub.status}"
            assert sub.score == 30.0

        with db_session_factory() as db:
            grades = db.query(ExamGrade).where(
                ExamGrade.exam_id == ctx["exam_id"]).all()
            assert len(grades) == 1


class TestRetryChoiceQuestion:
    """选择题 system_error 重试：无需判题配置，直接评分，不入队"""

    def test_choice_answer_retried_scored_inline(self, db_session_factory):
        """选择题（legacy 无 hidden_tests 也合法）重试 → 直接评分 completed"""
        from app.models import ExamAnswer as EA
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        actor = _make_actor(db_session_factory, "rcq_t", "teacher")

        # 把 q1 改成选择题（原本是 code）
        with db_session_factory() as db:
            q1 = db.get(ExamQuestion, ctx["q1_id"])
            q1.question_type = "single_choice"
            q1.correct_answer = {"correct": ["A"]}
            q1.options = {"A": "x", "B": "y"}
            q1.hidden_tests = None  # 选择题不需要 hidden_tests
            # ans1 改为 system_error（模拟历史异常路径）
            ans1 = db.get(EA, ctx["ans1_id"])
            ans1.selected_options = ["A"]
            ans1.grading_status = "system_error"
            ans1.score = None
            ans1.system_error = "缺少隐藏测试"  # 历史错误标记
            db.commit()

            sub = retry_exam_submission(ctx["submission_id"], [ctx["ans1_id"]], actor, db)

            ans1b = db.get(EA, ctx["ans1_id"])
            # 选择题直接评分：completed + 正确得分
            assert ans1b.grading_status == "completed", f"选择题应直接评分: {ans1b.grading_status}"
            assert ans1b.score == 10.0
            assert ans1b.system_error is None
            assert ans1b.attempt_count == 0

            # code 题（ans2）不在选择列表中不受影响
            ans2b = db.get(EA, ctx["ans2_id"])
            assert ans2b.grading_status == "system_error"

            # 收尾 finalize：仍有 system_error 答案 → 父保持 review_required（不会停在 grading）
            # 返回对象经 refresh 与数据库一致
            assert sub.status == "review_required", \
                f"仍有 system_error 答案时父应回 review_required: {sub.status}"
            assert sub.review_required_at is not None

    def test_choice_retry_all_answers_grades_immediately(self, db_session_factory):
        """选择题全部重试完成 → 收尾 finalize 立即 graded：父状态、ExamGrade 唯一且分数正确"""
        from app.models import ExamAnswer as EA, ExamSubmission as ES
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        actor = _make_actor(db_session_factory, "rcq2_t", "teacher")

        # 两道题都改成选择题，两个答案都是 system_error（均需重试）
        with db_session_factory() as db:
            for qid, aid, opts in ((ctx["q1_id"], ctx["ans1_id"], ["A"]),
                                   (ctx["q2_id"], ctx["ans2_id"], ["B"])):
                q = db.get(ExamQuestion, qid)
                q.question_type = "single_choice"
                q.correct_answer = {"correct": ["A"]} if opts == ["A"] else {"correct": ["B"]}
                q.options = {"A": "x", "B": "y"}
                q.hidden_tests = None
                ans = db.get(EA, aid)
                ans.selected_options = opts
                ans.grading_status = "system_error"
                ans.score = None
                ans.system_error = "缺少隐藏测试"
            db.commit()

            sub = retry_exam_submission(ctx["submission_id"], [ctx["ans1_id"], ctx["ans2_id"]],
                                        actor, db)

            # 无 code 题待判：收尾 finalize 立即 graded，不依赖 scanner
            # 返回对象必须与数据库一致（finalize 后已 refresh，而非陈旧 grading）
            assert sub.status == "graded", f"全部定分应立即 graded 且返回一致状态: {sub.status}"
            assert sub.score == 30.0, f"总分应为 30: {sub.score}"
            assert sub.graded_at is not None

        with db_session_factory() as db:
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "graded", "数据库与返回状态一致"
            assert sub.score == 30.0

            ans1 = db.get(EA, ctx["ans1_id"])
            ans2 = db.get(EA, ctx["ans2_id"])
            assert ans1.grading_status == "completed" and ans1.score == 10.0
            assert ans2.grading_status == "completed" and ans2.score == 20.0

            # ExamGrade 唯一且分数正确
            grades = db.query(ExamGrade).where(
                ExamGrade.exam_id == ctx["exam_id"]).all()
            assert len(grades) == 1, f"ExamGrade 应唯一: {len(grades)}"
            assert grades[0].score == 30.0, f"ExamGrade 分数应为 30: {grades[0].score}"


class TestRetryApi:
    """HTTP 入口：权限与状态码"""

    def test_teacher_can_retry(self, client, db_session_factory):
        from app.models import Course
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        teacher = create_user(db_session_factory, "ra_t", "teacher")
        # 让该教师成为课程教师
        with db_session_factory() as db:
            course = db.get(Course, ctx["course_id"])
            course.teacher_id = teacher.id
            # Content ownership does not transfer teaching authority. The test
            # administrator assigns the actual existing offering explicitly.
            from app.models.teaching import CourseOffering
            db.query(CourseOffering).filter_by(course_id=course.id).one().teacher_id = teacher.id
            q2 = db.get(ExamQuestion, ctx["q2_id"])
            q2.hidden_tests = "assert True"
            db.commit()

        tok, _ = login(client, "ra_t")

        resp = client.post(
            f"/api/v1/exams/{ctx['exam_id']}/submissions/{ctx['submission_id']}/retry",
            headers=auth_header(tok),
            json={"answer_ids": [ctx["ans2_id"]]},
        )
        assert resp.status_code == 200, f"重试应成功: {resp.status_code} {resp.text}"
        assert resp.json()["status"] == "grading"

    def test_teacher_choice_retry_api_returns_finalized_status(self, client, db_session_factory):
        """API 返回状态与数据库一致：选择题全重试后返回 graded 而非陈旧 grading"""
        from app.models import Course, ExamAnswer as EA
        ctx = _seed_review_required_submission(db_session_factory, q1_mode="legacy", q2_mode="legacy")
        teacher = create_user(db_session_factory, "ra2_t", "teacher")
        with db_session_factory() as db:
            course = db.get(Course, ctx["course_id"])
            course.teacher_id = teacher.id
            from app.models.teaching import CourseOffering
            db.query(CourseOffering).filter_by(course_id=course.id).one().teacher_id = teacher.id
            for qid, aid, opts in ((ctx["q1_id"], ctx["ans1_id"], ["A"]),
                                   (ctx["q2_id"], ctx["ans2_id"], ["B"])):
                q = db.get(ExamQuestion, qid)
                q.question_type = "single_choice"
                q.correct_answer = {"correct": ["A"]} if opts == ["A"] else {"correct": ["B"]}
                q.options = {"A": "x", "B": "y"}
                q.hidden_tests = None
                ans = db.get(EA, aid)
                ans.selected_options = opts
                ans.grading_status = "system_error"
                ans.score = None
                ans.system_error = "缺少隐藏测试"
            db.commit()

        tok, _ = login(client, "ra2_t")
        resp = client.post(
            f"/api/v1/exams/{ctx['exam_id']}/submissions/{ctx['submission_id']}/retry",
            headers=auth_header(tok),
            json={"answer_ids": [ctx["ans1_id"], ctx["ans2_id"]]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "graded", \
            f"API 应返回 graded 而非陈旧 grading: {resp.json()['status']}"
        assert resp.json()["score"] == 30.0

        # 与数据库一致
        with db_session_factory() as db:
            sub = db.get(ExamSubmission, ctx["submission_id"])
            assert sub.status == "graded" and sub.score == 30.0

    def test_student_forbidden(self, client, db_session_factory):
        ctx = _seed_review_required_submission(db_session_factory)
        create_user(db_session_factory, "ra_s", "student")
        tok, _ = login(client, "ra_s")

        resp = client.post(
            f"/api/v1/exams/{ctx['exam_id']}/submissions/{ctx['submission_id']}/retry",
            headers=auth_header(tok),
            json={"answer_ids": [ctx["ans2_id"]]},
        )
        assert resp.status_code == 403, f"学生应被拒绝: {resp.status_code}"


def patch_docker_accepted():
    """mock Docker 判题返回 accepted"""
    from unittest.mock import patch
    return patch(
        "app.worker.judge_worker._run_docker_pytest",
        return_value=("1 passed", "", 0, 150),
    )
