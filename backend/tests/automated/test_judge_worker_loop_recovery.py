"""Recovery deadlines survive busy queues and failures without external I/O."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
import redis

from app.services import judge_queue
from app.worker import judge_worker as worker


@pytest.fixture(autouse=True)
def _auto_seed_basic_environment():
    """Override the shared database seed: these loop tests use only fakes."""


class StopLoop(BaseException):
    """End a finite fake stream without exercising the worker's error handler."""


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Loop:
    def __init__(self, monkeypatch, *, messages=5, process_seconds=60, role="judge", enabled=True,
                 recovery_results=None, pop_results=None, fail_processing=False, session_failure=None,
                 message_kind="assignment"):
        self.clock = Clock()
        self.recoveries = []
        self.processed = []
        self.events = []
        self.connections = []
        self.pop_calls = []
        self.session_calls = 0
        self.recovery_results = list(recovery_results or [])
        # 批次 2：AI worker 还会消费一键 AI 配置队列，假 settings 必须提供该字段
        self.settings = SimpleNamespace(worker_role=role, ai_enabled=enabled,
                                        judge_queue_name="judge:queue", ai_queue_name="judge:ai:queue",
                                        ai_setup_queue_name="judge:ai-setup:queue",
                                        redis_url="redis://unused.invalid/0")
        kind = "ai_grade" if role == "ai" else message_kind
        queue = (self.settings.ai_queue_name if role == "ai" else worker.EXAM_JUDGE_QUEUE
                 if kind == "exam" else self.settings.judge_queue_name)
        outcomes = iter(pop_results if pop_results is not None else [
            (0, (queue, json.dumps({"type": kind, "id": ident})))
            for ident in range(1, messages + 1)])

        def pop(queues, timeout):
            self.pop_calls.append((tuple(queues), timeout))
            try:
                elapsed, outcome = next(outcomes)
            except StopIteration:
                raise StopLoop()
            self.clock.now += elapsed
            self.events.append(("pop", self.clock.now))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self.client = SimpleNamespace(blpop=pop)

        def from_url(url, **kwargs):
            self.connections.append((url, kwargs))
            return self.client

        @contextmanager
        def session():
            self.session_calls += 1
            session_number = self.session_calls
            self.events.append(("session_enter", self.clock.now))
            try:
                if session_number == (1 if role == "ai" else 2) and session_failure == "enter":
                    self.clock.now += process_seconds
                    raise RuntimeError("synthetic session creation failure")
                yield object()
            except Exception:
                self.events.append(("session_failed", self.clock.now))
                raise
            finally:
                self.events.append(("session_exit", self.clock.now))
                if session_number == (1 if role == "ai" else 2) and session_failure == "exit":
                    raise RuntimeError("synthetic session close failure")

        def recover(_db, client, _owner):
            assert client is self.client
            self.recoveries.append(self.clock.now)
            outcome = self.recovery_results.pop(0) if self.recovery_results else True
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def process(_db, client, settings, ident):
            assert client is self.client and settings is self.settings
            self.processed.append(ident)
            self.clock.now += process_seconds
            if fail_processing and ident == 1:
                raise RuntimeError("synthetic processing failure")

        monkeypatch.setattr(worker, "time", self.clock)
        monkeypatch.setattr(worker, "get_settings", lambda: self.settings)
        monkeypatch.setattr(worker, "SessionLocal", session)
        monkeypatch.setattr(worker, "_recover_stale", recover)
        monkeypatch.setattr(worker, "process_submission", process)
        monkeypatch.setattr(worker, "process_exam_answer", process)
        monkeypatch.setattr(worker, "process_ai_grade", process)
        monkeypatch.setattr(redis.Redis, "from_url", from_url)

    def run(self):
        with pytest.raises(StopLoop):
            worker.run_worker_loop()


def test_busy_worker_recovers_without_waiting_for_an_empty_queue(monkeypatch):
    loop = Loop(monkeypatch)
    loop.run()
    assert loop.processed == [1, 2, 3, 4, 5]
    assert loop.recoveries == [0, 120, 240]
    assert loop.clock.sleeps == []


