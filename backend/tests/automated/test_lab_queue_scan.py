from datetime import datetime

from app.models.lab import LabRun
from app.services import lab_queue
from app.services.lab_resources import reserve_resources, release_resources
from test_lab_queue import lab_owner  # noqa: F401


def test_worker_cursor_reaches_cpu_job_after_hundred_blocked_gpu_jobs(db_session_factory, test_settings, lab_owner):  # noqa: F811
    settings = test_settings.model_copy(update={'lab_gpu_enabled': True, 'lab_gpu_devices': '0', 'lab_training_concurrency': 4})
    scan = lab_queue.ClaimScanState()
    with db_session_factory() as db:
        reserve_resources(db, key='busy-gpu', token='owner', kind='job', cpus=1,
                          memory_mb=1024, device='gpu', settings=settings)
        stamp = datetime(2026, 10, 9)
        jobs = [LabRun(owner_id=lab_owner, kind='train', device='gpu' if i < 100 else 'cpu',
                       resources={'cpus': 1, 'memory_mb': 1024}, created_at=stamp) for i in range(101)]
        db.add_all(jobs)
        db.commit()
        ids = [job.id for job in jobs]
        assert lab_queue.claim_run(db, settings, scan) is None
        assert scan.after == (stamp, ids[99])
        # Continuous arrivals must not extend this pass and postpone older work forever.
        db.add_all([LabRun(owner_id=lab_owner, kind='train', device='cpu',
                           resources={'cpus': 1, 'memory_mb': 1024}, created_at=stamp) for _ in range(30)])
        db.commit()
        claimed = lab_queue.claim_run(db, settings, scan)
        assert claimed[0] == ids[100]
        release_resources(db, 'busy-gpu', 'owner')
        db.commit()
        assert lab_queue.claim_run(db, settings, scan)[0] == ids[0]


def test_empty_or_removed_tail_wraps_without_forgetting_old_jobs(db_session_factory, test_settings, lab_owner):  # noqa: F811
    scan = lab_queue.ClaimScanState()
    with db_session_factory() as db:
        assert lab_queue.claim_run(db, test_settings, scan) is None
        job = LabRun(owner_id=lab_owner, kind='train', resources={'cpus': 1, 'memory_mb': 1024})
        db.add(job)
        db.commit()
        assert lab_queue.claim_run(db, test_settings, scan)[0] == job.id
        assert lab_queue.claim_run(db, test_settings, scan) is None
        assert scan.after is None and scan.high_water is None
