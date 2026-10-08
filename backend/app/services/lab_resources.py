"""Transactional, single-host resource admission shared by kernels and lab jobs.

An expired reservation is *not* free capacity. Its owner must first prove that
the associated container has stopped; otherwise a crashed worker could cause
oversubscription or hand a live GPU to another student.
"""
from __future__ import annotations

import math
import re
import hashlib
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.lab import LabResourceLease

_LOCK_KEY = "__lab_resource_lock__"


def resource_key(key, settings):
    if settings.lab_worker_mode == 'benchmark':
        identity = hashlib.sha256(settings.lab_benchmark_node_id.encode()).hexdigest()[:24]
        return f'benchmark:{identity}:{key}'
    return key


@dataclass(frozen=True)
class ResourceReservation:
    key: str
    token: str
    gpu: str | None


def lock_resources(db):
    """Take a transaction-scoped DB write lock (also works on SQLite)."""
    now = db.scalar(select(func.now()))
    if db.get_bind().dialect.name == "mysql":
        # An UPDATE against a missing sentinel takes an InnoDB gap lock.
        # Concurrent first users then deadlock when both try to INSERT it.
        # A single upsert creates or locks the sentinel without that gap.
        from sqlalchemy.dialects.mysql import insert
        statement = insert(LabResourceLease).values(
            key=_LOCK_KEY, token="lock", kind="lock", cpus=0, memory_mb=0, expires_at=now)
        db.execute(statement.on_duplicate_key_update(expires_at=now))
        return now
    changed = db.execute(update(LabResourceLease).where(
        LabResourceLease.key == _LOCK_KEY,
    ).values(expires_at=now)).rowcount
    if not changed:
        try:
            with db.begin_nested():
                db.add(LabResourceLease(key=_LOCK_KEY, token="lock", kind="lock",
                    cpus=0, memory_mb=0, expires_at=now))
                db.flush()
        except IntegrityError:
            db.execute(update(LabResourceLease).where(
                LabResourceLease.key == _LOCK_KEY,
            ).values(expires_at=now))
    return now


def reserve_resources(db, *, key, token, kind, cpus, memory_mb, settings, device="cpu"):
    """Return a reservation, or None when capacity is occupied; caller commits.

    Invalid or impossible requests raise ValueError rather than queue forever.
    GPU IDs are administrator configured and exclusive for the entire lease.
    """
    if kind == 'kernel' and settings.lab_worker_mode == 'benchmark':
        raise ValueError('专用基准节点不提供交互内核')
    cpus = float(cpus)
    memory_mb = int(memory_mb)
    if not math.isfinite(cpus) or cpus <= 0 or memory_mb < 128:
        raise ValueError("Invalid lab CPU or memory request")
    if device not in {"cpu", "gpu"}:
        raise ValueError("Unsupported device")
    total_cpu = float(settings.lab_total_cpus)
    total_memory = int(settings.lab_total_memory_mb)
    background_memory = total_memory - int(settings.lab_interactive_reserved_memory_mb)
    if cpus > total_cpu or memory_mb > (total_memory if kind == "kernel" else background_memory):
        raise ValueError("Requested resources exceed the configured capacity")
    devices = [x.strip() for x in settings.lab_gpu_devices.split(",") if x.strip()]
    if device == "gpu" and (not settings.lab_gpu_enabled or not devices):
        raise ValueError("GPU execution is disabled or no GPU is configured")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", x) for x in devices):
        raise ValueError("Invalid administrator GPU device identifier")
    if device == "gpu" and kind == "kernel":
        raise ValueError("Interactive GPU kernels are not supported")
    now = lock_resources(db)
    key = resource_key(key, settings)
    existing = db.get(LabResourceLease, key)
    if existing:
        if existing.token != token:
            return None
        if (existing.cpus != cpus or existing.memory_mb != memory_mb or existing.kind != kind
                or (existing.gpu is not None) != (device == "gpu")):
            raise ValueError("An existing reservation cannot change its resource budget")
        return ResourceReservation(key, token, existing.gpu)
    leases = list(db.scalars(select(LabResourceLease).where(LabResourceLease.key != _LOCK_KEY)))
    if settings.lab_worker_mode == 'benchmark':
        prefix = resource_key('', settings)
        leases = [row for row in leases if row.key.startswith(prefix)]
    else:
        leases = [row for row in leases if not row.key.startswith('benchmark:')]
    if sum(x.cpus for x in leases) + cpus > total_cpu + 1e-9:
        return None
    if sum(x.memory_mb for x in leases) + memory_mb > total_memory:
        return None
    background = [x for x in leases if x.kind not in {"kernel", "validation"}]
    # Efficiency comparisons are serialized and do not compete with training
    # jobs. Interactive kernels retain their separately reserved quota.
    if kind == 'benchmark' and background:
        return None
    if kind not in {'kernel', 'validation'} and any(x.kind == 'benchmark' for x in background):
        return None
    if kind != "kernel" and sum(x.memory_mb for x in background) + memory_mb > background_memory:
        return None
    if kind == "kernel" and sum(x.kind == "kernel" for x in leases) >= settings.lab_max_kernels:
        return None
    if kind != "kernel" and len(background) >= settings.lab_training_concurrency:
        return None
    gpu = None
    if device == "gpu":
        busy = {x.gpu for x in leases if x.gpu is not None}
        gpu = next((x for x in devices if x not in busy), None)
        if gpu is None:
            return None
    db.add(LabResourceLease(key=key, token=token, kind=kind, cpus=cpus,
        memory_mb=memory_mb, gpu=gpu,
        expires_at=now + timedelta(seconds=settings.lab_lease_seconds)))
    db.flush()
    return ResourceReservation(key, token, gpu)


def touch_resources(db, key, token, settings):
    now = db.scalar(select(func.now()))
    key = resource_key(key, settings)
    return db.execute(update(LabResourceLease).where(
        LabResourceLease.key == key, LabResourceLease.token == token,
    ).values(expires_at=now + timedelta(seconds=settings.lab_lease_seconds))).rowcount == 1


def release_resources(db, key, token):
    """Release only after stopping the corresponding container, in caller's tx."""
    return db.execute(delete(LabResourceLease).where(
        ((LabResourceLease.key == key) | ((LabResourceLease.key.like('benchmark:%')) & LabResourceLease.key.endswith(':' + key))),
        LabResourceLease.token == token,
    )).rowcount == 1
