#!/usr/bin/env python3
"""Probe a built image and optionally register a NEW immutable lab environment.

Run from repository root with backend/.venv/bin/python. Defaults to read-only
probe; --apply requires an administrator ID. Existing environment versions are
never rewritten. Production requires a registry digest reference.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

PACKAGES = {
    "torch": ["torch"],
    "ipykernel": ["ipykernel"],
    "jupyter-client": ["jupyter_client"],
    "numpy": ["numpy"],
    "pandas": ["pandas"],
    "pillow": ["PIL"],
    "scikit-learn": ["sklearn"],
    "matplotlib": ["matplotlib"],
}


def probe_image(image, device="cpu", gpu_device=None):
    inspection = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", inspection):
        raise ValueError("镜像没有有效的不可变ID")
    reference = (
        image if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) else inspection
    )
    script = (
        "import importlib.metadata as m,json,sys,pathlib,torch,numpy,pandas,PIL,sklearn,matplotlib,ipykernel,jupyter_client; "
        "assert pathlib.Path('/opt/dai/kernel_runner.py').is_file(), 'Notebook Kernel runner missing'; "
        "x=torch.ones(2,device='" + ("cuda" if device == "gpu" else "cpu") + "'); "
        "assert x.sum().item()==2; "
        "print(json.dumps({'python':sys.version.split()[0],'cuda':bool(torch.cuda.is_available()),"
        "'packages':{p:m.version(p) for p in " + repr(list(PACKAGES)) + "}}))"
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=128m",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--pids-limit",
        "128",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-e",
        "OPENBLAS_NUM_THREADS=1",
        "-e",
        "OMP_NUM_THREADS=1",
    ]
    if device == "gpu":
        if not gpu_device or not re.fullmatch(r"[A-Za-z0-9_-]+", gpu_device):
            raise ValueError("GPU验证需要--gpu-device指定一个设备")
        command += ["--gpus", "device=" + gpu_device]
    report = json.loads(
        subprocess.run(
            [*command, reference, "python", "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout
    )
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", report["packages"]["torch"])
    if not match or tuple(map(int, match.groups())) < (2, 10, 0):
        raise ValueError(
            "新实验环境拒绝已知受影响的旧PyTorch版本，请构建当前已修补版本"
        )
    return reference, report


def register(db, reference, report, device, actor_id, base_image):
    from sqlalchemy import func, select
    from app.models import (
        EnvironmentProfile,
        EnvironmentVersion,
        PackageCatalog,
        ProfileVersionPackage,
        User,
    )

    actor = db.get(User, actor_id)
    if not actor or actor.role != "admin" or actor.status != "active":
        raise ValueError("--admin-id必须属于有效管理员")
    slug = "lab-pytorch-" + device
    profile = db.scalar(
        select(EnvironmentProfile)
        .where(EnvironmentProfile.slug == slug)
        .with_for_update()
    )
    if not profile:
        profile = EnvironmentProfile(
            slug=slug,
            display_name="实验学习 · PyTorch " + device.upper(),
            description="独立验证的教学训练环境；历史版本保持不变。",
            created_by_id=actor_id,
            status="active",
        )
        db.add(profile)
        db.flush()
    existing = db.scalar(
        select(EnvironmentVersion).where(
            EnvironmentVersion.profile_id == profile.id,
            EnvironmentVersion.image_digest == reference,
        )
    )
    if existing:
        return existing
    versions = report["packages"]
    specification = {
        "lab_device": device,
        "python": report["python"],
        "packages": versions,
        "image": reference,
    }
    manifest = hashlib.sha256(
        json.dumps(specification, sort_keys=True).encode()
    ).hexdigest()
    version = EnvironmentVersion(
        profile_id=profile.id,
        version_number=(
            db.scalar(
                select(func.max(EnvironmentVersion.version_number)).where(
                    EnvironmentVersion.profile_id == profile.id
                )
            )
            or 0
        )
        + 1,
        status="available",
        build_mode="legacy",
        base_image_ref=base_image,
        image_digest=reference,
        python_version=".".join(report["python"].split(".")[:2]),
        minimum_memory_mb=4096,
        manifest_sha256=manifest,
        resolved_packages=versions,
        requested_spec={
            "schema_version": 1,
            "python_packages": [],
            "system_packages": [],
            "lab_device": device,
        },
        created_by_id=actor_id,
        available_at=datetime.now(timezone.utc),
        first_published_at=datetime.now(timezone.utc),
        first_published_by_id=actor_id,
    )
    db.add(version)
    db.flush()
    for order, (name, imports) in enumerate(PACKAGES.items()):
        source = "pytorch_cpu" if name == "torch" and device == "cpu" else "pypi"
        package = db.scalar(
            select(PackageCatalog).where(
                PackageCatalog.normalized_name == name,
                PackageCatalog.locked_version == versions[name],
                PackageCatalog.source_key == source,
            )
        )
        if not package:
            package = PackageCatalog(
                normalized_name=name,
                pip_name=name,
                locked_version=versions[name],
                import_names=imports,
                category_tags=["machine-learning"],
                source_key=source,
                status="active",
            )
            db.add(package)
            db.flush()
        db.add(
            ProfileVersionPackage(
                environment_version_id=version.id,
                package_catalog_id=package.id,
                display_order=order,
            )
        )
    profile.current_version_id = version.id
    db.flush()
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--device", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--gpu-device")
    parser.add_argument("--admin-id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--base-image",
        default="python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36",
    )
    args = parser.parse_args()
    reference, report = probe_image(args.image, args.device, args.gpu_device)
    print(
        json.dumps(
            {"image": reference, "device": args.device, **report},
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.apply:
        from app.config import Settings
        from app.database import create_engine_from_url, sessionmaker_for_engine

        settings = Settings(
            _env_file=str(Path(__file__).resolve().parents[1] / "backend" / ".env")
        )
        if settings.environment == "production" and "@sha256:" not in reference:
            raise ValueError(
                "生产环境请先推送学校Registry并使用repository@sha256:digest注册"
            )
        if not args.admin_id:
            parser.error("--apply需要--admin-id")
        engine = create_engine_from_url(settings.database_url)
        with sessionmaker_for_engine(engine)() as db:
            version = register(
                db, reference, report, args.device, args.admin_id, args.base_image
            )
            db.commit()
            print("Registered environment version:", version.id)
    else:
        print(
            "Probe passed. No database changes; use --apply --admin-id ID to register."
        )


if __name__ == "__main__":
    main()
