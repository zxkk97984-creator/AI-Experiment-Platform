"""Wakeups are advisory; a disconnected Redis must permit database polling."""
import fakeredis
import redis

from app.worker import account_worker


def test_redis_wakeup_can_be_consumed_and_subscription_reused():
    client = fakeredis.FakeRedis(decode_responses=True)
    subscription = account_worker.wait_for_wakeup(client, None)
    client.publish('account-batches', 'test-batch-id')
    assert account_worker.wait_for_wakeup(client, subscription) is subscription
    subscription.close()
    client.close()


def test_redis_outage_returns_to_database_polling(monkeypatch):
    class OfflineRedis:
        def pubsub(self, **kwargs):
            raise redis.exceptions.ConnectionError('test-only outage')
    pauses = []
    monkeypatch.setattr(account_worker.time, 'sleep', pauses.append)
    assert account_worker.wait_for_wakeup(OfflineRedis(), None) is None
    assert pauses == [5]  # Bounded fallback; no tight retry loop or unbounded wait.
