"""The load harness must preserve scope, independent sessions and honest evidence."""
import asyncio
import importlib.util
import json
from pathlib import Path
import random
import sys
import time

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/verify_school_capacity.py'
spec = importlib.util.spec_from_file_location('school_capacity', SCRIPT)
capacity = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = capacity
spec.loader.exec_module(capacity)


def manifest_file(tmp_path, **changes):
    data = {'base_url': 'https://school.invalid', 'allowed_offering_ids': [10], 'students': [
        {'username': '000001', 'password': 'NeverPrintThisPassword!', 'course_id': 1,
         'offering_id': 10, 'record_id': 101, 'cell_id': 'cell-a'},
        {'username': '000002', 'password': 'AnotherPrivatePassword!', 'course_id': 1,
         'offering_id': 10, 'record_id': 102, 'cell_id': 'cell-a'},
    ]}
    data.update(changes)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def options(tmp_path, *arguments):
    return capacity.parse_options(['--manifest', str(tmp_path / 'manifest.json'), '--users', '2',
        '--duration', '.13', '--ramp-seconds', '0', '--interval', '.05', '--run-weight', '0', *arguments])


def test_manifest_preserves_zeroes_and_requires_scope_and_owner_only_permissions(tmp_path):
    path = manifest_file(tmp_path)
    manifest = capacity.load_manifest(path, 2)
    assert manifest.students[0].username == '000001'
    assert 'NeverPrint' not in repr(manifest)
    path.chmod(0o644)
    with pytest.raises(capacity.ManifestError, match='0600'):
        capacity.load_manifest(path, 2)
    path = manifest_file(tmp_path, allowed_offering_ids=[20])
    with pytest.raises(capacity.ManifestError, match='allow-list'):
        capacity.load_manifest(path, 2)


@pytest.mark.parametrize('base_url', ['http://remote.example', 'https://user:secret@example.com',
    'https://example.com/api', 'https://example.com?credential=secret', 'https://[broken', 'https://example.com:bad'])
def test_manifest_rejects_insecure_or_credential_bearing_remote_origins(tmp_path, base_url):
    with pytest.raises(capacity.ManifestError):
        capacity.load_manifest(manifest_file(tmp_path, base_url=base_url), 2)


def test_manifest_cannot_multiply_one_account_or_record_into_concurrent_users(tmp_path):
    path = manifest_file(tmp_path)
    data = json.loads(path.read_text())
    data['students'][1]['record_id'] = data['students'][0]['record_id']
    path.write_text(json.dumps(data))
    with pytest.raises(capacity.ManifestError, match='independent'):
        capacity.load_manifest(path, 2)


def test_formal_submission_requires_explicit_flag_and_has_per_user_attempt_bound(tmp_path):
    with pytest.raises(SystemExit):
        options(tmp_path, '--submit-weight', '5')
    opted = options(tmp_path, '--read-weight', '0', '--submit-weight', '5', '--allow-formal-submit')
    assert capacity.choose_action(opted, random.Random(1), 0) == 'formal_submit'
    assert capacity.choose_action(opted, random.Random(1), 1) is None


def test_histogram_is_bounded_and_does_not_understate_p95():
    latency = capacity.Latency()
    for i in range(50000):
        latency.add(i / 10000)
    assert len(latency.bins) <= 2800
    assert latency.percentile(.95) == 4750
    assert latency.read()['count'] == 50000
    queue = capacity.Latency()
    queue.add(1800)
    assert queue.percentile(.95) >= 1800000
    assert queue.read()['max_ms'] == 1800000


