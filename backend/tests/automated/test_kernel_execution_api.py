"""异步单元格执行 API 契约：202 + execution_id、状态查询、刷新恢复、中断、幂等与配置冻结。"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ExperimentRecord, NotebookTemplate, NotebookTemplateVersion
from app.models.kernel_exec import KernelExecution
from app.schemas.studio import StudioCell, StudioDraftUpdate
from app.services import kernel_queue, studio_service


class FakeKernelManager:
    """API/worker 边界替身：不建容器，记录执行调用。"""

    def __init__(self):
        self.executed = []
        self.destroyed = []
        self.sessions = {}
        self.initialized = set()

    def get_or_create_session(self, record_id, lesson_storage_dir="", **kwargs):
        return self.sessions.setdefault(record_id, SimpleNamespace(container_name="fake"))

    def execute(self, record_id, code, *, timeout_seconds=None, interrupt_event=None):
        self.executed.append((record_id, code, timeout_seconds))
        return {"outputs": [{"msg_type": "stream", "content": {"name": "stdout", "text": "ok"}}],
                "execution_time_ms": 7, "outcome": "succeeded", "state_lost": False, "message": None}

    def destroy(self, record_id):
        self.destroyed.append(record_id)
        self.sessions.pop(record_id, None)

    def interrupt(self, record_id):
        pass

    def restart(self, record_id, *args, **kwargs):
        return self.get_or_create_session(record_id)

    def is_template_initialized(self, record_id, version_id):
        return (record_id, version_id) in self.initialized

    def mark_template_initialized(self, record_id, version_id):
        self.initialized.add((record_id, version_id))


@pytest.fixture()
def exec_case(client, db_session_factory, test_settings, monkeypatch):
    """学生 + 模块 + 已发布模板版本 + 实验记录（经真实 API 创建）。"""
    from conftest import seed_basic_environment
    from kernel_test_support import post_execute
    from lab_test_support import headers, make_case, record

    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings)
    created = record(client, case, test_settings)
    fake = FakeKernelManager()
    monkeypatch.setattr("app.api.experiments.get_kernel_manager", lambda: fake)
    monkeypatch.setattr("app.services.lab_workspace.ensure_record_kernel",
                        lambda *args, **kwargs: None)
    return SimpleNamespace(case=case, record_id=created["id"], fake=fake,
                           headers=headers(case.student, test_settings),
                           other_headers=headers(case.other_student, test_settings),
                           post_execute=post_execute, client=client,
                           session_factory=db_session_factory, settings=test_settings)


def _set_version_timeout(db_session_factory, version_id, seconds):
    with db_session_factory() as db:
        version = db.get(NotebookTemplateVersion, version_id)
        version.cell_timeout_seconds = seconds
        db.commit()


def test_execute_cell_accepts_async_and_freezes_timeout(exec_case):
    """202 + execution_id；超时取发布版本的冻结配置，而不是平台默认。"""
    from kernel_test_support import post_execute

    _set_version_timeout(exec_case.session_factory, exec_case.case.version_id, 45)
    accepted = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                            exec_case.headers, client_request_id=str(uuid.uuid4()))
    assert set(accepted) >= {"execution_id", "cell_id", "status", "timeout_seconds"}
    assert accepted["status"] == "queued"
    assert accepted["cell_id"] == "code"
    assert accepted["timeout_seconds"] == 45, "必须使用发布版本冻结的单格超时"
    assert accepted["queue_position"] == 1

    with exec_case.session_factory() as db:
        row = db.get(KernelExecution, accepted["execution_id"])
        assert row.status == "queued" and row.code == "print(1)"
        assert row.timeout_seconds == 45
        assert row.attempts == 0
        assert row.context["student_id"] == exec_case.case.student.id


def test_duplicate_click_is_idempotent(exec_case):
    from kernel_test_support import post_execute

    request_id = str(uuid.uuid4())
    first = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                         exec_case.headers, client_request_id=request_id)
    second = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                          exec_case.headers, client_request_id=request_id)
    assert first["execution_id"] == second["execution_id"], "重复点击必须复用同一执行任务"
    with exec_case.session_factory() as db:
        rows = db.scalars(select(KernelExecution).where(
            KernelExecution.record_id == exec_case.record_id)).all()
        assert len(rows) == 1


def test_execution_status_and_refresh_recovery(exec_case):
    """状态查询 + 刷新恢复：active 在途时返回、终态后为 null，记录详情带 is_running。"""
    from kernel_test_support import drain_execution, kernel_runtime, post_execute

    accepted = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                            exec_case.headers, client_request_id=str(uuid.uuid4()))
    execution_id = accepted["execution_id"]

    active = exec_case.client.get(
        f"/api/v1/experiments/records/{exec_case.record_id}/executions/active",
        headers=exec_case.headers)
    assert active.status_code == 200, active.text
    assert active.json()["execution"]["id"] == execution_id
    assert active.json()["execution"]["status"] == "queued"

    detail = exec_case.client.get(f"/api/v1/experiments/records/{exec_case.record_id}",
                                  headers=exec_case.headers)
    running_cells = [c["id"] for c in detail.json()["cells"] if c["is_running"]]
    assert running_cells == ["code"], "刷新后页面要能标出正在运行的 cell"

    status = exec_case.client.get(
        f"/api/v1/experiments/records/{exec_case.record_id}/executions/{execution_id}",
        headers=exec_case.headers)
    assert status.status_code == 200 and status.json()["status"] == "queued"

    with kernel_runtime(exec_case.fake):
        drain_execution(exec_case.session_factory, exec_case.settings, execution_id)

    done = exec_case.client.get(
        f"/api/v1/experiments/records/{exec_case.record_id}/executions/{execution_id}",
        headers=exec_case.headers).json()
    assert done["status"] == "succeeded"
    assert done["outputs"][0]["content"]["text"] == "ok"
    assert done["execution_time_ms"] == 7
    assert done["execution_count"] == 1
    assert done["state_lost"] is False and done["timed_out"] is False

    active_after = exec_case.client.get(
        f"/api/v1/experiments/records/{exec_case.record_id}/executions/active",
        headers=exec_case.headers).json()
    assert active_after["execution"] is None
    assert exec_case.fake.executed[-1][1] == "print(1)"


def test_cancel_queued_and_terminal_is_idempotent(exec_case):
    from kernel_test_support import post_execute

    accepted = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                            exec_case.headers, client_request_id=str(uuid.uuid4()))
    execution_id = accepted["execution_id"]
    cancelled = exec_case.client.post(
        f"/api/v1/experiments/records/{exec_case.record_id}/executions/{execution_id}/cancel",
        headers=exec_case.headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested"] is True

    again = exec_case.client.post(
        f"/api/v1/experiments/records/{exec_case.record_id}/executions/{execution_id}/cancel",
        headers=exec_case.headers)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"


def test_other_student_cannot_read_or_cancel_execution(exec_case):
    from kernel_test_support import post_execute

    accepted = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                            exec_case.headers, client_request_id=str(uuid.uuid4()))
    execution_id = accepted["execution_id"]
    for path, method in ((f"/executions/{execution_id}", "get"),
                         ("/executions/active", "get"),
                         (f"/executions/{execution_id}/cancel", "post")):
        response = getattr(exec_case.client, method)(
            f"/api/v1/experiments/records/{exec_case.record_id}{path}",
            headers=exec_case.other_headers)
        assert response.status_code == 404, f"{method} {path}: {response.status_code}"


def test_execute_cell_rejects_hidden_and_unknown_cells(exec_case):
    """隐藏单元格不能被学生直接执行（脚手架不是评测入口）。"""
    with exec_case.session_factory() as db:
        version = db.get(NotebookTemplateVersion, exec_case.case.version_id)
        cells = list(version.cells) + [{
            "id": "hidden", "type": "code", "source": "SECRET = 1",
            "order": 2, "source_hidden": True, "student_editable": False,
        }]
        version.cells = cells
        db.commit()
    response = exec_case.client.post(
        f"/api/v1/experiments/records/{exec_case.record_id}/cells/hidden/execute",
        headers=exec_case.headers, json={"code": "SECRET = 2"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CELL_HIDDEN"
    missing = exec_case.client.post(
        f"/api/v1/experiments/records/{exec_case.record_id}/cells/nope/execute",
        headers=exec_case.headers, json={"code": "print(1)"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "CELL_NOT_FOUND"


def test_worker_uses_frozen_timeout_snapshot(exec_case, monkeypatch):
    """worker 执行时使用准入行里的超时快照（发布后不可变）。"""
    from app.services.kernel_manager import KernelManager
    from app.services.kernel_queue import claim_execution, enqueue_execution

    calls = []
    manager = KernelManager(exec_case.settings)
    manager.execute = lambda record_id, code, **kwargs: (
        calls.append((record_id, code, kwargs.get("timeout_seconds")))
        or {"outputs": [], "execution_time_ms": 3, "outcome": "succeeded",
            "state_lost": False, "message": None})
    monkeypatch.setattr("app.services.kernel_manager.get_kernel_manager", lambda: manager)

    with exec_case.session_factory() as db:
        record = db.get(ExperimentRecord, exec_case.record_id)
        context = {"record_revision": record.record_revision,
                   "student_id": record.student_id, "offering_id": record.offering_id,
                   "template_version_id": record.template_version_id,
                   "environment_version_id": record.environment_version_id}
        execution, created = kernel_queue.enqueue_execution(
            db, record_id=exec_case.record_id, cell_id="code", code="print(2)",
            timeout_seconds=42, client_request_id=str(uuid.uuid4()), context=context)
        assert created is True
        claimed = claim_execution(db, exec_case.settings)
        assert claimed is not None and claimed[0] == execution.id

    from app.worker import kernel_worker
    kernel_worker.process_execution(exec_case.session_factory, execution.id, claimed[1],
                                    exec_case.settings)

    assert calls and calls[0][2] == 42, "worker 必须传准入行冻结的 timeout_seconds"
    with exec_case.session_factory() as db:
        row = db.get(KernelExecution, execution.id)
        assert row.status == "succeeded"


def test_draft_config_is_frozen_into_published_version(db_session_factory, test_settings):
    """草稿可改运行配置；发布后旧版本锁定，调整只影响之后发布的新版本。"""
    from conftest import seed_basic_environment
    from lab_test_support import make_case

    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings, published=False)
    with db_session_factory() as db:
        template = db.get(NotebookTemplate, case.template_id)
        cells = [StudioCell(id="code", type="code", source="print(1)", order=0)]
        studio_service.save_draft(db, template, StudioDraftUpdate(
            draft_revision=template.draft_revision, cells=cells,
            cell_timeout_seconds=300, kernel_idle_seconds=3600), settings=test_settings)
        version = studio_service.publish_template(db, test_settings, template.id, case.teacher)
        assert version.cell_timeout_seconds == 300
        assert version.kernel_idle_seconds == 3600

        # 发布后再改草稿：已发布版本保持锁定
        template = db.get(NotebookTemplate, case.template_id)
        studio_service.save_draft(db, template, StudioDraftUpdate(
            draft_revision=template.draft_revision, cells=cells,
            cell_timeout_seconds=600, kernel_idle_seconds=1800), settings=test_settings)
        locked = db.get(NotebookTemplateVersion, version.id)
        assert locked.cell_timeout_seconds == 300 and locked.kernel_idle_seconds == 3600
        assert template.draft_cell_timeout_seconds == 600
        assert template.draft_kernel_idle_seconds == 1800

        read = studio_service.template_read(db, template, test_settings)
        assert read.draft_cell_timeout_seconds == 600
        assert read.current_version.cell_timeout_seconds == 300
        assert read.current_version.kernel_idle_seconds == 3600


def test_draft_config_range_is_validated(db_session_factory, test_settings):
    """超时 10–1800 秒、空闲 300–7200 秒：越界直接 422（不静默截断）。"""
    from pydantic import ValidationError

    from conftest import seed_basic_environment
    from lab_test_support import make_case

    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings)
    cells = [StudioCell(id="code", type="code", source="print(1)", order=0)]
    with pytest.raises(ValidationError):
        StudioDraftUpdate(draft_revision=1, cells=cells, cell_timeout_seconds=5)
    with pytest.raises(ValidationError):
        StudioDraftUpdate(draft_revision=1, cells=cells, kernel_idle_seconds=120)
    with db_session_factory() as db:
        template = db.get(NotebookTemplate, case.template_id)
        assert template.draft_cell_timeout_seconds == 120
        assert template.draft_kernel_idle_seconds == 900


def test_kernel_migration_is_reversible_on_sqlite(tmp_path):
    """20261013_0001 自己这一段可升级可回滚（disposable SQLite，不碰业务库）。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine, inspect

    backend_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "kernel-migration.db"
    env = dict(os.environ)
    env["DAI_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(backend_root)
    env.pop("DAI_ENVIRONMENT", None)

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend_root,
                              capture_output=True, text=True, timeout=120, env=env)

    # 链上 c5d6e7f8a901 需要 basic 环境版本（照 test_checkpoint2_fixes 的分段升级先例）
    staged = alembic("upgrade", "b4c5d6e7f890")
    assert staged.returncode == 0, staged.stderr
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text(
            "INSERT INTO environment_profiles (id, slug, display_name, status)"
            " VALUES (1, 'basic', 'Python 基础', 'active')"))
        conn.execute(text(
            "INSERT INTO environment_versions (id, profile_id, version_number, status,"
            " base_image_ref, image_digest, minimum_memory_mb, manifest_sha256)"
            " VALUES (1, 1, 1, 'available', 'python:3.12-slim@sha256:0000',"
            " 'sha256:" + "a" * 64 + "', 256, 'm" + "m" * 62 + "')"))
    engine.dispose()

    upgraded = alembic("upgrade", "20261013_0001")
    assert upgraded.returncode == 0, upgraded.stderr
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "kernel_executions" in inspector.get_table_names()
    template_columns = {c["name"] for c in inspector.get_columns("notebook_templates")}
    version_columns = {c["name"] for c in inspector.get_columns("notebook_template_versions")}
    assert {"draft_cell_timeout_seconds", "draft_kernel_idle_seconds"} <= template_columns
    assert {"cell_timeout_seconds", "kernel_idle_seconds"} <= version_columns

    downgraded = alembic("downgrade", "f4a5b6c7d8e9")
    assert downgraded.returncode == 0, downgraded.stderr
    inspector = inspect(engine)
    assert "kernel_executions" not in inspector.get_table_names()
    template_columns = {c["name"] for c in inspector.get_columns("notebook_templates")}
    version_columns = {c["name"] for c in inspector.get_columns("notebook_template_versions")}
    assert not ({"draft_cell_timeout_seconds", "draft_kernel_idle_seconds"} & template_columns)
    assert not ({"cell_timeout_seconds", "kernel_idle_seconds"} & version_columns)
    engine.dispose()

    upgraded_again = alembic("upgrade", "20261013_0001")
    assert upgraded_again.returncode == 0, upgraded_again.stderr


def test_restart_cancels_in_flight_executions(exec_case):
    """重启内核会取消在途/排队执行，避免排队任务在新内核上重放。"""
    from kernel_test_support import post_execute

    accepted = post_execute(exec_case.client, exec_case.record_id, "code", "print(1)",
                            exec_case.headers, client_request_id=str(uuid.uuid4()))
    response = exec_case.client.post(
        f"/api/v1/experiments/records/{exec_case.record_id}/restart",
        headers=exec_case.headers)
    assert response.status_code == 200, response.text
    with exec_case.session_factory() as db:
        row = db.get(KernelExecution, accepted["execution_id"], populate_existing=True)
        assert row.status == "cancelled"
        assert db.get(ExperimentRecord, exec_case.record_id).cells_outputs == {}
