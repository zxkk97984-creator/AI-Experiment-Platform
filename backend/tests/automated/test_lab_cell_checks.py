import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import User
from app.models.lab import LabAssessment, LabRun
from app.schemas.lab import LabAssessmentConfig, LabCellCheck
from app.services.lab_assessment_service import assessment_read, run_read
from app.services.lab_cell_checks import observation_requests, read_observations, template_marks
from app.services.lab_processing import process_run
from app.services.lab_queue import claim_run
from app.services.lab_runtime import RuntimeResult


def rule(kind='numeric_close', **changes):
    return LabCellCheck(id='answer', cell_id='code', kind=kind, variable='result',
        minimum=0, maximum=10, expected_value=7, shape=[2, 3], expected_output='hello', **changes).model_dump()


@pytest.mark.parametrize('actual,passed', [(7, True), (7.0000005, True), (8, False), (True, False), (float('nan'), False)])
def test_numeric_template_ignores_self_reported_pass(actual, passed):
    observations = {'code': {'completed': True, 'stdout': '', 'passed': True,
                             'values': {'answer': {'value': actual, 'passed': True}}}}
    assert template_marks([rule()], observations)['answer']['passed'] is passed


def test_stdout_shape_and_missing_cell_are_independent():
    observations = {'code': {'completed': True, 'stdout': 'hello\r\n', 'values': {'answer': {'shape': [2, 3]}}}}
    assert template_marks([rule('stdout')], observations)['answer']['passed']
    assert template_marks([rule('shape')], observations)['answer']['passed']
    observations['code']['values']['answer']['shape'] = [True, 3]
    assert not template_marks([rule('shape')], observations)['answer']['passed']
    assert not template_marks([rule()], {})['answer']['passed']


def test_invalid_rule_and_hidden_student_projection():
    with pytest.raises(ValidationError):
        LabCellCheck(id='a', cell_id='c', kind='numeric_close', variable='__import__("os")', expected_value=1)
    private = rule()
    private['expected_value'] = 918273.0
    config = LabAssessmentConfig(criteria=[{'id': 'answer', 'name': 'answer', 'description': 'calculate',
        'source': 'cell', 'max_score': 100}], cell_checks=[private]).model_dump()
    row = SimpleNamespace(id=1, version=1, status='published', config=config, template_version_id=2,
        config_hash='a', validated_hash='a', validation_run_id=7, published_at=None)
    public = assessment_read(row)
    assert public['config']['cell_checks'] == [{'id': 'answer', 'cell_id': 'code'}]
    assert '918273' not in json.dumps(public)
    assert 'expected_value' not in json.dumps(observation_requests([private]))


@pytest.mark.parametrize('kind', ['cell_check', 'public_validate', 'evaluate', 'baseline'])
def test_private_checker_logs_never_reach_student_run_projection(kind):
    run = SimpleNamespace(id=1, kind=kind, status='succeeded', device='cpu', result={},
        logs='PRIVATE_CHECKER_REFERENCE=918273', error_code=None, error_message=None,
        created_at=None, started_at=None, finished_at=None)
    assert run_read(run)['logs'] == ''
    assert run_read(run, teacher=True)['logs'] == 'PRIVATE_CHECKER_REFERENCE=918273'


def test_model_cell_scoring_requires_platform_notebook_and_fifty_fifty_is_valid():
    config = LabAssessmentConfig(task_type='tabular_classification', criteria=[
        {'id': 'metric', 'name': 'metric', 'description': 'model performance', 'source': 'metric', 'max_score': 50},
        {'id': 'answer', 'name': 'answer', 'description': 'Notebook result', 'source': 'cell', 'max_score': 50}],
        cell_checks=[rule()])
    assert config.require_platform_training is True
    assert config.allow_external is False


def test_observation_file_is_bounded(tmp_path):
    (tmp_path / '.platform-cell-evidence.json').write_text('{"code":{"completed":"yes"}}')
    with pytest.raises(ValueError):
        read_observations(tmp_path)


