"""Task 8: AI 评分管道测试——队列、幂等、状态转换

A/B/C 分类：B 类（最小父行）——CodeGrade 的 submission/rubric 外键经
共享工厂 make_submission / make_rubric 建真实父行。
"""
import json

import httpx
import pytest

from app.config import Settings
from app.services.ai_client import DeepSeekClient

from conftest import make_rubric, make_submission


def _make_fake_client():
    """构建返回合法 AI 评分结果的客户端"""
    data = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 16,
            "dimension_max": 20,
            "items": [
                {"criterion_id": "A1", "criterion": "搜索区间", "level": "complete", "score": 10, "max_score": 10, "code_lines": [1, 2], "evidence": "ok"},
                {"criterion_id": "A2", "criterion": "缩小范围", "level": "partial", "score": 6, "max_score": 10, "code_lines": [3], "evidence": "partial"},
            ],
        },
        "code_quality": {
            "dimension_score": 8,
            "dimension_max": 10,
            "items": [],
        },
        "triggered_cap_rule_ids": [],
        "uncertainties": [],
        "needs_teacher_review": False,
        "review_reason": None,
        "student_feedback": {"strengths": [], "issues": [], "suggestions": []},
    }

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(data)}}]
        })

    settings = Settings(
        _env_file=None, ai_base_url="https://aihub.codingpython.cn",
        ai_model="deepseek-v4-flash", ai_api_key="test-key", ai_max_retries=0,
    )
    return DeepSeekClient(settings, transport=httpx.MockTransport(handler))


# ── 队列操作测试 ──


def test_enqueue_transitions_pending_to_queued(db_session_factory, redis_client):
    """enqueue 将 pending → queued 并推送 Redis 消息"""
    from app.models import CodeGrade
    from app.services.ai_grading_queue import enqueue_ai_grade

    submission_id = make_submission(db_session_factory)
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="shadow", status="pending",
        )
        db.add(grade)
        db.flush()

        ok = enqueue_ai_grade(db, redis_client, grade.id)
        assert ok

        db.refresh(grade)
        assert grade.status == "queued"

        # Redis 中应有消息
        msg = redis_client.rpop("judge:ai:queue")
        assert msg is not None
        parsed = json.loads(msg)
        assert parsed["type"] == "ai_grade"
        assert parsed["id"] == grade.id


def test_enqueue_twice_only_queues_once(db_session_factory, redis_client):
    """重复入队不改变状态"""
    from app.models import CodeGrade
    from app.services.ai_grading_queue import enqueue_ai_grade

    submission_id = make_submission(db_session_factory)
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="shadow", status="pending",
        )
        db.add(grade)
        db.flush()

        assert enqueue_ai_grade(db, redis_client, grade.id) is True
        # 清除 Redis
        redis_client.rpop("judge:ai:queue")

        # 第二次入队——状态已不是 pending
        assert enqueue_ai_grade(db, redis_client, grade.id) is False


def test_claim_queued_to_running(db_session_factory, redis_client):
    """claim 将 queued → running"""
    from app.models import CodeGrade
    from app.services.ai_grading_queue import claim_ai_grade, enqueue_ai_grade

    submission_id = make_submission(db_session_factory)
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="shadow", status="pending",
        )
        db.add(grade)
        db.flush()
        enqueue_ai_grade(db, redis_client, grade.id)

        ok = claim_ai_grade(db, grade.id)
        assert ok

        db.refresh(grade)
        assert grade.status == "running"
        assert grade.attempt_count == 1


def test_fail_retryable_returns_to_queued(db_session_factory, redis_client):
    """可重试失败退回 queued（确保 claim_ai_grade 可领取）"""
    from app.models import CodeGrade
    from app.services.ai_grading_queue import claim_ai_grade, fail_ai_grade

    submission_id = make_submission(db_session_factory)
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="shadow", status="queued",
            attempt_count=1,
        )
        db.add(grade)
        db.flush()
        # 失败写入受认领栅栏保护：必须先认领（真实 Worker 序列）
        assert claim_ai_grade(db, grade.id)

        fail_ai_grade(db, redis_client, grade.id, "timeout", retryable=True, max_attempts=3)

        db.refresh(grade)
        assert grade.status == "queued"


def test_fail_non_retryable_goes_to_review(db_session_factory, redis_client):
    """不可重试失败进入 review_required"""
    from app.models import CodeGrade
    from app.services.ai_grading_queue import claim_ai_grade, fail_ai_grade

    submission_id = make_submission(db_session_factory)
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="shadow", status="queued",
            attempt_count=1,
        )
        db.add(grade)
        db.flush()
        assert claim_ai_grade(db, grade.id)

        fail_ai_grade(db, redis_client, grade.id, "bad json", retryable=False)

        db.refresh(grade)
        assert grade.status == "review_required"
        assert grade.needs_teacher_review is True


def test_complete_sets_status(db_session_factory, redis_client):
    """complete 设置 completed 状态"""
    from app.models import CodeGrade
    from app.services.ai_grading_queue import claim_ai_grade, complete_ai_grade

    submission_id = make_submission(db_session_factory)
    rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode="shadow", status="queued",
        )
        db.add(grade)
        db.flush()
        # 完成写入受认领栅栏保护：必须先认领（真实 Worker 序列）
        assert claim_ai_grade(db, grade.id)

        assert complete_ai_grade(db, grade.id) is True

        db.refresh(grade)
        assert grade.status == "completed"
        assert grade.finished_at is not None
