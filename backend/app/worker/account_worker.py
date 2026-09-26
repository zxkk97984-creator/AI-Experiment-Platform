"""Run with python -m app.worker.account_worker. No Docker access is required."""
import logging
import time

import redis

from app.config import get_settings
from app.database import SessionLocal
from app.services.account_batch_service import claim_batch, cleanup_batches, process_batch

logger = logging.getLogger('dai.account_worker')


def wait_for_wakeup(client, subscription):
    try:
        if subscription is None:
            subscription = client.pubsub(ignore_subscribe_messages=True)
            subscription.subscribe('account-batches')
        subscription.get_message(timeout=5)
        return subscription
    except redis.exceptions.RedisError:
        if subscription is not None:
            try:
                subscription.close()
            except redis.exceptions.RedisError:
                pass
        time.sleep(5)
        return None  # Next iteration always polls the durable database first.


def main():
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=6)
    subscription = None
    last_cleanup = 0.0
    while True:
        try:
            with SessionLocal() as db:
                claim = claim_batch(db)
            if claim:
                process_batch(SessionLocal, *claim, settings)
            else:
                subscription = wait_for_wakeup(client, subscription)
            if time.monotonic() - last_cleanup > 3600:
                with SessionLocal() as db:
                    cleanup_batches(db, settings)
                last_cleanup = time.monotonic()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error('Account worker iteration failed (%s); durable lease will recover', type(exc).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()