@pytest.fixture()
def replay_case(db_session_factory, test_settings, tmp_path, monkeypatch):
    settings = test_settings.model_copy(update={'judge_work_dir': str(tmp_path), 'judge_host_work_dir': '', 'lab_min_free_disk_mb': 64,
        'lab_worker_mode': 'benchmark', 'lab_worker_node_id': 'fixture-daemon', 'lab_benchmark_node_id': 'fixture-daemon'})
    monkeypatch.setattr('app.services.lab_node.benchmark_daemon_ready', lambda settings: True)
    config = LabAssessmentConfig(criteria=[
        {'id': 'answer', 'name': 'answer', 'description': 'calculate', 'source': 'cell', 'max_score': 80},
        {'id': 'speed', 'name': 'speed', 'description': 'uniform CPU', 'source': 'efficiency', 'max_score': 20}],
        cell_checks=[rule()], efficiency_full_seconds=1, efficiency_zero_seconds=10, benchmark_node_id='fixture-daemon',
        reference_cells=[{'id': 'code', 'type': 'code', 'source': 'result=7'}], confirmed=True).model_dump()
    with db_session_factory() as db:
        owner = User(username='cell-replay-teacher', real_name='Teacher', role='teacher', password_hash='test')
        db.add(owner)
        db.flush()
        assessment = LabAssessment(owner_id=owner.id, entry_key='module:987', config=config,
            config_hash='a' * 64, version=1, status='draft')
        db.add(assessment)
        db.flush()
        run = LabRun(owner_id=owner.id, kind='baseline', assessment_id=assessment.id,
            resources=config['resources'], snapshot={'image_ref': 'sha256:' + 'a' * 64,
            'assessment_hash': 'a' * 64, 'assessment_config': copy.deepcopy(config), 'benchmark_node_id': 'fixture-daemon'})
        db.add(run)
        db.commit()
        claimed = claim_run(db, settings)
    return settings, claimed


def test_formal_replay_grades_actual_values_and_three_host_times(db_session_factory, replay_case, monkeypatch):
    settings, (run_id, token) = replay_case
    calls = []
    times = iter([1.0, 3.0, 2.0])
    def training(**kwargs):
        assert 'expected_value' not in json.dumps(kwargs['observations'])
        output = kwargs['work_dir']
        output.mkdir(parents=True)
        (output / '.platform-cell-evidence.json').write_text(json.dumps({'code': {'completed': True,
            'stdout': '', 'values': {'answer': {'value': 7}}, 'passed': False}}))
        calls.append(output)
        return RuntimeResult(0, '', 999, output, next(times))
    monkeypatch.setattr('app.services.lab_processing.execute_training', training)
    process_run(db_session_factory, run_id, token, settings)
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.status == 'succeeded', run.error_message
        assert run.result['score'] == pytest.approx(97.78)
        assert run.result['program_elapsed_seconds'] == 2
        assert run.result['efficiency_samples_seconds'] == [1, 3, 2]
        assert len(calls) == 3


def test_missing_host_time_does_not_use_student_or_old_infrastructure_time(db_session_factory, replay_case, monkeypatch):
    settings, (run_id, token) = replay_case
    def training(**kwargs):
        output = kwargs['work_dir']
        output.mkdir(parents=True)
        (output / '.platform-cell-evidence.json').write_text(json.dumps({'code': {'completed': True, 'stdout': '', 'values': {'answer': {'value': 7}}}}))
        return RuntimeResult(0, 'program_elapsed_seconds=0', 0.001, output)
    monkeypatch.setattr('app.services.lab_processing.execute_training', training)
    process_run(db_session_factory, run_id, token, settings)
    with db_session_factory() as db:
        run = db.get(LabRun, run_id)
        assert run.status == 'needs_review'
        assert 'score' not in run.result
