"""Task 1：认领代次栅栏——失去认领的旧 Worker 不得覆盖新结果。

对应审查结论：Worker 先用普通 ORM 提交结果、再调用只以 `running` 为条件的
complete_job，两者之间没有任何代次校验；恢复机制把超时的 running 打回 pending 后
由新 Worker 接管，旧 Worker 继续执行即可覆盖新结果。
"""
import pytest
from unittest.mock import patch

from app.models import CodeGrade, ExamAnswer, ExamQuestion, ExamSubmission, JudgeQuestion, Submission
from app.services.judge_queue import (
    claim_job,
    complete_job,
    current_claim_token,
    enqueue_job,
    fail_job,
    requeue_stale_jobs,
)


@pytest.fixture(autouse=True)
def _default_fake_redis(redis_client):
    with patch("app.services.judge_queue._get_redis", return_value=redis_client):
        yield


def _make_submission(db_session_factory, *, status="queued", grading_status="pending"):
    from app.models import Assignment, Course, User
    with db_session_factory() as db:
        teacher = User(username="cf_t", real_name="CFT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="cf_s", real_name="CFS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        course = Course(title="CFC", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(course_id=course.id, title="CFA", status="published")
        db.add(assignment)
        db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title="CFQ",
                                 function_name="add", hidden_tests="assert True",
                                 public_cases=[{"args": [1, 2], "expected": 3}])
        db.add(question)
        db.flush()
        submission = Submission(question_id=question.id, student_id=student.id,
                                code="def add(a, b): return a + b",
                                status=status, grading_status=grading_status)
        db.add(submission)
        db.commit()
        return submission.id


def _make_exam_answer(db_session_factory, *, grading_status="pending"):
    from app.models import Course, Exam, User
    with db_session_factory() as db:
        teacher = User(username="cfe_t", real_name="CFET", role="teacher", status="active",
                       password_hash="x")
        student = User(username="cfe_s", real_name="CFES", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        course = Course(title="CFEC", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        exam = Exam(course_id=course.id, title="CFEE", status="published", duration_minutes=60)
        db.add(exam)
        db.flush()
        question = ExamQuestion(exam_id=exam.id, question_type="code", prompt="写 add",
                                correct_answer={}, points=10, hidden_tests="assert True",
                                public_cases=[{"args": [1, 2], "expected": 3}])
        db.add(question)
        db.flush()
        sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
        db.add(sub)
        db.flush()
        answer = ExamAnswer(submission_id=sub.id, question_id=question.id,
                            code_answer="def add(a, b): return a + b",
                            grading_status=grading_status)
        db.add(answer)
        db.commit()
        return answer.id


# ─────────────────────────────────────────────────────────────
# 正常路径：认领 → 完成 仍然可用（不得把栅栏做成回归）
# ─────────────────────────────────────────────────────────────

def test_claim_returns_token_and_complete_succeeds(db_session_factory):
    sid = _make_submission(db_session_factory, grading_status="queued")
    with db_session_factory() as db:
        token = claim_job(db, job_type="assignment", object_id=sid)
        assert token
        assert current_claim_token(db, "assignment", sid) == token

        assert complete_job(db, job_type="assignment", object_id=sid, score=100.0,
                            values={"status": "accepted"}) is True

    with db_session_factory() as db:
        row = db.get(Submission, sid)
        assert row.grading_status == "completed"
        assert row.score == 100.0
        assert row.status == "accepted"
        assert row.finished_at is not None
        assert row.claim_token is None  # 终态释放代次


def test_repeated_claim_after_completion_is_rejected(db_session_factory):
    """重复消息不得再次认领已完成任务。"""
    sid = _make_submission(db_session_factory, grading_status="queued")
    with db_session_factory() as db:
        assert claim_job(db, job_type="assignment", object_id=sid)
        complete_job(db, job_type="assignment", object_id=sid, score=1.0)
        assert claim_job(db, job_type="assignment", object_id=sid) is None


def test_write_without_claim_is_refused(db_session_factory):
    """从未认领却写结果 → 拒绝，且不落库。"""
    sid = _make_submission(db_session_factory, grading_status="running")
    with db_session_factory() as db:
        assert complete_job(db, job_type="assignment", object_id=sid, score=99.0) is False
        assert fail_job(db, job_type="assignment", object_id=sid,
                        error="x", retryable=False) is False
    with db_session_factory() as db:
        row = db.get(Submission, sid)
        assert row.score is None
        assert row.status == "queued"
        assert row.grading_status == "running"


# ─────────────────────────────────────────────────────────────
# 核心：恢复重新认领后，旧 Worker 的成功/失败写入都必须失效
# ─────────────────────────────────────────────────────────────

def _reclaim_cycle(db_session_factory, sid, job_type="assignment"):
    """旧 Worker 认领后超时 → 恢复重排 → 新 Worker 认领。返回 (旧token, 新token)。"""
    with db_session_factory() as db:
        old_token = claim_job(db, job_type=job_type, object_id=sid)
        assert old_token
        # 阈值 0 ⇒ 立刻视为超时（心跳回退 started_at，均已过期）
        stats = requeue_stale_jobs(db, job_type=job_type, stale_running_seconds=0)
        assert stats["running_reset"] == 1
        assert enqueue_job(db, job_type=job_type, object_id=sid) is True
    with db_session_factory() as db:
        new_token = claim_job(db, job_type=job_type, object_id=sid)
        assert new_token and new_token != old_token
    return old_token, new_token


def test_stale_worker_cannot_complete_after_claim_reclaimed(db_session_factory):
    """旧 Worker 的迟到成功写入不得覆盖新 Worker 的结果。"""
    sid = _make_submission(db_session_factory, grading_status="queued")
    old_token, _new_token = _reclaim_cycle(db_session_factory, sid)

    # 新 Worker 先完成
    with db_session_factory() as db:
        assert claim_job(db, job_type="assignment", object_id=sid) is None  # 已 running
    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", sid)] = _new_token
        assert complete_job(db, job_type="assignment", object_id=sid, score=100.0,
                            values={"status": "accepted"}) is True

    # 旧 Worker 恢复执行并试图写回（携带它自己的旧 token）
    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", sid)] = old_token
        assert complete_job(db, job_type="assignment", object_id=sid, score=0.0,
                            values={"status": "wrong_answer"}) is False

    with db_session_factory() as db:
        row = db.get(Submission, sid)
        assert row.score == 100.0, "新结果被旧 Worker 覆盖"
        assert row.status == "accepted"
        assert row.grading_status == "completed"
        assert row.claim_token is None


def test_stale_worker_cannot_fail_after_claim_reclaimed(db_session_factory):
    """旧 Worker 的迟到失败写入同样不得覆盖新结果。"""
    sid = _make_submission(db_session_factory, grading_status="queued")
    old_token, new_token = _reclaim_cycle(db_session_factory, sid)

    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", sid)] = new_token
        assert complete_job(db, job_type="assignment", object_id=sid, score=88.0,
                            values={"status": "accepted"}) is True

    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", sid)] = old_token
        assert fail_job(db, job_type="assignment", object_id=sid,
                        error="旧 Worker 崩溃", retryable=False) is False

    with db_session_factory() as db:
        row = db.get(Submission, sid)
        assert row.grading_status == "completed"
        assert row.score == 88.0
        assert row.status == "accepted"


def test_old_worker_cannot_resurrect_status_after_reclaim(db_session_factory):
    """旧 Worker 的"退回 pending"也不得撤销新 Worker 已完成的任务。"""
    sid = _make_submission(db_session_factory, grading_status="queued")
    old_token, new_token = _reclaim_cycle(db_session_factory, sid)
    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", sid)] = new_token
        complete_job(db, job_type="assignment", object_id=sid, score=50.0)
    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", sid)] = old_token
        assert fail_job(db, job_type="assignment", object_id=sid,
                        error="retry", retryable=True) is False
    with db_session_factory() as db:
        assert db.get(Submission, sid).grading_status == "completed"


def test_stale_worker_cannot_complete_exam_answer(db_session_factory):
    """考试答案同样受栅栏保护。"""
    aid = _make_exam_answer(db_session_factory, grading_status="queued")
    old_token, new_token = _reclaim_cycle(db_session_factory, aid, job_type="exam")
    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("exam", aid)] = new_token
        assert complete_job(db, job_type="exam", object_id=aid, score=10.0) is True
    with db_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("exam", aid)] = old_token
        assert complete_job(db, job_type="exam", object_id=aid, score=0.0) is False
    with db_session_factory() as db:
        assert db.get(ExamAnswer, aid).score == 10.0


def test_exam_completion_writes_finished_at(db_session_factory):
    """回归：考试 legacy 路径原先先 ORM 置 completed 再调 complete_job，
    后者的 running 条件必然不命中 ⇒ finished_at 永不写入。"""
    aid = _make_exam_answer(db_session_factory, grading_status="queued")
    with db_session_factory() as db:
        assert claim_job(db, job_type="exam", object_id=aid)
        assert complete_job(db, job_type="exam", object_id=aid, score=10.0) is True
    with db_session_factory() as db:
        row = db.get(ExamAnswer, aid)
        assert row.grading_status == "completed"
        assert row.finished_at is not None


# ─────────────────────────────────────────────────────────────
# AI 支路：同一套代次语义
# ─────────────────────────────────────────────────────────────

def _make_code_grade(db_session_factory):
    from app.models import QuestionRubric
    sid = _make_submission(db_session_factory, grading_status="completed")
    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        rubric = QuestionRubric(judge_question_id=sub.question_id, status="locked",
                                version=1, source_hash="h", source_snapshot={},
                                rubric_json={"dimensions": []}, model_name="test")
        db.add(rubric)
        db.flush()
        cg = CodeGrade(submission_id=sid, rubric_id=rubric.id, mode="active",
                       status="queued", functional_score=0, robustness_score=0)
        db.add(cg)
        db.commit()
        return cg.id


def test_ai_stale_run_cannot_overwrite_new_grade(db_session_factory):
    """AI 客户端最多重试 3 次 × 60–180s（超过旧的 600s stale 阈值），
    旧运行的评分写入必须被栅栏拒绝。"""
    from app.services.ai_grading_queue import claim_ai_grade, complete_ai_grade, finish_ai_grade

    cg_id = _make_code_grade(db_session_factory)
    with db_session_factory() as db:
        old_token = claim_ai_grade(db, cg_id)
        assert old_token

    # 新运行接管（模拟 stale 恢复后的重新排队 + 认领）
    with db_session_factory() as db:
        from sqlalchemy import update as _update
        db.execute(_update(CodeGrade).where(CodeGrade.id == cg_id)
                   .values(status="queued", claim_token=None))
        db.commit()
        new_token = claim_ai_grade(db, cg_id)
        assert new_token and new_token != old_token
        assert complete_ai_grade(db, cg_id, {"final_score_100": 90.0}) is True

    # 旧运行迟到写入
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[cg_id] = old_token
        assert finish_ai_grade(db, cg_id, {"final_score_100": 10.0}) is False

    with db_session_factory() as db:
        row = db.get(CodeGrade, cg_id)
        assert row.final_score_100 == 90.0, "旧 AI 运行覆盖了新结果"
        assert row.status == "completed"