class SchoolTransport:
    def __init__(self, wrong_scope=False, short_tokens=False, slow_reads=False, legacy_detail=False):
        self.wrong_scope, self.short_tokens, self.slow_reads = wrong_scope, short_tokens, slow_reads
        self.legacy_detail = legacy_detail
        self.calls = []
        self.jobs = {}
        self.read_started = None

    async def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path, request.headers.get('Authorization'), request.headers.get('Cookie')))
        if path.endswith('/login'):
            data = json.loads(request.content)
            ident = int(data['username'])
            return httpx.Response(200, json=self.tokens(ident), headers={'Set-Cookie': f'dai_refresh_token=refresh-{ident}; Path=/api/v1/auth'})
        if path.endswith('/refresh'):
            ident = int(request.headers['Cookie'].split('refresh-')[-1])
            return httpx.Response(200, json=self.tokens(ident), headers={'Set-Cookie': f'dai_refresh_token=refresh-{ident}; Path=/api/v1/auth'})
        ident = int(request.headers['Authorization'].split('token-')[-1])
        if path == '/api/v1/offerings/10':
            return httpx.Response(200, json={'id': 10, 'course_id': 1, 'status': 'active'})
        if path == f'/api/v1/experiments/records/{100 + ident}':
            data = {'id': 100 + ident, 'student_id': ident,
                'offering_id': 20 if self.wrong_scope else 10, 'record_revision': 1, 'lab_enabled': True,
                'cells': [{'id': 'cell-a', 'type': 'code', 'source': 'result = 2'}]}
            if self.legacy_detail:
                data.pop('offering_id')
            return httpx.Response(200, json=data)
        if path == '/api/v1/experiments/records':
            return httpx.Response(200, json={'items': [{'id': 100 + ident, 'student_id': ident,
                'offering_id': 20 if self.wrong_scope else 10}]})
        if path.endswith('/execute'):
            return httpx.Response(200, json={'outputs': []})
        if path.endswith('/check'):
            self.jobs[ident] = 0
            return httpx.Response(202, json={'id': ident, 'status': 'queued'})
        if '/labs/runs/' in path:
            tick = self.jobs.get(ident, 0)
            self.jobs[ident] = tick + 1
            state = ['queued', 'recovering', 'succeeded'][min(tick, 2)]
            return httpx.Response(200, json={'id': ident, 'status': state, 'created_at': '2026-10-08T00:00:00Z',
                'started_at': '2026-10-08T00:00:01Z' if tick else None})
        if path.endswith('/submit'):
            return httpx.Response(201, json={'id': ident})
        if '/labs/submissions/' in path:
            return httpx.Response(200, json={'run': {'id': ident, 'status': 'queued'}})
        if self.slow_reads:
            if self.read_started is not None:
                self.read_started.set()
            await asyncio.sleep(5)
        return httpx.Response(200, json={'items': []})

    def tokens(self, ident):
        return {'access_token': f'token-{ident}', 'expires_in': 1 if self.short_tokens else 1800,
                'user': {'id': ident, 'role': 'student', 'must_change_password': False}}


def test_each_student_has_independent_cookie_and_refresh_session_and_redacted_report(tmp_path):
    config = capacity.load_manifest(manifest_file(tmp_path), 2)
    fake = SchoolTransport(short_tokens=True)
    report = asyncio.run(capacity.run_verification(config, options(tmp_path, '--duration', '1.15'), transport=httpx.MockTransport(fake)))
    assert report['started_with_independent_authenticated_students'] == 2
    refreshed = [call for call in fake.calls if call[1].endswith('/refresh')]
    assert {x[3] for x in refreshed} == {'dai_refresh_token=refresh-1', 'dai_refresh_token=refresh-2'}
    assert {x[2] for x in fake.calls if x[1].endswith('/materials')} <= {'Bearer token-1', 'Bearer token-2'}
    assert report['targets']['read_p95_le_1000ms']
    assert not report['evidence_complete']['200_students_30_minutes']
    assert not report['evidence_complete']['read_run_formal_submit_observed']
    text = json.dumps(report)
    for secret in ('000001', 'NeverPrint', 'token-', 'refresh-', 'school.invalid'):
        assert secret not in text


def test_scope_mismatch_aborts_before_any_load_or_writes(tmp_path):
    manifest = capacity.load_manifest(manifest_file(tmp_path), 2)
    fake = SchoolTransport(wrong_scope=True)
    opts = options(tmp_path, '--run-weight', '20', '--submit-weight', '5', '--allow-formal-submit')
    report = asyncio.run(capacity.run_verification(manifest, opts, transport=httpx.MockTransport(fake)))
    assert report['measured_duration_seconds'] == 0
    assert report['safe_error_categories']['preflight:TEST_SCOPE_OR_LOGIN_REJECTED'] == 2
    assert not any(x[1].endswith(('/execute', '/check', '/submit')) for x in fake.calls)


