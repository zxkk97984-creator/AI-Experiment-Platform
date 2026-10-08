"""Exercise the real consumer loop against Redis list semantics and durable claims."""
from datetime import timedelta
import json
import sys

import fakeredis
import pytest
import redis

from app.models import Submission
from app.services import judge_queue
from app.services.time_utils import utc_now
from app.worker import judge_worker as worker
from conftest import seed_basic_environment
from test_judge_queue_reliability import _setup_assignment_submission
from test_judge_worker_loop_recovery import Loop, StopLoop


@pytest.fixture(autouse=True)
def _auto_seed_basic_environment():
    """Most cases use no DB; claim/producer cases seed their own temporary DB."""


def _message(ident, kind="assignment", **extra):
    return json.dumps({"type": kind, "id": ident, **extra})


def _redis_loop(monkeypatch, *, role="judge", queue_name="judge:queue", limit=None, after_pop=None):
    loop = Loop(monkeypatch, role=role, process_seconds=0)
    loop.settings.judge_queue_name = queue_name
    client = fakeredis.FakeRedis(decode_responses=True, encoding_errors="replace")
    popped = []

    def wrap(method):
        original = getattr(client, method)

        def pop(queues, timeout):
            assert isinstance(queues, (list, tuple))
            if ((limit is not None and len(popped) >= limit)
                    or not any(client.llen(queue) for queue in queues)):
                raise StopLoop()
            result = original(queues, timeout=timeout)
            popped.append((method, tuple(queues), result))
            if after_pop is not None:
                after_pop(client, result)
            return result
        return pop

    # Both methods delegate to actual fakeredis behavior. This makes the RED
    # run expose the old BRPOP ordering rather than failing on a missing mock.
    monkeypatch.setattr(client, "blpop", wrap("blpop"))
    monkeypatch.setattr(client, "brpop", wrap("brpop"))
    loop.client = client
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: client)
    return loop, client, popped


@pytest.mark.parametrize("role", ["judge", "ai"])
def test_existing_rpush_messages_are_consumed_fifo_without_clearing_other_queues(monkeypatch, role):
    loop, client, popped = _redis_loop(monkeypatch, role=role)
    queue = "judge:ai:queue" if role == "ai" else "judge:queue"
    untouched = "judge:queue" if role == "ai" else "judge:ai:queue"
    kind = "ai_grade" if role == "ai" else "assignment"
    client.rpush(queue, *[_message(ident, kind, attempt=0 if role == "ai" else 1) for ident in (1, 2, 3)])
    client.rpush(untouched, "retain-existing-message")
    loop.run()
    assert loop.processed == [1, 2, 3]
    assert all(method == "blpop" for method, _queues, _result in popped)
    assert client.lrange(untouched, 0, -1) == ["retain-existing-message"]
    assert client.llen(queue) == 0


def test_continuous_assignment_arrivals_do_not_starve_exams(monkeypatch):
    next_assignment = iter(range(2, 20))

    def refill(client, result):
        if result[0] == "judge:queue":
            client.rpush("judge:queue", _message(next(next_assignment)))

    loop, client, popped = _redis_loop(monkeypatch, limit=6, after_pop=refill)
    client.rpush("judge:queue", _message(1))
    client.rpush(worker.EXAM_JUDGE_QUEUE, _message(101, "exam"), _message(102, "exam"))
    loop.run()
    assert loop.processed == [1, 101, 2, 102, 3, 4]
    assert [result[0] for _method, _queues, result in popped[:4]] == [
        "judge:queue", worker.EXAM_JUDGE_QUEUE, "judge:queue", worker.EXAM_JUDGE_QUEUE]
    assert loop.clock.sleeps == []