def test_redis_consume_errors_do_not_skip_due_recovery(monkeypatch):
    loop = Loop(monkeypatch, pop_results=[
        (120, redis.ConnectionError("synthetic connection failure")),
        (120, redis.TimeoutError("synthetic socket timeout")),
    ])
    loop.run()
    assert loop.recoveries == [0, 121, 242]
    assert loop.clock.sleeps == [1, 1]


@pytest.mark.parametrize("failure_at", ["startup", "periodic"])
def test_failed_recovery_retries_after_five_seconds_while_consuming(monkeypatch, failure_at):
    results = [RuntimeError("synthetic recovery failure")] if failure_at == "startup" else [
        True, RuntimeError("synthetic recovery failure")]
    loop = Loop(monkeypatch, messages=130, process_seconds=1, recovery_results=results)
    loop.run()
    assert loop.processed == list(range(1, 131))
    assert loop.recoveries == ([0, 5, 125] if failure_at == "startup" else [0, 120, 125])
    assert loop.clock.sleeps == []
    failed_at = 0 if failure_at == "startup" else 120
    failure_index = loop.events.index(("session_failed", failed_at))
    assert loop.events[failure_index + 1:failure_index + 3] == [
        ("session_exit", failed_at), ("pop", failed_at)]


def test_lost_recovery_lease_uses_normal_interval_without_stopping_consumption(monkeypatch):
    loop = Loop(monkeypatch, recovery_results=[False, False, False])
    loop.run()
    assert loop.recoveries == [0, 120, 240]
    assert loop.processed == [1, 2, 3, 4, 5]
    assert loop.clock.sleeps == []


def test_processing_failure_does_not_skip_next_recovery_deadline(monkeypatch):
    loop = Loop(monkeypatch, messages=3, fail_processing=True)
    loop.run()
    assert loop.processed == [1, 2, 3]
    assert loop.recoveries == [0, 120]


@pytest.mark.parametrize("failure", ["enter", "exit"])
@pytest.mark.parametrize("kind", ["assignment", "exam", "ai_grade"])
def test_processing_session_failure_does_not_stop_consumption_or_recovery(monkeypatch, failure, kind):
    loop = Loop(monkeypatch, messages=3, session_failure=failure,
                role="ai" if kind == "ai_grade" else "judge", message_kind=kind)
    loop.run()
    assert loop.processed == ([2, 3] if failure == "enter" else [1, 2, 3])
    assert loop.recoveries == ([] if kind == "ai_grade" else [0, 120])


def test_enabled_ai_worker_keeps_its_queue_and_never_runs_shared_recovery(monkeypatch):
    loop = Loop(monkeypatch, role="ai")
    loop.run()
    assert loop.recoveries == []
    assert loop.processed == [1, 2, 3, 4, 5]
    # blpop 每消费一条就轮转队列顺序，故按集合比较消费面
    assert all(set(queues) == {"judge:ai:queue", "judge:ai-setup:queue"}
               for queues, _timeout in loop.pop_calls)


def test_disabled_ai_worker_opens_no_redis_connection(monkeypatch):
    loop = Loop(monkeypatch, role="ai", enabled=False)
    worker.run_worker_loop()
    assert loop.connections == []
    assert loop.recoveries == []


def test_consumer_socket_timeout_exceeds_the_blocking_pop_timeout(monkeypatch):
    loop = Loop(monkeypatch, messages=0)
    loop.run()
    assert loop.connections == [(loop.settings.redis_url, {
        "decode_responses": True, "encoding_errors": "replace", "socket_connect_timeout": 2, "socket_timeout": 7})]
    assert all(timeout == 5 for _queues, timeout in loop.pop_calls)


def test_recovery_push_connections_have_bounded_network_waits(monkeypatch):
    calls = []
    client = object()

    def from_url(url, **kwargs):
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(judge_queue, "get_settings", lambda: SimpleNamespace(redis_url="redis://unused.invalid/0"))
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    assert judge_queue._get_redis() is client
    assert calls == [("redis://unused.invalid/0", {
        "decode_responses": False, "socket_connect_timeout": 2, "socket_timeout": 3})]
