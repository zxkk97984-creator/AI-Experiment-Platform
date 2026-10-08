"""Kernel admission and public teaching materials, with real DB/storage."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import subprocess
from types import SimpleNamespace

import boto3
from fakeredis import FakeStrictRedis
from moto import mock_aws
import pytest
from sqlalchemy import select

from app.models import (EnvironmentVersion, ExperimentModule, ExperimentRecord, LabAssessment,
                        LabRecord, LabResourceLease, NotebookTemplate, NotebookTemplateVersion,
                        StudioAssetManifest, StudioAssetManifestEntry)
from app.services.kernel_manager import KernelManager
from app.services.lab_data_service import register_dataset_upload, validate_dataset
from app.services.lab_workspace import ensure_preview_kernel, ensure_record_kernel, prepare_course_workspace
from app.services.storage_object_binding_service import register_active_object
from app.storage import StorageArea, StorageService, S3CompatibleStorage
from conftest import auth_header, create_user, login
from teaching_test_support import grant_experiment


@pytest.fixture
def kernel_settings(test_settings, tmp_path):
    return test_settings.model_copy(update={"judge_work_dir": str(tmp_path / "judge-work"),
        "judge_host_work_dir": "/host/dai/judge-work", "lab_min_free_disk_mb": 64})


@pytest.fixture
def docker(monkeypatch):
    state = SimpleNamespace(containers={}, calls=[], fail_remove=False, fail_run=False)
    def run(cmd, **kwargs):
        state.calls.append(cmd)
        stdout, stderr, code = "", "", 0
        if cmd[1] == "rm":
            if state.fail_remove:
                return subprocess.CompletedProcess(cmd, 1, "", "daemon unavailable")
            state.containers.pop(cmd[-1], None)
        elif cmd[1] == "run":
            if state.fail_run:
                return subprocess.CompletedProcess(cmd, 1, "", "image unavailable")
            name = cmd[cmd.index("--name") + 1]
            state.containers[name] = cmd
            stdout = name
        elif cmd[1] == "ps":
            filters = [p for p in cmd if p.startswith("name=")]
            if filters and filters[0][5:] in state.containers:
                stdout = filters[0][5:]
        elif cmd[1] == "exec":
            stdout = '{"outputs": []}'
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)
    monkeypatch.setattr("app.services.kernel_manager.subprocess.run", run)
    monkeypatch.setattr("app.services.kernel_manager.time.sleep", lambda _: None)
    redis = FakeStrictRedis()
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: redis)
    state.redis = redis
    return state


def _template(db, owner_id):
    env = db.scalar(select(EnvironmentVersion))
    template = NotebookTemplate(name="教材", owner_id=owner_id, status="published",
        draft_cells=[{"id": "code", "type": "code", "source": "print(1)", "order": 0}],
        draft_environment_version_id=env.id, draft_revision=1)
    db.add(template)
    db.flush()
    version = NotebookTemplateVersion(template_id=template.id, version_number=1, sha256="a" * 64,
        cells=template.draft_cells, cell_order=["code"], published_by_id=owner_id, environment_version_id=env.id)
    db.add(version)
    db.flush()
    return template, version


def _asset(db, settings, owner_id, version, *, legacy=False):
    storage = StorageService.from_settings(settings)
    key = f"templates/{version.template_id}/versions/{version.version_number}/assets/lesson.csv"
    storage.put(StorageArea.STUDIO, key, BytesIO(b"feature,label\n1,a\n"))
    if legacy:
        version.assets_dir = key.rsplit("/assets/", 1)[0]
        return
    obj = register_active_object(db, storage, area=StorageArea.STUDIO, namespace="studio-assets",
        object_key=key, original_filename="lesson.csv", content_type="text/csv", created_by_id=owner_id,
        size_bytes=len(b"feature,label\n1,a\n"))
    manifest = StudioAssetManifest(version_id=version.id)
    db.add(manifest)
    db.flush()
    db.add(StudioAssetManifestEntry(manifest_id=manifest.id, storage_object_id=obj.id, relative_path="assets/lesson.csv"))
    db.flush()


def _dataset(db, settings, owner_id):
    row = register_dataset_upload(db, owner_id, "data", "tabular_classification", {}, "data.csv",
        BytesIO(("sample_id,value,label\n" + "".join(f"s{i},{i},{i % 2}\n" for i in range(20))).encode()), settings)
    validate_dataset(db, row.id, settings)
    assert row.status == "ready", row.error
    return row


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("backend", ["local", "s3"])
def test_course_cache_materializes_versioned_assets_and_only_public_dataset(db_session_factory, kernel_settings, monkeypatch, legacy, backend):
    owner = create_user(db_session_factory, "workspace-owner", "teacher")
    with mock_aws():
        if backend == "s3":
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="course-workspace")
            storage = StorageService({area: S3CompatibleStorage(client, bucket="course-workspace") for area in StorageArea}, backend_name="s3")
            monkeypatch.setattr(StorageService, "from_settings", classmethod(lambda cls, _: storage))
        with db_session_factory() as db:
            _, version = _template(db, owner.id)
            _asset(db, kernel_settings, owner.id, version, legacy=legacy)
            dataset = _dataset(db, kernel_settings, owner.id)
            db.commit()
            path = prepare_course_workspace(db, kernel_settings, version=version, dataset_id=dataset.id)
            assert (path / "assets/lesson.csv").read_bytes() == b"feature,label\n1,a\n"
            for split in ("train", "validation"):
                assert (path / split / "samples.csv").is_file()
                assert (path / split / "labels.csv").is_file()
            assert not (path / "test").exists()
            assert path == prepare_course_workspace(db, kernel_settings, version=version, dataset_id=dataset.id)
            assert not list(path.parent.glob(".pending-*"))
            assert (path.stat().st_mode & 0o005) == 0o005


def test_workspace_refuses_low_disk(db_session_factory, kernel_settings, monkeypatch):
    owner = create_user(db_session_factory, "disk-owner", "teacher")
    with db_session_factory() as db:
        _, version = _template(db, owner.id)
        db.commit()
        monkeypatch.setattr("app.services.lab_workspace.shutil.disk_usage", lambda _: SimpleNamespace(free=1))
        with pytest.raises(RuntimeError, match="磁盘"):
            prepare_course_workspace(db, kernel_settings, version=version)


def test_kernel_memory_budget_host_mount_and_release(db_session_factory, kernel_settings, docker):
    owner = create_user(db_session_factory, "kernel-owner", "teacher")
    manager = KernelManager(kernel_settings)
    with db_session_factory() as db:
        template, version = _template(db, owner.id)
        env = db.get(EnvironmentVersion, version.environment_version_id)
        env.minimum_memory_mb = 768
        module = ExperimentModule(name="实验", owner_id=owner.id, status="published", template_id=template.id)
        db.add(module)
        db.flush()
        record = ExperimentRecord(module_id=module.id, student_id=owner.id, template_version_id=version.id,
            environment_version_id=env.id)
        db.add(record)
        db.flush()
        assessment = LabAssessment(entry_key=f"module:{module.id}", owner_id=owner.id, status="published", version=1,
            config={"resources": {"memory_mb": 1024, "cpus": 1.5}}, template_version_id=version.id)
        db.add(assessment)
        db.flush()
        db.add(LabRecord(record_id=record.id, assessment_id=assessment.id))
        db.commit()
        session = ensure_record_kernel(manager, record, version, db, image_ref=env.image_digest, environment_version_id=env.id)
        command = docker.containers[session.container_name]
        assert command[command.index("--memory") + 1] == "1024m"
        assert command[command.index("--cpus") + 1] == "1.5"
        assert any(value.startswith("/host/dai/judge-work/labs/course-cache/") and value.endswith(":/course:ro") for value in command)
        with db_session_factory() as resources:
            lease = resources.get(LabResourceLease, f"kernel:{record.id}")
            assert lease.memory_mb == 1024 and lease.kind == "kernel"
            token = lease.token
        assert ensure_record_kernel(manager, record, version, db, image_ref=env.image_digest, environment_version_id=env.id) is session
        assert len([cmd for cmd in docker.calls if cmd[1] == "run"]) == 1
        manager.destroy(record.id)
        with db_session_factory() as resources:
            assert resources.get(LabResourceLease, f"kernel:{record.id}") is None
        assert not docker.containers


def test_preview_uses_profile_minimum_and_same_budget(db_session_factory, kernel_settings, docker):
    owner = create_user(db_session_factory, "preview-owner", "teacher")
    manager = KernelManager(kernel_settings)
    with db_session_factory() as db:
        template, version = _template(db, owner.id)
        env = db.get(EnvironmentVersion, version.environment_version_id)
        env.minimum_memory_mb = 1536
        db.commit()
        session = ensure_preview_kernel(manager, template, -20, db, image_ref=env.image_digest, environment_version_id=env.id)
        assert session.memory_mb == 1536
        with db_session_factory() as resources:
            assert resources.get(LabResourceLease, "kernel:-20").kind == "kernel"
        session.last_active_at = 0
        manager.cleanup_idle(1)
        with db_session_factory() as resources:
            assert resources.get(LabResourceLease, "kernel:-20") is None


def test_failed_stop_retains_lease_and_successful_retry_releases(db_session_factory, kernel_settings, docker):
    manager = KernelManager(kernel_settings)
    manager.create_session(42, memory_mb=512, resource_factory=db_session_factory)
    docker.fail_remove = True
    with pytest.raises(RuntimeError, match="暂不释放"):
        manager.destroy(42)
    with db_session_factory() as db:
        assert db.get(LabResourceLease, "kernel:42") is not None
    assert 42 in manager._sessions
    docker.fail_remove = False
    manager.destroy(42)
    with db_session_factory() as db:
        assert db.get(LabResourceLease, "kernel:42") is None


def test_kernel_launch_failure_releases_only_after_stop(db_session_factory, kernel_settings, docker):
    manager = KernelManager(kernel_settings)
    docker.fail_run = True
    with pytest.raises(RuntimeError, match="启动失败"):
        manager.create_session(43, memory_mb=512, resource_factory=db_session_factory)
    with db_session_factory() as db:
        assert db.get(LabResourceLease, "kernel:43") is None


def test_redis_recovery_touches_lease_and_restart_preserves_resource_profile(db_session_factory, kernel_settings, docker):
    manager = KernelManager(kernel_settings)
    old = manager.create_session(44, memory_mb=1024, cpus=1.5, resource_factory=db_session_factory)
    with db_session_factory() as db:
        lease = db.get(LabResourceLease, "kernel:44")
        lease.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    recovered = KernelManager(kernel_settings)
    session = recovered.get_or_create_session(44, memory_mb=1024, cpus=1.5, resource_factory=db_session_factory)
    assert session.resource_token == old.resource_token
    with db_session_factory() as db:
        assert db.get(LabResourceLease, "kernel:44").expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    restarted = recovered.restart(44)
    assert restarted.memory_mb == 1024 and restarted.cpus == 1.5
    assert restarted.resource_token != old.resource_token
    recovered.destroy(44)


def test_admission_refuses_second_kernel_without_starting_docker(db_session_factory, kernel_settings, docker):
    settings = kernel_settings.model_copy(update={"lab_max_kernels": 1})
    manager = KernelManager(settings)
    manager.create_session(45, resource_factory=db_session_factory)
    with pytest.raises(RuntimeError, match="占满"):
        manager.create_session(46, resource_factory=db_session_factory)
    assert len(docker.containers) == 1
    manager.destroy(45)


def test_restart_api_after_manager_process_replacement_binds_request_database(client, db_session_factory, kernel_settings, docker, monkeypatch):
    student = create_user(db_session_factory, "restart-student", "student")
    teacher = create_user(db_session_factory, 'restart-teacher', 'teacher')
    token, _ = login(client, student.username)
    manager = KernelManager(kernel_settings)
    with db_session_factory() as db:
        template, version = _template(db, teacher.id)
        module = ExperimentModule(name="公开实验", owner_id=teacher.id, status="published", template_id=template.id)
        db.add(module)
        db.flush()
        record = ExperimentRecord(module_id=module.id, student_id=student.id, template_version_id=version.id,
            environment_version_id=version.environment_version_id)
        db.add(record)
        db.commit()
        env = db.get(EnvironmentVersion, version.environment_version_id)
        old = ensure_record_kernel(manager, record, version, db, image_ref=env.image_digest, environment_version_id=env.id)
        record_id = record.id
        module_id, version_id = module.id, version.id
    offering_id = grant_experiment(db_session_factory, 'module', module_id, [student.username],
                                   template_version_id=version_id)
    with db_session_factory() as db:
        record = db.get(ExperimentRecord, record_id)
        record.offering_id, record.scope_key = offering_id, f'offering:{offering_id}'
        db.commit()
    replacement = KernelManager(kernel_settings)
    monkeypatch.setattr("app.api.experiments.get_kernel_manager", lambda: replacement)
    response = client.post(f"/api/v1/experiments/records/{record_id}/restart", headers=auth_header(token))
    assert response.status_code == 200, response.text
    with db_session_factory() as db:
        lease = db.get(LabResourceLease, f"kernel:{record_id}")
        assert lease is not None and lease.token != old.resource_token
    assert record_id in replacement._resource_factories
    replacement.destroy(record_id)
