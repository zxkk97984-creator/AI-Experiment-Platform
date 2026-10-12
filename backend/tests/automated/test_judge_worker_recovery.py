"""Task 2: Worker 恢复测试——crash 恢复、重复消息、stale-running、终态"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import ExamAnswer, ExamSubmission, ExamQuestion, Submission
from app.services.judge_queue import (
    MAX_ATTEMPTS,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    requeue_stale_jobs,
)
from conftest import auth_header, create_user, login, seed_basic_environment


def _setup_submission(db_session_factory):
    """创建 pending 的作业提交"""
    with db_session_factory() as db:
        from app.models import Assignment, Course, JudgeQuestion, User
        teacher = User(username="rcv_t", real_name="RT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="rcv_s", real_name="RS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        course = Course(title="RC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        assignment = Assignment(course_id=course.id, title="RA", status="published")
        db.add(assignment); db.flush()
        question = JudgeQuestion(
            assignment_id=assignment.id, title="RQ", function_name="add",
            hidden_tests="assert True", public_cases=[{"args": [1, 2], "expected": 3}],
        )
        db.add(question); db.flush()
        sub = Submission(
            question_id=question.id, student_id=student.id,
            code="def add(a,b): return a+b",
            status="queued", grading_status="pending",
        )
        db.add(sub); db.commit()
        return sub.id


def _setup_exam_answer(db_session_factory):
    """创建 pending 的考试答案"""
    with db_session_factory() as db:
        from app.models import Course, Exam, User
        teacher = User(username="rv_t", real_name="RVT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="rv_s", real_name="RVS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="RVC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="RVE", status="published",
                    duration_minutes=60)
        db.add(exam); db.flush()
        eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="X",
                          correct_answer={}, points=10, hidden_tests="assert True")
        db.add(eq); db.flush()
        sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
        db.add(sub); db.flush()
        ans = ExamAnswer(submission_id=sub.id, question_id=eq.id,
                         code_answer="def add(a,b): return a+b", grading_status="pending")
        db.add(ans); db.commit()
        return ans.id


def _bind_trusted_legacy_version(db, submission_id):
    """Bind an explicit v2 legacy/binary version when a test needs the scoring path."""
    from app.models import JudgeQuestion
    from app.services import grading_version_service as versions
    from app.services.grading_values import encode_value

    submission = db.get(Submission, submission_id)
    question = db.get(JudgeQuestion, submission.question_id)
    version = versions.create_draft(
        db, question_kind="assignment", question_id=question.id,
        payload={"binary_cases": [{
            "id": "B1", "input": encode_value((1, 2)),
            "expected": encode_value(3), "compare": {"kind": "literal"},
        }]},
        entry_function=question.function_name, actor_id=None,
        grading_mode_snapshot="legacy",
    )
    versions.confirm_version(db, version, confirm_isolated_case_v2=True)
    versions.activate_version(db, version)
    versions.set_question_current_version(db, question_kind="assignment",
                                          question_id=question.id, version_id=version.id)
    submission.grading_version_id = version.id
    db.flush()
    return version.id


# ═══════════════════════════════════════════════════════════════
# 1. crash 恢复：Worker 领取后崩溃，任务能被恢复扫描重置
# ═══════════════════════════════════════════════════════════════

def test_crash_after_claim_recoverable(db_session_factory):
    """Worker 在 claim 后崩溃 → running 超时后重置为 pending，重新入队"""
    sid = _setup_submission(db_session_factory)

    # 入队
    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)

    # 模拟 Worker 领取任务后立即崩溃
    with db_session_factory() as db:
        ok = claim_job(db, job_type="assignment", object_id=sid)
        assert ok

        # 验证已变为 running
        sub = db.get(Submission, sid)
        assert sub.grading_status == "running"

    # 手动设置 started_at 为 10 分钟前（模拟崩溃后长时间无人处理）
    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        sub.started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        sub.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db.commit()

    # 恢复扫描：stale_running_seconds=300，应重置此任务
    with db_session_factory() as db:
        stats = requeue_stale_jobs(db, job_type="assignment", stale_running_seconds=300)
        assert stats["running_reset"] >= 1, f"应重置 stale running: {stats}"

        sub = db.get(Submission, sid)
        assert sub.grading_status == "pending", f"崩溃后应重置为 pending: {sub.grading_status}"
        assert "stale running" in (sub.last_error or "")


# ═══════════════════════════════════════════════════════════════
# 2. 重复消息不会执行两次
# ═══════════════════════════════════════════════════════════════

def test_duplicate_redis_message_not_processed_twice(db_session_factory):
    """同一任务的重复 Redis 消息：claim_job 第二次失败，不重复执行"""
    sid = _setup_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)

    # Worker-1 领取
    with db_session_factory() as db:
        ok1 = claim_job(db, job_type="assignment", object_id=sid)
        assert ok1

    # Worker-2 收到重复消息尝试领取 → 失败
    with db_session_factory() as db:
        ok2 = claim_job(db, job_type="assignment", object_id=sid)
        assert not ok2, "重复消息不应成功抢占"

    # 状态仍然是 running（Worker-1 设置的）
    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        assert sub.grading_status == "running"


def test_duplicate_redis_message_exam_not_processed_twice(db_session_factory):
    """考试题同样不会被重复执行"""
    aid = _setup_exam_answer(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)
        ok1 = claim_job(db, job_type="exam", object_id=aid)
        assert ok1

    with db_session_factory() as db:
        ok2 = claim_job(db, job_type="exam", object_id=aid)
        assert not ok2


# ═══════════════════════════════════════════════════════════════
# 3. 永久错误最终进入终态
# ═══════════════════════════════════════════════════════════════

def test_permanent_error_reaches_system_error(db_session_factory):
    """不可重试的错误 → system_error 终态"""
    sid = _setup_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)

        fail_job(db, job_type="assignment", object_id=sid,
                 error="题目不存在", retryable=False)

        sub = db.get(Submission, sid)
        assert sub.grading_status == "system_error"
        assert sub.last_error == "题目不存在"


def test_max_retries_reaches_system_error(db_session_factory):
    """达到最大重试次数 → system_error，分数为 0"""
    sid = _setup_submission(db_session_factory)

    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        sub.attempt_count = MAX_ATTEMPTS
        db.commit()

        stats = requeue_stale_jobs(db, job_type="assignment", stale_pending_seconds=0)
        assert stats["max_retries_reached"] >= 1

        sub2 = db.get(Submission, sid)
        assert sub2.grading_status == "system_error"
        assert sub2.score is None  # 系统错误不扣分（第六轮修正）
        assert "超过最大重试次数" in (sub2.last_error or "")


def _setup_active_submission(db_session_factory):
    """创建 active 模式作业提交（含测试组与锁定 Rubric，测试按需破坏配置）"""
    with db_session_factory() as db:
        from app.models import Assignment, Course, JudgeQuestion, QuestionRubric, User
        teacher = User(username="pc_t", real_name="PCT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="pc_s", real_name="PCS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="PCC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        assignment = Assignment(course_id=course.id, title="PCA", status="published")
        db.add(assignment); db.flush()
        q = JudgeQuestion(assignment_id=assignment.id, title="PCQ", function_name="f",
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
                         code="def f(): pass", status="queued", grading_status="pending")
        db.add(sub); db.commit()
        return {"sid": sub.id, "qid": q.id}


def _run_process_submission(db_session_factory, sid):
    """入队 + process_submission（内部自行 claim）"""
    from app.worker.judge_worker import process_submission
    from app.config import get_settings
    import fakeredis
    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
    with db_session_factory() as db:
        process_submission(db, fakeredis.FakeStrictRedis(), get_settings(), sid)
    with db_session_factory() as db:
        return db.get(Submission, sid)


# ═══════════════════════════════════════════════════════════════
# 3.1 未绑定可信评分版本：安全转人工，不使用当前题目配置回退
# ═══════════════════════════════════════════════════════════════

def test_submission_missing_test_groups_permanent_error_immediate(db_session_factory):
    """未绑定基线时，即使实时 active 缺组也必须转人工。"""
    from app.models import JudgeQuestion
    ctx = _setup_active_submission(db_session_factory)

    with db_session_factory() as db:
        db.get(JudgeQuestion, ctx["qid"]).test_groups = []
        db.commit()

    sub = _run_process_submission(db_session_factory, ctx["sid"])
    assert sub.grading_status == "completed"
    assert sub.status == "review_required"
    assert sub.result_details["diagnostic"]["code"] == "MISSING_GRADING_BASELINE"
    assert sub.score is None


def test_submission_missing_locked_rubric_permanent_error_immediate(db_session_factory):
    """未绑定基线时，不读取实时 Rubric 并猜测评分方式。"""
    from app.models import JudgeQuestion, QuestionRubric
    ctx = _setup_active_submission(db_session_factory)

    with db_session_factory() as db:
        db.get(JudgeQuestion, ctx["qid"]).test_groups = [
            {"id": "F1", "name": "F", "dimension": "F",
             "max_score": 60, "tests": "def test(): pass"}]
        rub = db.query(QuestionRubric).filter(
            QuestionRubric.judge_question_id == ctx["qid"]).first()
        db.delete(rub)
        db.commit()

    sub = _run_process_submission(db_session_factory, ctx["sid"])
    assert sub.grading_status == "completed"
    assert sub.status == "review_required"
    assert sub.result_details["diagnostic"]["code"] == "MISSING_GRADING_BASELINE"
    assert sub.score is None


def test_submission_test_group_missing_tests_permanent_error_immediate(db_session_factory):
    """未绑定基线时，当前题目的 tests 文本不能触发旧正式评分。"""
    from app.models import JudgeQuestion
    ctx = _setup_active_submission(db_session_factory)

    with db_session_factory() as db:
        db.get(JudgeQuestion, ctx["qid"]).test_groups = [
            {"id": "F1", "name": "F", "dimension": "F", "max_score": 60, "tests": ""}]
        db.commit()

    sub = _run_process_submission(db_session_factory, ctx["sid"])
    assert sub.grading_status == "completed"
    assert sub.status == "review_required"
    assert sub.result_details["diagnostic"]["code"] == "MISSING_GRADING_BASELINE"
    assert sub.score is None


def test_submission_missing_hidden_tests_permanent_error_immediate(db_session_factory):
    """缺少可信版本时，实时 hidden_tests 不能作为新正式评分基线。"""
    sid = _setup_submission(db_session_factory)

    with db_session_factory() as db:
        from app.models import JudgeQuestion
        qid = db.get(Submission, sid).question_id
        db.get(JudgeQuestion, qid).hidden_tests = ""  # NOT NULL 约束：用空串模拟缺失
        db.commit()

    sub = _run_process_submission(db_session_factory, sid)
    assert sub.grading_status == "completed"
    assert sub.status == "review_required"
    assert sub.result_details["diagnostic"]["code"] == "MISSING_GRADING_BASELINE"
    assert sub.score is None


def test_exam_answer_max_retries_with_finalize(db_session_factory):
    """考试题超过最大重试 → system_error + 父级立即转 review_required（公平性不结算）"""
    aid = _setup_exam_answer(db_session_factory)

    with db_session_factory() as db:
        ans = db.get(ExamAnswer, aid)
        ans.attempt_count = MAX_ATTEMPTS
        db.commit()

        stats = requeue_stale_jobs(db, job_type="exam", stale_pending_seconds=0)
        assert stats["max_retries_reached"] >= 1

        ans2 = db.get(ExamAnswer, aid)
        assert ans2.grading_status == "system_error"
        assert ans2.score is None  # 系统错误不扣分（第六轮修正）

        # 父级转入 review_required 终态，而非永远卡在 grading
        sub = db.get(ExamSubmission, ans2.submission_id)
        assert sub.status == "review_required", f"应为 review_required: {sub.status}"
        assert sub.review_required_at is not None


def test_missing_hidden_tests_permanent_error_immediate(db_session_factory):
    """旧 hidden_tests 未绑定版本时不可触发正式判分，任务转人工。"""
    from app.worker.judge_worker import process_exam_answer
    from app.config import get_settings
    import fakeredis

    aid = _setup_exam_answer(db_session_factory)
    settings = get_settings()

    with db_session_factory() as db:
        # 清掉隐藏测试（模拟历史数据/导入绕过校验）
        ans = db.get(ExamAnswer, aid)
        q = db.get(ExamQuestion, ans.question_id)
        q.hidden_tests = None
        db.commit()

    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)

    with db_session_factory() as db:
        process_exam_answer(db, fakeredis.FakeStrictRedis(), settings, aid)

        ans = db.get(ExamAnswer, aid)
        # 没有绑定的冻结版本：明确告知教师人工处理，而非读取实时 hidden_tests。
        assert ans.grading_status == "system_error"
        assert ans.attempt_count <= 1, f"不应耗尽重试: {ans.attempt_count}"
        assert ans.score is None  # 系统错误不扣分
        assert "评分版本基线" in (ans.system_error or "")

        # 父级当场转 review_required
        sub = db.get(ExamSubmission, ans.submission_id)
        assert sub.status == "review_required", f"父级应转 review_required: {sub.status}"


def test_test_group_missing_tests_permanent_error_immediate(db_session_factory):
    """未绑定评分版本时，实时 test_groups 缺失不应进入旧评分路径。"""
    from app.worker.judge_worker import process_exam_answer
    from app.config import get_settings
    import fakeredis

    aid = _setup_exam_answer(db_session_factory)
    settings = get_settings()

    with db_session_factory() as db:
        ans = db.get(ExamAnswer, aid)
        q = db.get(ExamQuestion, ans.question_id)
        q.grading_mode = "active"
        q.hidden_tests = None
        q.test_groups = [{"id": "F1", "name": "F", "dimension": "F",
                          "max_score": 60, "tests": ""}]  # 配置缺失：无 tests 代码
        # 需锁定 rubric（否则先命中 rubric 检查；为验证 tests 缺失，补一个）
        from app.models import QuestionRubric
        from datetime import datetime, timezone
        rub = QuestionRubric(exam_question_id=q.id, version=1, status="locked",
                             source_hash="h", source_snapshot={}, rubric_json={},
                             model_name="m", locked_at=datetime.now(timezone.utc))
        db.add(rub)
        db.commit()

    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)

    with db_session_factory() as db:
        process_exam_answer(db, fakeredis.FakeStrictRedis(), settings, aid)

        ans = db.get(ExamAnswer, aid)
        assert ans.grading_status == "system_error", \
            f"测试组缺 tests 应立即 system_error: {ans.grading_status}"
        assert ans.attempt_count <= 1, f"不应消耗重试: {ans.attempt_count}"
        assert ans.score is None
        assert "评分版本基线" in (ans.system_error or "")

        sub = db.get(ExamSubmission, ans.submission_id)
        assert sub.status == "review_required", f"父级应转 review_required: {sub.status}"


# ═══════════════════════════════════════════════════════════════
# 3.5 状态 CAS：旧 Worker 的 fail 不得覆盖新 Worker 已完成的判题结果
# ═══════════════════════════════════════════════════════════════

def test_fail_job_does_not_override_completed(db_session_factory):
    """fail_job 仅对 running 生效——已完成的任务不被旧 Worker 失败覆盖"""
    sid = _setup_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)
        # 新 Worker 已完成
        complete_job(db, job_type="assignment", object_id=sid, score=80.0)
        assert db.get(Submission, sid).grading_status == "completed"

        # 旧 Worker 迟到 fail
        fail_job(db, job_type="assignment", object_id=sid,
                 error="旧 Worker 超时", retryable=True)
        db.expire_all()
        sub = db.get(Submission, sid)
        assert sub.grading_status == "completed", \
            f"已完成的任务不应被旧 Worker 失败覆盖: {sub.grading_status}"
        assert sub.score == 80.0


def test_fail_ai_grade_does_not_override_completed(db_session_factory):
    """fail_ai_grade 仅对 running 生效——已完成 CodeGrade 不被旧 Worker 失败覆盖"""
    from app.services.ai_grading_queue import fail_ai_grade
    from unittest.mock import MagicMock
    from datetime import datetime, timezone
    from app.models import CodeGrade, QuestionRubric
    from app.models import Assignment, Course, JudgeQuestion, Submission as Sub, User

    with db_session_factory() as db:
        teacher = User(username="fa_t", real_name="FAT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="fa_s", real_name="FAS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student]); db.flush()
        course = Course(title="FAC", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        assignment = Assignment(course_id=course.id, title="FAA", status="published")
        db.add(assignment); db.flush()
        q = JudgeQuestion(assignment_id=assignment.id, title="FAQ", function_name="f",
                          hidden_tests="assert True", public_cases=[],
                          grading_mode="active",
                          test_groups=[{"id": "F1", "name": "F", "dimension": "F",
                                        "max_score": 60, "tests": "def test(): pass"}])
        db.add(q); db.flush()
        rub = QuestionRubric(judge_question_id=q.id, version=1, status="locked",
                             source_hash="h", source_snapshot={}, rubric_json={},
                             model_name="m", locked_at=datetime.now(timezone.utc))
        db.add(rub); db.flush()
        sub = Sub(question_id=q.id, student_id=student.id,
                  code="def f(): pass", status="running", grading_status="running")
        db.add(sub); db.commit()
        cg = CodeGrade(submission_id=sub.id, rubric_id=rub.id, mode="active",
                       status="completed", functional_score=60, robustness_score=10)
        db.add(cg); db.commit()
        cg_id = cg.id

    with db_session_factory() as db:
        # 已完成状态：fail_ai_grade 不得覆盖
        fail_ai_grade(db, MagicMock(), cg_id, "旧 Worker 超时",
                      retryable=True, max_attempts=3)
        db.expire_all()
        cg2 = db.get(CodeGrade, cg_id)
        assert cg2.status == "completed", \
            f"已完成 CodeGrade 不应被旧 Worker 失败覆盖: {cg2.status}"


# ═══════════════════════════════════════════════════════════════
# 4. 未知异常在 process_submission/process_exam_answer 中触发 fail_job
# ═══════════════════════════════════════════════════════════════

def test_process_submission_unknown_exception_triggers_fail_job(db_session_factory):
    """process_submission 内部未知异常 → fail_job 退回 pending"""
    from app.worker.judge_worker import process_submission
    from app.config import Settings

    sid = _setup_submission(db_session_factory)
    # 使用真实 Settings 而非 MagicMock：os.fspath(MagicMock) 会返回
    # 'MagicMock/mock.judge_work_dir/<id>' 伪路径并在仓库根生成垃圾目录。
    settings = Settings(
        _env_file=None,
        judge_use_docker=False,
        judge_timeout_seconds=5,
        judge_work_dir="",  # 空 → 回退 TemporaryDirectory
    )

    with db_session_factory() as db:
        _bind_trusted_legacy_version(db, sid)
        enqueue_job(db, job_type="assignment", object_id=sid)

    with db_session_factory() as db:
        # 模拟 data_v1 runner 准备抛出未知异常（版本已解析、认领已成功后）
        with patch("app.services.data_grading.write_runner_files",
                   side_effect=RuntimeError("模拟未知异常")):
            result = process_submission(db, MagicMock(), settings, sid)

        # 应该退回 pending 而非 running
        sub = db.get(Submission, sid)
        assert sub.grading_status == "pending", f"未知异常应退回 pending: {sub.grading_status}"
        assert sub.last_error is not None, "应记录错误信息"


def test_mock_settings_do_not_create_magicmock_workdirs(db_session_factory):
    """回归：误传 MagicMock settings 时工作目录必须回退临时目录，
    不得在仓库根生成 MagicMock/mock.judge_work_dir/<id> 垃圾目录。"""
    from app.worker.judge_worker import process_submission
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2].parent
    marker = repo_root / "MagicMock"
    before = len(list(marker.rglob("*"))) if marker.exists() else 0

    sid = _setup_submission(db_session_factory)
    settings = MagicMock()
    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
    with db_session_factory() as db:
        with patch("app.worker.judge_worker._write_submission_files",
                   side_effect=RuntimeError("模拟未知异常")):
            process_submission(db, MagicMock(), settings, sid)

    after = len(list(marker.rglob("*"))) if marker.exists() else 0
    assert after == before, \
        f"MagicMock settings 不得生成工作目录垃圾（{before} -> {after}）"


def test_process_exam_answer_unknown_exception_triggers_fail_job(db_session_factory):
    """process_exam_answer 内部未知异常（非 Docker 路径） → fail_job 退回 pending"""
    from app.worker.judge_worker import process_exam_answer
    from app.config import Settings
    import fakeredis

    aid = _setup_exam_answer(db_session_factory)
    # backend/.env points production-like runs at the shared judge directory;
    # this test specifically covers the TemporaryDirectory fallback branch.
    settings = Settings(judge_work_dir="", judge_host_work_dir="")

    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)

    with db_session_factory() as db:
        # 模拟 tempfile.TemporaryDirectory 创建失败（在 claim 之后、Docker 执行之前）
        with patch("tempfile.TemporaryDirectory",
                   side_effect=OSError("磁盘已满，无法创建临时目录")):
            result = process_exam_answer(db, fakeredis.FakeStrictRedis(), settings, aid)

        ans = db.get(ExamAnswer, aid)
        # 异常在 claim 之后、complete 之前发生，应退回 pending
        assert ans.grading_status in ("pending", "running"), \
            f"未知异常应退回 pending/running: {ans.grading_status}"
        assert ans.last_error is not None


# ═══════════════════════════════════════════════════════════════
# 5. 进程崩溃后考试 stal-running 触发汇总
# ═══════════════════════════════════════════════════════════════

def test_stale_running_exam_trigger_finalize(db_session_factory):
    """考试编程题 stal-running 被重置后，最终汇总应能正确执行"""
    aid = _setup_exam_answer(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)
        claim_job(db, job_type="exam", object_id=aid)

        # 模拟崩溃
        ans = db.get(ExamAnswer, aid)
        ans.started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        ans.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db.commit()

        # 恢复扫描
        stats = requeue_stale_jobs(db, job_type="exam", stale_running_seconds=300)
        assert stats["running_reset"] >= 1

        ans2 = db.get(ExamAnswer, aid)
        assert ans2.grading_status == "pending"

    # 重新入队并成功判题
    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)
        ans = db.get(ExamAnswer, aid)
        assert ans.grading_status == "queued"
        assert ans.attempt_count == 2  # 第二次尝试


# ═══════════════════════════════════════════════════════════════
# P0-1: 考试重复判题——claim_job 失败必须立即返回
# ═══════════════════════════════════════════════════════════════

def test_p0_1_duplicate_exam_message_not_judged_twice(db_session_factory):
    """P0-1: claim_job 抢占失败必须立即返回，不能绕过抢占继续执行 Docker"""
    from app.worker.judge_worker import process_exam_answer
    from app.config import get_settings
    import fakeredis

    aid = _setup_exam_answer(db_session_factory)
    settings = get_settings()

    # 入队 → status = queued
    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)

    docker_call_count = 0

    def counting_docker(*args, **kwargs):
        nonlocal docker_call_count
        docker_call_count += 1
        return ("1 passed", "", 0, 150)

    # Step 1: Worker-1 抢占成功（claim_job 把 queued→running），但尚未执行 Docker
    with db_session_factory() as db_w1:
        claim_job(db_w1, job_type="exam", object_id=aid)

    # Step 2: Worker-2 收到重复 Redis 消息，调用 process_exam_answer
    #   answer.grading_status 在 DB 中是 "running"（Worker-1 抢占的）
    #   claim_job 返回 False（因为 grading_status 不是 queued）
    #   → 应该立即返回，不执行 Docker
    with db_session_factory() as db_w2:
        with patch("app.worker.judge_worker._run_docker_pytest", side_effect=counting_docker):
            result2 = process_exam_answer(db_w2, fakeredis.FakeStrictRedis(), settings, aid)

    # 断言：Docker 调用次数必须是 0（Worker-2 claim 失败后不容许绕过抢占）
    assert docker_call_count == 0, \
        f"claim_job 失败后不应执行 Docker，实际调用了 {docker_call_count} 次"
