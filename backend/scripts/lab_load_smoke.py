"""Isolated 100–200 learner control-plane smoke, without running model training.

Usage: .venv/bin/python scripts/lab_load_smoke.py --clients 200 --output /tmp/lab-load.json

Uses a fresh temporary SQLite DB and fake Redis. ASGI sync handlers execute
inline, matching the repository test adapter; reported times are functional
smoke measurements, NOT production HTTP/DB throughput or GPU capacity claims.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fakeredis
import httpx
from anyio._backends._asyncio import AsyncIOBackend
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.dependencies import get_db, get_redis_client
from app.main import create_app
from app.models import (EnvironmentProfile, EnvironmentVersion, ExperimentModule,
    ExperimentSubmission, LabAssessment, LabRun, NotebookTemplate, NotebookTemplateVersion, User)
from app.schemas.lab import LabAssessmentConfig
from app.security import create_token
from app.services.lab_queue import claim_run


@classmethod
async def _inline_sync(cls, func, args, abandon_on_cancel=False, limiter=None):
    await cls.checkpoint()
    return func(*args)


async def exercise(clients):
    AsyncIOBackend.run_sync_in_worker_thread = _inline_sync
    with tempfile.TemporaryDirectory(prefix="dai-lab-load-") as temporary:
        root = Path(temporary)
        settings = Settings(_env_file=None, environment="development",
            database_url=f"sqlite:///{root / 'test.sqlite'}", secret_key="isolated-lab-smoke-only-not-production",
            studio_storage_dir=str(root / "studio"), judge_work_dir=str(root / "work"),
            judge_host_work_dir="", lab_enabled=True, ai_enabled=False)
        engine = create_engine_from_url(settings.database_url)
        Base.metadata.create_all(engine)
        factory = sessionmaker_for_engine(engine)
        with factory() as db:
            teacher = User(username="smoke-teacher", real_name="Smoke teacher", role="teacher", password_hash="unused")
            db.add(teacher)
            db.flush()
            profile = EnvironmentProfile(slug="basic", display_name="Smoke", status="active")
            db.add(profile)
            db.flush()
            environment = EnvironmentVersion(profile_id=profile.id, version_number=1, status="available",
                base_image_ref="python:3.12", image_digest="sha256:" + "1" * 64,
                python_version="3.12", minimum_memory_mb=256, manifest_sha256="a" * 64)
            db.add(environment)
            db.flush()
            cells = [{"id": "code", "type": "code", "source": "print(1)", "student_editable": True}]
            template = NotebookTemplate(name="Load lab", owner_id=teacher.id, status="published",
                draft_cells=cells, draft_environment_version_id=environment.id)
            db.add(template)
            db.flush()
            version = NotebookTemplateVersion(template_id=template.id, version_number=1,
                environment_version_id=environment.id, cells=cells, sha256="b" * 64, published_by_id=teacher.id)
            db.add(version)
            db.flush()
            template.current_version_id = version.id
            module = ExperimentModule(name="Load lab", owner_id=teacher.id, status="published", template_id=template.id)
            db.add(module)
            db.flush()
            config = LabAssessmentConfig(criteria=[{"id": "method", "name": "Method", "description": "Check implementation",
                "source": "check", "max_score": 100}], check_script="# synthetic; worker is not invoked", confirmed=True).model_dump()
            db.add(LabAssessment(owner_id=teacher.id, entry_key=f"module:{module.id}", version=1,
                template_version_id=version.id, config=config, config_hash="c" * 64, status="published"))
            students = [User(username=f"smoke-{i}", real_name=f"Student {i}", role="student", password_hash="unused") for i in range(clients)]
            db.add_all(students)
            db.commit()
            module_id = module.id
            tokens = [create_token(subject=u.id, role="student", token_type="access", secret_key=settings.secret_key,
                algorithm=settings.algorithm, expires_delta=timedelta(hours=1), session_version=u.session_version) for u in students]
        app = create_app(settings)
        fake = fakeredis.FakeRedis(decode_responses=True)
        def database():
            with factory() as db:
                yield db
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_redis_client] = lambda: fake
        timings = []
        checks = 0
        # Inline sync dispatch cannot wait for SQLAlchemy's pooled connections
        # while other ASGI tasks close theirs. Bound in-flight requests below
        # that pool; 200 independent authenticated clients remain active.
        in_flight = asyncio.Semaphore(8)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smoke") as client:
            async def request(index, method, path, *, expected=200, **kwargs):
                nonlocal checks
                async with in_flight:
                    start = time.monotonic()
                    response = await client.request(method, "/api/v1" + path,
                        headers={"Authorization": f"Bearer {tokens[index]}"}, **kwargs)
                    timings.append((time.monotonic() - start) * 1000)
                if response.status_code != expected:
                    raise AssertionError(f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:300]}")
                checks += 1
                return response.json()
            started = time.monotonic()
            records = await asyncio.gather(*(request(i, "POST", f"/experiments/records/ensure-for-module/{module_id}") for i in range(clients)))
            async def student_flow(index):
                row = records[index]
                saved = await request(index, "PUT", f"/experiments/records/{row['id']}/cells",
                    json={"cells": {"code": f"print({index})"}, "record_revision": row["record_revision"]})
                key = str(uuid4())
                payload = {"client_request_id": key, "expected_record_revision": saved["record_revision"]}
                queued = await request(index, "POST", f"/labs/records/{row['id']}/train", expected=202, json=payload)
                repeated = await request(index, "POST", f"/labs/records/{row['id']}/train", expected=202, json=payload)
                assert queued["id"] == repeated["id"]
                await request(index, "GET", f"/labs/records/{row['id']}/overview")
                neighbor = records[(index + 1) % clients]
                await request(index, "GET", f"/labs/records/{neighbor['id']}/overview", expected=403)
                await request(index, "POST", f"/experiments/records/{row['id']}/submit", expected=201,
                    json={"client_request_id": str(uuid4()), "expected_record_revision": saved["record_revision"]})
            await asyncio.gather(*(student_flow(i) for i in range(clients)))
            duration = time.monotonic() - started
        with factory() as db:
            submission_count = db.scalar(select(func.count()).select_from(ExperimentSubmission))
            total_jobs = db.scalar(select(func.count()).select_from(LabRun))
            assert submission_count == clients and total_jobs == clients * 2
            assert claim_run(db, settings) is not None
            assert claim_run(db, settings) is None
            running = db.scalar(select(func.count()).select_from(LabRun).where(LabRun.status == "running"))
            assert running == 1
        engine.dispose()
        return {"clients": clients, "max_in_flight_requests": 8, "requests_checked": checks, "submissions": submission_count,
            "jobs": total_jobs, "simultaneously_admitted_jobs": running, "elapsed_seconds": round(duration, 3),
            "median_request_ms": round(statistics.median(timings), 3),
            "p95_request_ms": round(sorted(timings)[int(len(timings) * .95) - 1], 3),
            "isolation_denials": clients, "duplicate_training_requests_deduplicated": clients,
            "environment": "temporary SQLite + fake Redis + deterministic ASGI inline sync handlers",
            "limitations": "Functional concurrency simulation only; no network server, production MySQL, real training throughput or GPU capacity measured."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=200, choices=range(2, 201))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(exercise(args.clients))
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
