"""Task 1: 判题队列可靠性测试——幂等入队、条件抢占、统一协议、恢复扫描

A/B/C 分类：B 类——本文件全部用例依赖判题队列行为，默认用 fakeredis 打桩
`_get_redis`（CI SQLite job 没有 Redis 服务，直连 localhost:6379 会
ConnectionRefused 导致「已入队但断言 Redis 副作用」的用例假失败）；
需要断言断连语义的用例在自身上下文内显式 patch 覆盖本默认。
"""
import json as _json

import pytest
from unittest.mock import MagicMock, patch

from app.models import ExamAnswer, ExamQuestion, ExamSubmission, JudgeQuestion, Submission
from app.services.judge_queue import (
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    requeue_stale_jobs,
    RETRYABLE_STATUSES,
    MAX_ATTEMPTS,
)
from conftest import auth_header, create_user, login, seed_basic_environment


@pytest.fixture(autouse=True)
def _default_fake_redis(redis_client):
    """全文件默认：队列服务 `_get_redis` 返回 fakeredis（不依赖本地 Redis 服务）"""
    with patch("app.services.judge_queue._get_redis", return_value=redis_client):
        yield


def _setup_assignment_submission(db_session_factory):
    """创建一个 pending 状态的作业 Submission"""
    with db_session_factory() as db:
        # 创建必要的外键记录
        from app.models import Assignment, Course, User
        teacher = User(username="q_t", real_name="QT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="q_s", real_name="QS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()

        course = Course(title="QC", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()

        assignment = Assignment(course_id=course.id, title="QA", status="published")
        db.add(assignment)
        db.flush()

        question = JudgeQuestion(
            assignment_id=assignment.id, title="QQ",
            function_name="add", hidden_tests="assert True",
            public_cases=[{"args": [1, 2], "expected": 3}],
        )
        db.add(question)
        db.flush()

        submission = Submission(
            question_id=question.id, student_id=student.id,
            code="def add(a,b): return a+b",
            status="queued", grading_status="pending",
        )
        db.add(submission)
        db.commit()
        return submission.id


def _setup_exam_answer(db_session_factory):
    """创建一个 pending 状态的 ExamAnswer"""
    with db_session_factory() as db:
        from app.models import Course, Exam, User
        teacher = User(username="qe_t", real_name="QET", role="teacher", status="active",
                       password_hash="x")
        student = User(username="qe_s", real_name="QES", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()

        course = Course(title="QEC", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()

        exam = Exam(course_id=course.id, title="QEE", status="published",
                    duration_minutes=60)
        db.add(exam)
        db.flush()

        eq = ExamQuestion(
            exam_id=exam.id, question_type="code",
            prompt="code q", correct_answer={}, points=10,
            hidden_tests="assert True",
        )
        db.add(eq)
        db.flush()

        sub = ExamSubmission(exam_id=exam.id, student_id=student.id,
                             status="grading")
        db.add(sub)
        db.flush()

        ans = ExamAnswer(
            submission_id=sub.id, question_id=eq.id,
            code_answer="def add(a,b): return a+b",
            grading_status="pending",
        )
        db.add(ans)
        db.commit()
        return ans.id


# ═══════════════════════════════════════════════════════════════
# 1. 重复入队只产生一条有效任务
# ═══════════════════════════════════════════════════════════════

def test_duplicate_enqueue_only_one_valid(db_session_factory):
    """重复调用 enqueue_job 只有第一次成功，后续返回 False"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        # 第一次入队：pending → queued，成功
        ok1 = enqueue_job(db, job_type="assignment", object_id=sid)
        assert ok1

        # 第二次入队：状态已是 queued，条件更新不命中
        ok2 = enqueue_job(db, job_type="assignment", object_id=sid)
        assert not ok2

        # 验证 DB 状态
        sub = db.get(Submission, sid)
        assert sub.grading_status == "queued"
        assert sub.attempt_count == 1


def test_duplicate_enqueue_exam_only_one_valid(db_session_factory):
    """考试题重复入队同样幂等"""
    aid = _setup_exam_answer(db_session_factory)

    with db_session_factory() as db:
        ok1 = enqueue_job(db, job_type="exam", object_id=aid)
        assert ok1

        ok2 = enqueue_job(db, job_type="exam", object_id=aid)
        assert not ok2

        ans = db.get(ExamAnswer, aid)
        assert ans.grading_status == "queued"
        assert ans.attempt_count == 1


# ═══════════════════════════════════════════════════════════════
# 2. Redis 故障不会丢失数据库任务
# ═══════════════════════════════════════════════════════════════

def test_redis_failure_preserves_db_state(db_session_factory):
    """Redis 不可用时，DB 状态仍然从 pending 转为 queued，任务不丢失"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        # 模拟 Redis 故障
        with patch("app.services.judge_queue._get_redis", side_effect=ConnectionError("redis down")):
            ok = enqueue_job(db, job_type="assignment", object_id=sid)

        # 虽然 Redis 推送失败，但 DB 状态已正确更新
        assert ok

        sub = db.get(Submission, sid)
        assert sub.grading_status == "queued", f"Redis故障时DB状态应为queued，实际: {sub.grading_status}"
        assert sub.attempt_count == 1

    # 恢复扫描应能发现 queued 超时任务并重新推送
    with db_session_factory() as db:
        from datetime import timedelta
        # 手动设置 queued_at 为过去时间
        sub = db.get(Submission, sid)
        sub.queued_at = sub.queued_at - timedelta(seconds=300)
        db.commit()

        with patch("app.services.judge_queue._get_redis") as mock_redis:
            mock_r = MagicMock()
            mock_redis.return_value = mock_r
            stats = requeue_stale_jobs(db, job_type="assignment",
                                       stale_queued_seconds=120)
            assert stats["queued_repushed"] >= 1, f"恢复扫描应重新推送queued任务: {stats}"
            assert mock_r.rpush.called


# ═══════════════════════════════════════════════════════════════
# 3. 统一消息协议
# ═══════════════════════════════════════════════════════════════

def test_unified_message_format(db_session_factory):
    """enqueue_job 推送的 Redis 消息使用统一格式 {"type":"...","id":...,"attempt":...}"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        with patch("app.services.judge_queue._get_redis") as mock_redis:
            mock_r = MagicMock()
            mock_redis.return_value = mock_r
            enqueue_job(db, job_type="assignment", object_id=sid)

            # 验证推送的消息格式
            assert mock_r.rpush.called
            call_args = mock_r.rpush.call_args
            queue_name = call_args[0][0]
            raw_message = call_args[0][1]

            msg = _json.loads(raw_message)
            assert msg["type"] == "assignment", f"type 应为 assignment: {msg}"
            assert msg["id"] == sid, f"id 应为 {sid}: {msg}"
            assert msg["attempt"] == 1, f"attempt 应为 1: {msg}"
            assert queue_name == "judge:queue"

    # 考试消息格式
    aid = _setup_exam_answer(db_session_factory)
    with db_session_factory() as db:
        with patch("app.services.judge_queue._get_redis") as mock_redis:
            mock_r = MagicMock()
            mock_redis.return_value = mock_r
            enqueue_job(db, job_type="exam", object_id=aid)

            assert mock_r.rpush.called
            raw_message = mock_r.rpush.call_args[0][1]
            msg = _json.loads(raw_message)
            assert msg["type"] == "exam", f"type 应为 exam: {msg}"
            assert msg["id"] == aid, f"id 应为 {aid}: {msg}"
            assert msg["attempt"] == 1, f"attempt 应为 1: {msg}"


# ═══════════════════════════════════════════════════════════════
# 4. 条件更新防止竞态
# ═══════════════════════════════════════════════════════════════

def test_claim_job_atomic_grab(db_session_factory):
    """claim_job 使用条件 UPDATE 实现原子抢占，第二个 Worker 抢占失败"""
    sid = _setup_assignment_submission(db_session_factory)

    # 先入队
    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)

    # 两个"Worker"同时尝试抢占
    with db_session_factory() as db1:
        ok1 = claim_job(db1, job_type="assignment", object_id=sid)
        assert ok1

        # 第二个会话独立操作
        with db_session_factory() as db2:
            ok2 = claim_job(db2, job_type="assignment", object_id=sid)
            assert not ok2, "第二个Worker应抢占失败"

    # 验证最终状态
    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        assert sub.grading_status == "running"
        assert sub.started_at is not None


def test_claim_job_exam_atomic_grab(db_session_factory):
    """考试答案的 claim_job 同样原子抢占"""
    aid = _setup_exam_answer(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="exam", object_id=aid)

    with db_session_factory() as db1:
        ok1 = claim_job(db1, job_type="exam", object_id=aid)
        assert ok1

        with db_session_factory() as db2:
            ok2 = claim_job(db2, job_type="exam", object_id=aid)
            assert not ok2

    with db_session_factory() as db:
        ans = db.get(ExamAnswer, aid)
        assert ans.grading_status == "running"


# ═══════════════════════════════════════════════════════════════
# 5. complete_job / fail_job 状态转换
# ═══════════════════════════════════════════════════════════════

def test_complete_job_transition(db_session_factory):
    """complete_job: running → completed，写入分数"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)
        complete_job(db, job_type="assignment", object_id=sid,
                     score=85.0, result_details={"returncode": 0})

        sub = db.get(Submission, sid)
        assert sub.grading_status == "completed"
        assert sub.score == 85.0
        assert sub.finished_at is not None
        assert sub.result_details == {"returncode": 0}


def test_fail_job_retryable(db_session_factory):
    """fail_job retryable=True: 退回 pending 状态"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)
        fail_job(db, job_type="assignment", object_id=sid,
                 error="Worker 崩溃", retryable=True)

        sub = db.get(Submission, sid)
        assert sub.grading_status == "pending", f"可重试失败应退回pending: {sub.grading_status}"
        assert sub.last_error == "Worker 崩溃"


def test_fail_job_permanent(db_session_factory):
    """fail_job retryable=False: 进入 system_error 终态"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)
        fail_job(db, job_type="assignment", object_id=sid,
                 error="题目不存在", retryable=False)

        sub = db.get(Submission, sid)
        assert sub.grading_status == "system_error"
        assert sub.last_error == "题目不存在"


# ═══════════════════════════════════════════════════════════════
# 6. system_error 不可自动复活（只接受显式受控重试）
# ═══════════════════════════════════════════════════════════════

def test_system_error_not_auto_retried(db_session_factory):
    """system_error 是终态：自动 enqueue_job 不得复活（显式 retry 才可）"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)
        fail_job(db, job_type="assignment", object_id=sid,
                 error="临时错误", retryable=False)
        assert db.get(Submission, sid).grading_status == "system_error"

        # 自动入队拒绝 system_error：不再复活
        ok = enqueue_job(db, job_type="assignment", object_id=sid)
        assert not ok, "system_error 不应被自动入队复活"
        sub = db.get(Submission, sid)
        assert sub.grading_status == "system_error"
        assert sub.attempt_count == 1  # 不再递增


# ═══════════════════════════════════════════════════════════════
# 7. 恢复扫描
# ═══════════════════════════════════════════════════════════════

def test_requeue_stale_pending_jobs(db_session_factory):
    """恢复扫描：pending 超时任务重新入队"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        # 手动设置 created_at 为过去（SQLite 不支持 ALTER，用 update 绕过）
        from datetime import timedelta
        sub = db.get(Submission, sid)
        # 直接在 Python 对象上无法修改 created_at（server_default），
        # 改为验证 pending 任务能被扫描统计
        stats = requeue_stale_jobs(db, job_type="assignment",
                                   stale_pending_seconds=0,
                                   stale_queued_seconds=120,
                                   stale_running_seconds=300)
        # pending 任务且 attempt_count=0 < MAX_ATTEMPTS 且 created_at < now
        # stale_pending_seconds=0 表示所有 pending 都算过期
        assert stats["pending_requeued"] >= 1, f"应扫描到 pending 任务: {stats}"


def test_exam_recovery_ignores_pending_answers_outside_grading(db_session_factory):
    """恢复扫描不得把非 grading 提交中的答案误当成可执行判题任务。

    这类答案在演示数据中代表尚未正式交卷或已经等待人工复核的历史快照；
    若被重新入队，Worker 会绕过提交状态并把无隐藏测试的选择题写成
    system_error。
    """
    with db_session_factory() as db:
        from app.models import Course, Exam, User

        teacher = User(
            username="qe_non_grading_t", real_name="QENGT", role="teacher",
            status="active", password_hash="x",
        )
        student = User(
            username="qe_non_grading_s", real_name="QENGS", role="student",
            status="active", password_hash="x",
        )
        db.add_all([teacher, student])
        db.flush()

        course = Course(title="QENGC", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        exam = Exam(course_id=course.id, title="QENGE", status="published", duration_minutes=60)
        db.add(exam)
        db.flush()
        question = ExamQuestion(
            exam_id=exam.id,
            question_type="single_choice",
            prompt="选择题",
            options={"A": "正确"},
            correct_answer={"correct": ["A"]},
            points=10,
        )
        db.add(question)
        db.flush()
        submission = ExamSubmission(
            exam_id=exam.id,
            student_id=student.id,
            status="submitted",
        )
        db.add(submission)
        db.flush()
        answer = ExamAnswer(
            submission_id=submission.id,
            question_id=question.id,
            selected_options=["A"],
            grading_status="pending",
        )
        db.add(answer)
        db.commit()
        answer_id = answer.id

        stats = requeue_stale_jobs(db, job_type="exam", stale_pending_seconds=0)

        db.refresh(answer)
        assert stats["pending_requeued"] == 0
        assert answer.id == answer_id
        assert answer.grading_status == "pending"


def test_max_retries_system_error(db_session_factory):
    """超过最大重试次数的 pending 任务被标记为 system_error"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        sub.attempt_count = MAX_ATTEMPTS  # 已达最大重试
        db.commit()

        stats = requeue_stale_jobs(db, job_type="assignment",
                                   stale_pending_seconds=0)
        assert stats["max_retries_reached"] >= 1

        sub2 = db.get(Submission, sid)
        assert sub2.grading_status == "system_error"


def test_requeue_stale_running_jobs(db_session_factory):
    """恢复扫描：running 超时任务重置为 pending"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)
        claim_job(db, job_type="assignment", object_id=sid)

        # 手动设置 started_at 为过去时间
        from datetime import timedelta
        sub = db.get(Submission, sid)
        sub.started_at = sub.started_at - timedelta(seconds=600)
        # 存活信号是 heartbeat_at（认领时写入、判题在测试组间续期）；
        # 只回拨 started_at 已不足以模拟"Worker 失联"。
        sub.heartbeat_at = sub.heartbeat_at - timedelta(seconds=600)
        db.commit()

        stats = requeue_stale_jobs(db, job_type="assignment",
                                   stale_running_seconds=300)
        assert stats["running_reset"] >= 1, f"running超时应被重置: {stats}"

        sub2 = db.get(Submission, sid)
        assert sub2.grading_status == "pending", f"应为pending: {sub2.grading_status}"
        assert "stale running" in (sub2.last_error or "")


# ═══════════════════════════════════════════════════════════════
# P0-2: 最大重试终态完整 + queued 去重
# ═══════════════════════════════════════════════════════════════

def test_p0_2_max_retries_syncs_submission_status(db_session_factory):
    """P0-2: 达到 MAX_ATTEMPTS 时 Submission.status 也设为 system_error，前端停止轮询"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        sub = db.get(Submission, sid)
        sub.attempt_count = MAX_ATTEMPTS
        db.commit()

        stats = requeue_stale_jobs(db, job_type="assignment", stale_pending_seconds=0)
        assert stats["max_retries_reached"] >= 1

        sub2 = db.get(Submission, sid)
        assert sub2.grading_status == "system_error"
        # P0-2 关键断言：前端读取的 status 字段也必须是 system_error
        assert sub2.status == "system_error", \
            f"前端轮询的 status 应为 system_error，实际: {sub2.status}"
        assert sub2.score is None  # 系统错误不扣分（第六轮修正）


def test_p0_2_exam_max_retries_immediate_finalize(db_session_factory):
    """P0-2: 考试答案超过最大重试→system_error→父级立即转 review_required（不按零分结算）"""
    aid = _setup_exam_answer(db_session_factory)

    with db_session_factory() as db:
        ans = db.get(ExamAnswer, aid)
        ans.attempt_count = MAX_ATTEMPTS
        db.commit()

        stats = requeue_stale_jobs(db, job_type="exam", stale_pending_seconds=0)
        assert stats["max_retries_reached"] >= 1

        # system_error 答案不可作为零分结算：父级转入 review_required 终态
        sub = db.get(ExamSubmission, ans.submission_id)
        assert sub.status == "review_required", \
            f"system_error 后父级应转 review_required，实际: {sub.status}"
        assert sub.review_required_at is not None
        assert _count_exam_grades(db, sub) == 0, "系统错误不得创建 ExamGrade"


def _count_exam_grades(db, sub):
    from app.models import ExamGrade
    from sqlalchemy import func, select
    return db.scalar(
        select(func.count()).select_from(ExamGrade).where(
            ExamGrade.exam_id == sub.exam_id,
            ExamGrade.student_id == sub.student_id,
        )
    ) or 0


def test_p0_2_queued_repush_updates_queued_at(db_session_factory):
    """P0-2: queued 重新推送后更新 queued_at，防止每 15 秒无限重复推送"""
    sid = _setup_assignment_submission(db_session_factory)

    with db_session_factory() as db:
        enqueue_job(db, job_type="assignment", object_id=sid)

        # 手动设 queued_at 为过去
        from datetime import datetime as dt, timedelta, timezone as tz
        sub = db.get(Submission, sid)
        sub.queued_at = dt.now(tz.utc) - timedelta(seconds=300)
        old_queued_at = sub.queued_at
        db.commit()

        # 第一次扫描：因为 queued 超时，应重新推送
        stats1 = requeue_stale_jobs(db, job_type="assignment", stale_queued_seconds=120)
        assert stats1["queued_repushed"] >= 1

        # 重新加载——SQLite 可能返回 naive，用 replace 规范化
        sub2 = db.get(Submission, sid)
        new_queued = sub2.queued_at
        if new_queued.tzinfo is None:
            new_queued = new_queued.replace(tzinfo=tz.utc)
        # queued_at 应已刷新到接近现在（与 300 秒前有明显差距）
        assert new_queued > old_queued_at, \
            f"queued_at 应在重新推送后更新: old={old_queued_at}, new={new_queued}"

    # 第二次扫描：因为 queued_at 刚被刷新为接近当前时间，不应再次推送
    with db_session_factory() as db:
        stats2 = requeue_stale_jobs(db, job_type="assignment", stale_queued_seconds=120)
        assert stats2["queued_repushed"] == 0, \
            f"queued_at 刷新后不应重复推送: {stats2}"
