from datetime import timedelta
import uuid

import pytest
from sqlalchemy import func, select

from app.models import User
from app.models.lab import LabResourceLease, LabRun
from app.services.lab_queue import claim_run, finish_run, heartbeat_run, recover_runs
from app.services.lab_resources import release_resources, reserve_resources


@pytest.fixture()
def lab_owner(db_session_factory):
    with db_session_factory() as db:
        owner = User(username="lab-runtime-student", real_name="Lab Student", role="student", password_hash="test")
        db.add(owner)
        db.commit()
        return owner.id


def add_run(db, owner_id, **kwargs):
    row = LabRun(owner_id=owner_id, kind=kwargs.pop("kind", "train"),
        resources={"cpus": 1, "memory_mb": 1024}, **kwargs)
    db.add(row)
    db.commit()
    return row


def test_claim_reserves_capacity_and_fences_stale_completion(db_session_factory, test_settings, lab_owner):
    with db_session_factory() as db:
        first = add_run(db, lab_owner)
        second = add_run(db, lab_owner)
        run_id, token = claim_run(db, test_settings)
        assert run_id == first.id
        assert claim_run(db, test_settings) is None
        assert not finish_run(db, run_id, str(uuid.uuid4()), "succeeded")
        assert db.get(LabResourceLease, f"run:{run_id}") is not None
        assert finish_run(db, run_id, token, "succeeded", result={"ok": True})
        db.commit()
        assert claim_run(db, test_settings)[0] == second.id


def test_validation_has_separate_single_slot(db_session_factory, test_settings, lab_owner):
    with db_session_factory() as db:
        one = add_run(db, lab_owner, kind="dataset_validate")
        add_run(db, lab_owner, kind="artifact_validate")
        training = add_run(db, lab_owner)
        assert claim_run(db, test_settings)[0] == one.id
        assert claim_run(db, test_settings)[0] == training.id
        assert claim_run(db, test_settings) is None


def test_cancel_after_concurrent_claim_retains_cleanup_lease(db_session_factory, test_settings, lab_owner):
    from app.services.lab_queue import request_cancel
    with db_session_factory() as stale:
        run = add_run(stale, lab_owner, active_student_id=lab_owner)
        assert run.status == "queued"
        with db_session_factory() as worker:
            run_id, token = claim_run(worker, test_settings)
        request_cancel(stale, run.id)
        stale.commit()
    with db_session_factory() as db:
        current = db.get(LabRun, run_id)
        assert current.status == "running" and current.cancel_requested
        assert current.active_student_id == lab_owner
        assert db.get(LabResourceLease, f"run:{run_id}") is not None
        assert finish_run(db, run_id, token, "cancelled")
        db.commit()
        assert db.get(LabResourceLease, f"run:{run_id}") is None


def test_large_unicode_training_logs_are_stored_within_byte_limit(db_session_factory, test_settings, lab_owner):
    from app.services.lab_queue import append_logs
    with db_session_factory() as db:
        run = add_run(db, lab_owner)
        run_id, token = claim_run(db, test_settings)
        append_logs(db, run_id, token, "训练进度\n" * 100000, test_settings)
        db.refresh(run)
        assert 65535 < len(run.logs.encode("utf-8")) <= test_settings.lab_log_max_bytes
        append_logs(db, run_id, token, "完成", test_settings)
        db.refresh(run)
        assert run.logs.endswith("完成")
        assert len(run.logs.encode("utf-8")) <= test_settings.lab_log_max_bytes


def test_expired_resources_not_freed_until_container_stopped(db_session_factory, test_settings, lab_owner, monkeypatch):
    with db_session_factory() as db:
        run = add_run(db, lab_owner, active_student_id=lab_owner)
        run_id, token = claim_run(db, test_settings)
        run.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()
        monkeypatch.setattr("app.services.lab_runtime.stop_run_containers", lambda *_: False)
        assert recover_runs(db, test_settings) == 0
        assert db.get(LabResourceLease, f"run:{run_id}") is not None
        assert not finish_run(db, run_id, token, "succeeded")
        monkeypatch.setattr("app.services.lab_runtime.stop_run_containers", lambda *_: True)
        assert recover_runs(db, test_settings) == 1
        db.expire_all()
        assert db.get(LabRun, run_id).status == "queued"
        assert db.get(LabResourceLease, f"run:{run_id}") is None
        claimed = claim_run(db, test_settings)
        assert claimed[0] == run_id and claimed[1] != token


def test_expired_lease_cannot_heartbeat_or_finish(db_session_factory, test_settings, lab_owner):
    with db_session_factory() as db:
        run = add_run(db, lab_owner)
        run_id, token = claim_run(db, test_settings)
        run.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()
        assert not heartbeat_run(db, run_id, token, test_settings)
        assert not finish_run(db, run_id, token, "succeeded")


def test_cancelled_job_cannot_publish_success(db_session_factory, test_settings, lab_owner):
    with db_session_factory() as db:
        run = add_run(db, lab_owner, active_student_id=lab_owner)
        run_id, token = claim_run(db, test_settings)
        run.cancel_requested = True
        db.commit()
        assert not finish_run(db, run_id, token, "succeeded")
        assert finish_run(db, run_id, token, "cancelled")
        db.commit()
        assert db.get(LabRun, run_id).active_student_id is None


def test_gpu_exclusive_and_reserved_interactive_memory(db_session_factory, test_settings):
    settings = test_settings.model_copy(update={"lab_gpu_enabled": True, "lab_gpu_devices": "0", "lab_training_concurrency": 4})
    with db_session_factory() as db:
        first = reserve_resources(db, key="run:a", token="one", kind="job", cpus=1,
            memory_mb=1024, device="gpu", settings=settings)
        assert first.gpu == "0"
        db.commit()
        assert reserve_resources(db, key="run:b", token="two", kind="job", cpus=1,
            memory_mb=1024, device="gpu", settings=settings) is None
        assert not release_resources(db, "run:a", "wrong")
        with pytest.raises(ValueError, match="capacity"):
            reserve_resources(db, key="run:c", token="three", kind="job", cpus=1,
                memory_mb=7000, settings=settings)


def test_recovery_has_bounded_attempts(db_session_factory, test_settings, lab_owner, monkeypatch):
    monkeypatch.setattr("app.services.lab_runtime.stop_run_containers", lambda *_: True)
    with db_session_factory() as db:
        run = add_run(db, lab_owner, active_student_id=lab_owner)
        run_id, token = claim_run(db, test_settings)
        run.attempts = test_settings.lab_max_attempts
        run.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()
        assert recover_runs(db, test_settings) == 1
        db.expire_all()
        run = db.get(LabRun, run_id)
        assert run.status == "failed" and run.error_code == "WORKER_LOST"
        assert run.active_student_id is None
        assert claim_run(db, test_settings) is None


def test_parallel_claims_cannot_exceed_global_capacity(db_session_factory, test_settings, lab_owner):
    from concurrent.futures import ThreadPoolExecutor
    settings = test_settings.model_copy(update={"lab_training_concurrency": 2})
    with db_session_factory() as db:
        for _ in range(8):
            add_run(db, lab_owner)
    def claim(_):
        with db_session_factory() as db:
            return claim_run(db, settings)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [result for result in pool.map(claim, range(8)) if result]
    assert len(claims) == 2
    assert len({ident for ident, _ in claims}) == 2
    with db_session_factory() as db:
        leases = list(db.scalars(select(LabResourceLease).where(LabResourceLease.kind == "job")))
        assert len(leases) == 2
        assert sum(lease.memory_mb for lease in leases) == 2048
