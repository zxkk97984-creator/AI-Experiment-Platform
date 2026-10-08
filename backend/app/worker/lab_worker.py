"""Run ``python -m app.worker.lab_worker`` on the Docker execution node.

The database is authoritative. Redis merely wakes idle polling, so a Redis
outage does not lose work. Threads allow configured concurrent jobs, each with
its own independent DB heartbeat and fenced completion.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import redis
from sqlalchemy import func, select, update

from app.config import get_settings
from app.database import SessionLocal
from app.models.lab import LabRun, LabRegradeBatch
from app.services.lab_queue import LeaseGuard, LeaseLost, RunCancelled, claim_run, finish_run, recover_runs
from app.services.lab_runtime import stop_run_containers

logger = logging.getLogger("dai.lab_worker")


def _process(session_factory, run_id, token, settings):
    from app.services.lab_processing import process_run

    try:
        with LeaseGuard(session_factory, run_id, token, settings) as guard:
            guard.assert_owned()
            process_run(session_factory, run_id, token, settings)
    except LeaseLost:
        logger.info("Run %s abandoned after losing its lease", run_id)
    except RunCancelled:
        if stop_run_containers(run_id, token):
            with session_factory() as db:
                finish_run(db, run_id, token, "cancelled")
                db.commit()
    except Exception:
        logger.exception("Run %s interrupted; durable recovery will retry", run_id)
        # Never complete or free capacity speculatively. Recovery stops the
        # exact container before releasing or handing the work to another node.
        with session_factory() as db:
            now = db.scalar(select(func.now()))
            db.execute(update(LabRun).where(LabRun.id == run_id,
                LabRun.lease_token == token, LabRun.status == "running",
            ).values(lease_until=now))
            db.commit()


def main():
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    if not settings.lab_enabled:
        logger.info("Lab worker is disabled")
        return
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
        socket_connect_timeout=2, socket_timeout=3)
    subscription = None
    futures = set()
    pool = ThreadPoolExecutor(max_workers=settings.lab_training_concurrency + 1,
        thread_name_prefix="lab-job")
    try:
        while True:
            futures = {future for future in futures if not future.done()}
            try:
                with SessionLocal() as db:
                    recover_runs(db, settings)
                with SessionLocal() as db:
                    batches = list(db.scalars(select(LabRegradeBatch.id).where(LabRegradeBatch.status == "running").order_by(LabRegradeBatch.id).limit(20)))
                for batch_id in batches:
                    from app.services.lab_grading import refresh_regrade_batch
                    with SessionLocal() as db:
                        refresh_regrade_batch(db, batch_id)
                        db.commit()
                claimed = None
                if len(futures) < settings.lab_training_concurrency + 1:
                    with SessionLocal() as db:
                        claimed = claim_run(db, settings)
                if claimed:
                    futures.add(pool.submit(_process, SessionLocal, *claimed, settings))
                    continue
                if subscription is None:
                    subscription = client.pubsub(ignore_subscribe_messages=True)
                    subscription.subscribe("lab-runs")
                subscription.get_message(timeout=min(2, settings.lab_queue_poll_seconds))
            except redis.exceptions.RedisError:
                if subscription is not None:
                    try:
                        subscription.close()
                    except redis.exceptions.RedisError:
                        pass
                subscription = None
                time.sleep(min(2, settings.lab_queue_poll_seconds))
            except Exception:
                logger.exception("Lab worker poll failed; jobs remain in database")
                time.sleep(min(5, settings.lab_queue_poll_seconds))
    except KeyboardInterrupt:
        logger.info("Lab worker is shutting down after current jobs")
    finally:
        if subscription is not None:
            subscription.close()
        client.close()
        pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