@pytest.mark.parametrize("raw", [
    pytest.param(b"\xff", id="invalid_utf8"),
    "PRIVATE_MESSAGE_SENTINEL malformed-json",
    pytest.param(None, id="deeply_nested_json"),
    "null", "[]", '"PRIVATE_MESSAGE_SENTINEL"',
    _message(None), _message(True), _message(0), _message(-1), _message("1"),
    json.dumps({"id": 1}), _message(1, []), _message(1, "PRIVATE_MESSAGE_SENTINEL"),
    _message(1, attempt=-1), _message(1, attempt=True), _message(1, attempt="1"),
    _message(1, "exam"), _message(1, "ai_grade"),
])
def test_invalid_messages_rotate_then_allow_other_queue_without_logging_payload(monkeypatch, caplog, raw):
    if raw is None:
        depth = max(10_000, sys.getrecursionlimit() * 10)
        raw = "[" * depth + '"PRIVATE_MESSAGE_SENTINEL"' + "]" * depth
        with pytest.raises(RecursionError):
            json.loads(raw)
    loop, client, popped = _redis_loop(monkeypatch)
    client.rpush("judge:queue", raw, _message(2))
    client.rpush(worker.EXAM_JUDGE_QUEUE, _message(101, "exam"))
    loop.run()
    assert loop.processed == [101, 2]
    assert [result[0] for _method, _queues, result in popped] == [
        "judge:queue", worker.EXAM_JUDGE_QUEUE, "judge:queue"]
    assert "PRIVATE_MESSAGE_SENTINEL" not in caplog.text


def test_duplicate_assignment_messages_still_rotate_and_claim_only_once(monkeypatch, db_session_factory):
    seed_basic_environment(db_session_factory)
    ident = _setup_assignment_submission(db_session_factory)
    with db_session_factory() as db:
        db.get(Submission, ident).grading_status = "queued"
        db.commit()
    loop, client, popped = _redis_loop(monkeypatch)
    client.rpush("judge:queue", _message(ident), _message(ident))
    client.rpush(worker.EXAM_JUDGE_QUEUE, _message(101, "exam"))
    executed, attempted = [], []

    def process(db, _client, _settings, job_id):
        attempted.append(job_id)
        if judge_queue.claim_job(db, job_type="assignment", object_id=job_id):
            executed.append(job_id)
            judge_queue.complete_job(db, job_type="assignment", object_id=job_id, score=10)

    monkeypatch.setattr(worker, "SessionLocal", db_session_factory)
    monkeypatch.setattr(worker, "process_submission", process)
    loop.run()
    assert attempted == [ident, ident]
    assert executed == [ident]
    assert [result[0] for _method, _queues, result in popped] == [
        "judge:queue", worker.EXAM_JUDGE_QUEUE, "judge:queue"]
    with db_session_factory() as db:
        assert db.get(Submission, ident).grading_status == "completed"


@pytest.mark.parametrize("state", ["new", "stale_pending", "stale_queued"])
def test_custom_assignment_queue_is_shared_by_producer_recovery_and_consumer(
    monkeypatch, db_session_factory, state,
):
    seed_basic_environment(db_session_factory)
    ident = _setup_assignment_submission(db_session_factory)
    custom_queue = "school:grading:assignments"
    loop, client, popped = _redis_loop(monkeypatch, queue_name=custom_queue)
    monkeypatch.setattr(judge_queue, "get_settings", lambda: loop.settings)
    monkeypatch.setattr(judge_queue, "_get_redis", lambda: client)
    with db_session_factory() as db:
        if state == "new":
            assert judge_queue.enqueue_job(db, job_type="assignment", object_id=ident)
        else:
            submission = db.get(Submission, ident)
            old = utc_now() - timedelta(minutes=10)
            submission.created_at = old
            if state == "stale_queued":
                submission.grading_status = "queued"
                submission.queued_at = old
                submission.attempt_count = 1
            db.commit()
            stats = judge_queue.requeue_stale_jobs(db, job_type="assignment")
            assert stats["queued_repushed" if state == "stale_queued" else "pending_requeued"] == 1
    assert judge_queue.JUDGE_QUEUE == "judge:queue"
    assert client.llen(judge_queue.JUDGE_QUEUE) == 0
    queued = [json.loads(raw) for raw in client.lrange(custom_queue, 0, -1)]
    assert len(queued) == 1 and queued[0]["id"] == ident and queued[0]["type"] == "assignment"
    loop.run()
    assert loop.processed == [ident]
    assert popped[0][2][0] == custom_queue
