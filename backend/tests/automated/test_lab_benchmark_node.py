"""Dedicated daemon admission, frozen hardware identity and queue separation."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings
from app.models import LabAssessment, LabResourceLease, LabRun, User
from app.services.lab_node import benchmark_daemon_ready, require_interactive_node
from app.services.lab_queue import claim_run, finish_run, recover_runs
from app.services.lab_resources import reserve_resources, resource_key
from lab_test_support import headers, make_case

NODE = 'fixture-docker-daemon'


@pytest.fixture()
def node_settings(test_settings):
    return test_settings.model_copy(update={'lab_worker_mode': 'benchmark',
        'lab_worker_node_id': NODE, 'lab_benchmark_node_id': NODE})


@pytest.mark.parametrize('worker,benchmark', [('', ''), ('node-a', ''), ('node-a', 'node-b')])
def test_benchmark_mode_requires_matching_explicit_ids(worker, benchmark):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, lab_worker_mode='benchmark',
            lab_worker_node_id=worker, lab_benchmark_node_id=benchmark)


@pytest.mark.parametrize('actual,kernel,error,ready', [
    (NODE, '', False, True), ('other-daemon', '', False, False),
    (NODE, 'interactive-container', False, False), (NODE, '', True, False)])
def test_actual_daemon_identity_and_no_interactive_container_are_both_required(node_settings, monkeypatch, actual, kernel, error, ready):
    def docker(command, **kwargs):
        return SimpleNamespace(returncode=1 if error else 0,
            stdout=actual if command[1] == 'info' else kernel)
    monkeypatch.setattr('app.services.lab_node.subprocess.run', docker)
    assert benchmark_daemon_ready(node_settings) is ready


def test_general_mode_cannot_launch_on_benchmark_daemon_even_after_claim_check(test_settings, node_settings, monkeypatch):
    configured = test_settings.model_copy(update={'lab_benchmark_node_id': NODE})
    monkeypatch.setattr('app.services.lab_node.docker_daemon_id', lambda: NODE)
    with pytest.raises(RuntimeError, match='专用于效率评测'):
        require_interactive_node(configured)
    with pytest.raises(RuntimeError, match='专用基准节点'):
        require_interactive_node(node_settings)
    monkeypatch.setattr('app.services.lab_node.docker_daemon_id', lambda: 'general-daemon')
    require_interactive_node(configured)
    monkeypatch.setattr('app.services.lab_node.docker_daemon_id', Mock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError):
        require_interactive_node(configured)


def _benchmark_run(owner_id, *, node=NODE):
    return LabRun(owner_id=owner_id, kind='evaluate', resources={'cpus': 1, 'memory_mb': 1024},
        snapshot={'benchmark_node_id': node, 'assessment_config': {
            'criteria': [{'id': 'speed', 'source': 'efficiency', 'max_score': 100}]}})


def test_node_queues_and_resource_budgets_are_independent_and_fenced(db_session_factory, test_settings, node_settings, monkeypatch):
    monkeypatch.setattr('app.services.lab_node.benchmark_daemon_ready', lambda settings: True)
    general = test_settings.model_copy(update={'lab_total_cpus': 1})
    benchmark = node_settings.model_copy(update={'lab_total_cpus': 1})
    with db_session_factory() as db:
        owner = User(username='benchmark-queue-owner', real_name='Owner', role='teacher', password_hash='fixture')
        db.add(owner)
        db.flush()
        ordinary = LabRun(owner_id=owner.id, kind='train', resources={'cpus': 1, 'memory_mb': 1024}, snapshot={})
        bench = _benchmark_run(owner.id)
        wrong = _benchmark_run(owner.id, node='other-daemon')
        db.add_all([ordinary, bench, wrong])
        db.commit()
        normal_claim = claim_run(db, general)
        assert normal_claim[0] == ordinary.id
        assert claim_run(db, general) is None
        measured_claim = claim_run(db, benchmark)
        assert measured_claim[0] == bench.id  # general node is already at its CPU limit
        assert claim_run(db, benchmark) is None
        assert db.get(LabResourceLease, resource_key(f'run:{bench.id}', benchmark)) is not None
        assert not finish_run(db, bench.id, 'wrong-token', 'succeeded')
        assert finish_run(db, bench.id, measured_claim[1], 'succeeded')
        db.commit()
        assert db.get(LabResourceLease, resource_key(f'run:{bench.id}', benchmark)) is None
        assert db.get(LabResourceLease, f'run:{ordinary.id}') is not None
        assert db.get(LabRun, wrong.id).status == 'queued'
        with pytest.raises(ValueError, match='专用基准节点'):
            reserve_resources(db, key='kernel:99', token='fixture', kind='kernel',
                cpus=1, memory_mb=1024, settings=benchmark)


def test_benchmark_queue_is_not_starved_by_first_hundred_ordinary_jobs(db_session_factory, node_settings, monkeypatch):
    monkeypatch.setattr('app.services.lab_node.benchmark_daemon_ready', lambda settings: True)
    with db_session_factory() as db:
        owner = User(username='benchmark-priority-owner', real_name='Owner', role='teacher', password_hash='fixture')
        db.add(owner)
        db.flush()
        db.add_all([LabRun(owner_id=owner.id, kind='train', resources={'cpus': 1, 'memory_mb': 1024}, snapshot={}) for _ in range(101)])
        bench = _benchmark_run(owner.id)
        db.add(bench)
        db.commit()
        assert claim_run(db, node_settings)[0] == bench.id


def test_general_worker_cannot_recover_or_stop_bound_benchmark_container(db_session_factory, test_settings, node_settings, monkeypatch):
    monkeypatch.setattr('app.services.lab_node.benchmark_daemon_ready', lambda settings: True)
    stopped = Mock(return_value=True)
    monkeypatch.setattr('app.services.lab_runtime.stop_run_containers', stopped)
    with db_session_factory() as db:
        owner = User(username='benchmark-recovery-owner', real_name='Owner', role='teacher', password_hash='fixture')
        db.add(owner)
        db.flush()
        bench = _benchmark_run(owner.id)
        db.add(bench)
        db.commit()
        claim_run(db, node_settings)
        bench.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        db.commit()
        assert recover_runs(db, test_settings) == 0
        stopped.assert_not_called()
        assert recover_runs(db, node_settings) == 1
        stopped.assert_called_once()


def test_efficiency_draft_and_publish_fail_closed_without_frozen_configured_node(client, db_session_factory, test_settings):
    case = make_case(db_session_factory, test_settings)
    endpoint = f'/api/v1/labs/entries/module/{case.module_id}/assessment'
    config = {**case.config, 'criteria': [{'id': 'speed', 'source': 'efficiency',
        'name': 'CPU time', 'description': 'controlled CPU replay', 'max_score': 100}], 'check_script': ''}
    teacher = headers(case.teacher, test_settings)
    unavailable = client.put(endpoint, headers=teacher, json=config)
    assert unavailable.status_code == 422 and unavailable.json()['detail']['code'] == 'LAB_BENCHMARK_NOT_CONFIGURED'
    test_settings.lab_benchmark_node_id = NODE
    saved = client.put(endpoint, headers=teacher, json=config)
    assert saved.status_code == 200, saved.text
    assert saved.json()['config']['benchmark_node_id'] == NODE
    with db_session_factory() as db:
        draft = db.get(LabAssessment, saved.json()['id'])
        draft.validated_hash = draft.config_hash
        db.commit()
    test_settings.lab_benchmark_node_id = 'replacement-daemon'
    changed = client.post(endpoint + '/publish', headers=teacher)
    assert changed.status_code == 422 and changed.json()['detail']['code'] == 'LAB_BENCHMARK_NODE_MISMATCH'
