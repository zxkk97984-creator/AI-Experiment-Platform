"""第三轮回归测试——P0 问题：缺配置/缺Rubric→系统错误、越权、考试结算

A/B/C 分类：B 类（最小父行）——题目/学生经共享工厂 make_judge_question /
make_student 建真实父行，不再硬编码 assignment_id=1、student_id=1。
"""
import pytest

from conftest import grant_judge_claim


# ── P0: 缺配置/缺Rubric → 系统错误不扣分 ──

def test_v1_without_test_groups_and_without_bound_version_goes_to_manual_review(db_session_factory):
    """实时配置不构成可信基线；未绑定的新评分一律转人工且不产出分数。"""
    from app.models import Submission
    from app.services.judge_queue import enqueue_job
    from conftest import make_judge_question, make_student

    question_id = make_judge_question(
        db_session_factory, grading_mode="shadow", test_groups=[],
    )
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        sub = Submission(question_id=question_id, student_id=student.id,
                         code="def f(): pass",
                         status="queued", grading_status="pending")
        db.add(sub); db.commit()
        enqueue_job(db, job_type="assignment", object_id=sub.id)

    from app.config import Settings
    from app.worker.judge_worker import process_submission

    settings = Settings(_env_file=None, judge_use_docker=False, judge_timeout_seconds=5)
    with db_session_factory() as db:
        result = process_submission(db, None, settings, sub.id)
        assert result.status == "review_required", f"缺可信版本应转人工: {result.status}"
        assert result.grading_status == "completed"
        assert result.score is None
        assert result.result_details["diagnostic"]["code"] == "MISSING_GRADING_BASELINE"


def test_v1_missing_rubric_system_error(db_session_factory):
    """缺锁定 Rubric→系统错误"""
    from app.models import JudgeQuestion, Submission
    from app.worker.judge_worker import _v1_judge_submission
    from app.config import Settings
    from conftest import make_judge_question, make_student

    question_id = make_judge_question(
        db_session_factory,
        grading_mode="active",
        test_groups=[
            {"id": "F1", "name": "F", "dimension": "F", "max_score": 60, "tests": "def test(): assert True"},
            {"id": "R1", "name": "R", "dimension": "R", "max_score": 10, "tests": "def test(): assert True"},
        ],
    )
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        q = db.get(JudgeQuestion, question_id)
        sub = Submission(question_id=question_id, student_id=student.id,
                         code="def f(): pass",
                         status="running", grading_status="running")
        db.add(sub); db.commit()
        # 判题函数只在认领成功后调用；补上认领前置，否则结果写入会被栅栏拒绝
        grant_judge_claim(db, Submission, sub.id)

        settings = Settings(_env_file=None, judge_use_docker=False)
        result = _v1_judge_submission(db, None, settings, sub, q, None, None, 5, 256)
        assert result.status == "system_error"


def test_legacy_without_bound_version_does_not_fall_back_to_pytest(db_session_factory):
    """旧题的 hidden_tests 不能替代确认过的版本基线或启动正式 pytest 评分。"""
    from unittest.mock import patch
    from app.models import Submission
    from app.worker.judge_worker import process_submission
    from app.config import Settings
    from app.services.judge_queue import enqueue_job
    from conftest import make_judge_question, make_student

    question_id = make_judge_question(
        db_session_factory, grading_mode="legacy", test_groups=[],
    )
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        sub = Submission(question_id=question_id, student_id=student.id,
                         code="def f(): pass",
                         status="queued", grading_status="pending")
        db.add(sub); db.commit()
        enqueue_job(db, job_type="assignment", object_id=sub.id)

    settings = Settings(_env_file=None, judge_use_docker=False, judge_timeout_seconds=5)
    with db_session_factory() as db:
        with patch("app.worker.judge_worker._run_docker_pytest",
                   side_effect=AssertionError("unbound submissions must not run the legacy scorer")) as docker:
            result = process_submission(db, None, settings, sub.id)
        assert result.status == "review_required"
        assert result.score is None
        docker.assert_not_called()


# ── P0: 考试权限——越权覆盖返回 403 ──

def test_teacher_cannot_override_other_course(client, db_session_factory):
    """教师不能覆盖其他课程的成绩"""
    from conftest import create_user, login, auth_header

    with db_session_factory() as db:
        from app.models import User, Course, Exam, ExamQuestion, ExamSubmission, ExamAnswer, CodeGrade, QuestionRubric
        import datetime as _dt

        t1 = create_user(db_session_factory, "t1_reg", "teacher")
        t2 = create_user(db_session_factory, "t2_reg", "teacher")
        s1 = create_user(db_session_factory, "s1_reg", "student")

        with db_session_factory() as db2:
            c1 = Course(title="C1", status="published", teacher_id=t1.id)
            c2 = Course(title="C2", status="published", teacher_id=t2.id)
            db2.add_all([c1, c2]); db2.flush()
            e1 = Exam(course_id=c1.id, title="E1", status="published", duration_minutes=60)
            db2.add(e1); db2.flush()
            eq = ExamQuestion(exam_id=e1.id, question_type="code", prompt="t",
                              correct_answer={"test_file":""}, points=10, grading_mode="shadow")
            db2.add(eq); db2.flush()
            es = ExamSubmission(exam_id=e1.id, student_id=s1.id, status="grading")
            db2.add(es); db2.flush()
            ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                            grading_status="completed", score=5)
            db2.add(ea); db2.flush()
            now = _dt.datetime.now(_dt.timezone.utc)
            rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                                 source_hash="a", source_snapshot={}, rubric_json={},
                                 model_name="m", locked_at=now)
            db2.add(rub); db2.flush()
            cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="shadow",
                           status="completed", functional_score=30, robustness_score=5)
            db2.add(cg); db2.commit()
            gid = cg.id

    # t2 登录尝试覆盖 t1 课程的评分
    t2_tok, _ = login(client, "t2_reg")
    resp = client.post(
        f"/api/v1/ai-grading/grades/{gid}/override",
        headers=auth_header(t2_tok),
        json={"reason": "跨课程覆盖测试", "final_score_100": 80},
    )
    assert resp.status_code == 404, f"跨课程覆盖隐藏记录应 404: {resp.status_code} {resp.text}"


# ── P0: exam finalize 阻止 active score=None ──

def test_finalize_blocks_active_score_none(db_session_factory):
    """active 答案 score=None（AI 分未落地）→ 父转 review_required，不按 0 分结算"""
    from app.services.exam_grading import finalize_if_ready, FinalizeOutcome
    from datetime import datetime, timezone
    from app.models import Course, Exam, ExamAnswer, ExamQuestion, ExamSubmission, User

    with db_session_factory() as db:
        teacher = User(username="v3_t", real_name="V3T", role="teacher", status="active",
                       password_hash="x")
        student = User(username="v3_s", real_name="V3S", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="V3C", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="V3E", status="published", duration_minutes=60)
        db.add(exam); db.flush()
        eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                          correct_answer={}, points=10, grading_mode="active")
        db.add(eq); db.flush()
        es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
        db.add(es); db.flush()
        ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                        grading_status="completed", score=None)  # active 等 AI 分但已终止
        db.add(ea); db.commit()
        sub_id = es.id

    with db_session_factory() as db:
        result = finalize_if_ready(sub_id, db)
        assert result.outcome == FinalizeOutcome.REVIEW_REQUIRED, \
            f"score=None 的 active 答案应转 review_required: {result}"
        es2 = db.get(ExamSubmission, sub_id)
        assert es2.status == "review_required", "父应转 review_required"
        assert es2.review_required_at is not None