def test_signal_stop_cancels_inflight_requests_and_keeps_report_honest(tmp_path):
    manifest = capacity.load_manifest(manifest_file(tmp_path), 2)
    fake = SchoolTransport(slow_reads=True)
    async def run():
        stop = asyncio.Event()
        fake.read_started = asyncio.Event()
        stopping_at = None
        async def interrupt():
            nonlocal stopping_at
            await asyncio.wait_for(fake.read_started.wait(), timeout=5)
            stopping_at = time.monotonic()
            stop.set()
        interruption = asyncio.create_task(interrupt())
        result = await capacity.run_verification(manifest, options(tmp_path, '--duration', '60'), stop=stop,
            transport=httpx.MockTransport(fake))
        await interruption
        return result, time.monotonic() - stopping_at
    report, cancellation_seconds = asyncio.run(run())
    assert cancellation_seconds < 1
    assert report['interrupted']
    assert report['interrupted_requests_with_unknown_outcome'] >= 1
    assert not report['evidence_complete']['200_students_30_minutes']


def test_runtime_queue_recovery_is_separate_from_http_latencies(tmp_path):
    manifest = capacity.load_manifest(manifest_file(tmp_path), 2)
    fake = SchoolTransport()
    opts = options(tmp_path, '--duration', '.3', '--read-weight', '0', '--run-weight', '20')
    report = asyncio.run(capacity.run_verification(manifest, opts, transport=httpx.MockTransport(fake)))
    states = report['runtime']['state_transitions']
    assert states['queued'] >= 2 and states['recovering'] >= 2 and states['succeeded'] >= 2
    assert report['runtime']['queue_wait']['p95_upper_ms'] == 1000
    assert not report['targets']['read_p95_le_1000ms']
    assert not report['evidence_complete']['runtime_recovery_and_disk_limits']


def test_unknown_error_body_cannot_leak_into_report():
    response = httpx.Response(500, json={'error': {'code': 'NeverPrintThisPassword', 'message': 'secret token'}})
    assert capacity.safe_error(response) == 'HTTP_500'
    assert capacity.safe_error(httpx.Response(409, json={'detail': {'code': 'LAB_ATTEMPTS_EXHAUSTED'}})) == 'LAB_ATTEMPTS_EXHAUSTED'
    assert capacity.safe_error(httpx.Response(500, json={'detail': {'code': {'secret': 'password'}}})) == 'HTTP_500'


@pytest.mark.parametrize('wrong_scope', [False, True])
def test_old_detail_schema_still_verifies_server_list_scope(tmp_path, wrong_scope):
    manifest = capacity.load_manifest(manifest_file(tmp_path), 2)
    fake = SchoolTransport(legacy_detail=True, wrong_scope=wrong_scope)
    report = asyncio.run(capacity.run_verification(manifest, options(tmp_path), transport=httpx.MockTransport(fake)))
    assert report['started_with_independent_authenticated_students'] == (0 if wrong_scope else 2)
    assert any(call[1] == '/api/v1/experiments/records' for call in fake.calls)


def test_explicit_formal_traffic_confirms_once_per_student_without_automatic_quota_override(tmp_path):
    manifest = capacity.load_manifest(manifest_file(tmp_path), 2)
    fake = SchoolTransport()
    opts = options(tmp_path, '--read-weight', '0', '--submit-weight', '100', '--allow-formal-submit')
    report = asyncio.run(capacity.run_verification(manifest, opts, transport=httpx.MockTransport(fake)))
    assert report['latency']['submit_confirmed']['count'] == 2
    assert len([call for call in fake.calls if call[1].endswith('/submit')]) == 2
    assert report['targets']['submit_confirmation_p95_le_2000ms']
    assert not report['evidence_complete']['read_run_formal_submit_observed']


def test_report_file_is_private_and_atomic(tmp_path):
    path = tmp_path / 'runtime/report.json'
    capacity.write_report(path, {'count': 2})
    assert json.loads(path.read_text()) == {'count': 2}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_suffix('.tmp').exists()
