"""MySQL 上的认领代次与过期恢复（显式过去时间，不依赖"0 秒立即过期"）。

SQLite 通过不能替代 MySQL：`DATETIME(0)` 会截断微秒，正是历史上
"running_reset 长期为 0"的根因。本用例在专用空 schema 上用**明确的过去时间**
建立过期状态，验证：

1. 确实过期的 running 任务被恢复为 pending（token 释放、修订号不变）；
2. 恢复后旧代次的写入一律被栅栏拒绝；
3. 结果写入原子推进结果修订号（result_revision）。

需要 ``DAI_TEST_JUDGE_MYSQL_URL`` 指向专用空 schema ``dai_judge_claim_verify``；
未提供时跳过，且不计通过。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import select, update

from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.models import (
    Assignment,
    Course,
    EnvironmentProfile,
    EnvironmentVersion,
    JudgeQuestion,
    Submission,
    User,
)
from app.services.judge_queue import (
    claim_job,
    complete_job,
    enqueue_job,
    requeue_stale_jobs,
)

pytestmark = pytest.mark.no_auto_env_seed

STALE_SECONDS = 1800


def _engine_or_skip():
    raw = os.environ.get("DAI_TEST_JUDGE_MYSQL_URL")
    if not raw:
        pytest.skip("Requires DAI_TEST_JUDGE_MYSQL_URL pointing to empty dai_judge_claim_verify schema")
    url = sa.engine.make_url(raw)
    if url.get_backend_name() != "mysql" or url.database != "dai_judge_claim_verify":
        pytest.skip("Requires reserved disposable dai_judge_claim_verify MySQL schema")
    engine = create_engine_from_url(raw)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture()
def mysql_session_factory():
    engine = _engine_or_skip()
    factory = sessionmaker_for_engine(engine)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed_submission(factory) -> int:
    with factory() as db:
        teacher = User(username="mq_t", real_name="MQT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="mq_s", real_name="MQS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        profile = EnvironmentProfile(slug="mq-basic", display_name="mq", status="active")
        db.add(profile)
        db.flush()
        version = EnvironmentVersion(
            profile_id=profile.id, version_number=1, status="available",
            base_image_ref="python:3.12-slim", image_digest="sha256:" + "1" * 64,
            minimum_memory_mb=256, manifest_sha256="c" * 64,
        )
        db.add(version)
        db.flush()
        course = Course(title="MQC", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(course_id=course.id, title="MQA", status="published",
                                environment_version_id=version.id)
        db.add(assignment)
        db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title="MQQ", function_name="f",
                                 hidden_tests="assert True", public_cases=[],
                                 test_groups=[{"id": "F1", "name": "F", "dimension": "F",
                                               "max_score": 60, "tests": "def test(): pass"}])
        db.add(question)
        db.flush()
        submission = Submission(question_id=question.id, student_id=student.id,
                                environment_version_id=version.id,
                                code="def f(): pass", status="running",
                                grading_status="queued")
        db.add(submission)
        db.commit()
        return submission.id


def test_expired_claim_is_recovered_with_explicit_past_time(mysql_session_factory):
    submission_id = _seed_submission(mysql_session_factory)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS + 3600)

    with mysql_session_factory() as db:
        token = claim_job(db, job_type="assignment", object_id=submission_id)
        assert token
        # 明确的过去时间（而不是"阈值 0 秒立即过期"）
        db.execute(update(Submission).where(Submission.id == submission_id)
                   .values(started_at=stale_at, heartbeat_at=stale_at))
        db.commit()

    with mysql_session_factory() as db:
        stats = requeue_stale_jobs(db, job_type="assignment",
                                   stale_running_seconds=STALE_SECONDS)
    assert stats["running_reset"] == 1, stats

    with mysql_session_factory() as db:
        row = db.get(Submission, submission_id)
        assert row.grading_status == "pending"
        assert row.claim_token is None, "恢复必须释放代次"
        assert row.result_revision == 0, "退回 pending 不是结果写入，不推进修订号"

    # 旧 Worker 携带旧 token 迟到写入：必须被拒
    with mysql_session_factory() as db:
        db.info.setdefault("judge_claims", {})[("assignment", submission_id)] = token
        assert complete_job(db, job_type="assignment", object_id=submission_id, score=99.0,
                            values={"status": "accepted"}) is False

    # 新代次：恢复扫描重排（pending → queued）后再认领，与生产序列一致
    with mysql_session_factory() as db:
        assert enqueue_job(db, job_type="assignment", object_id=submission_id) is True
        new_token = claim_job(db, job_type="assignment", object_id=submission_id)
        assert new_token and new_token != token
        assert complete_job(db, job_type="assignment", object_id=submission_id, score=88.0,
                            values={"status": "accepted"}) is True

    with mysql_session_factory() as db:
        row = db.get(Submission, submission_id)
        assert row.score == 88.0
        assert row.grading_status == "completed"
        assert row.result_revision == 1


def test_live_heartbeat_is_not_reset_on_mysql(mysql_session_factory):
    """SELECT 之后续过心跳的任务不得被旧扫描结果重置（MySQL 时间精度下同样成立）。"""
    submission_id = _seed_submission(mysql_session_factory)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS + 3600)

    with mysql_session_factory() as db:
        token = claim_job(db, job_type="assignment", object_id=submission_id)
        assert token
        db.execute(update(Submission).where(Submission.id == submission_id)
                   .values(started_at=stale_at, heartbeat_at=stale_at))
        db.commit()
        # 同一事务内续期：等价于扫描 SELECT 与 UPDATE 之间活着的 Worker 续心跳
        db.execute(update(Submission).where(Submission.id == submission_id)
                   .values(heartbeat_at=datetime.now(timezone.utc)))
        db.commit()

        stats = requeue_stale_jobs(db, job_type="assignment",
                                   stale_running_seconds=STALE_SECONDS)
    assert stats["running_reset"] == 0, stats
    with mysql_session_factory() as db:
        row = db.get(Submission, submission_id)
        assert row.grading_status == "running"
        assert row.claim_token == token
