#!/usr/bin/env python3
"""Opt-in HTTP load against explicitly listed disposable school test accounts.

No application imports, database writes, account creation, or secret-bearing output.
Run with the backend virtualenv, which pins HTTPX in requirements.txt.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import signal
import stat
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


SAFE_ERRORS = frozenset({
    'KERNEL_BUSY', 'KERNEL_ERROR', 'ENVIRONMENT_IMAGE_MISSING', 'LAB_DISABLED',
    'LAB_CHECK_ACTIVE', 'LAB_TRAINING_ACTIVE', 'LAB_EVALUATION_ACTIVE',
    'LAB_ATTEMPTS_EXHAUSTED', 'LAB_IDENTICAL_SUBMISSION', 'LAB_CELL_CHECK_UNAVAILABLE',
    'LAB_CELL_CHECK_NOT_FOUND', 'LAB_GPU_UNAVAILABLE', 'RESOURCE_CONFIGURATION',
    'WORKER_LOST', 'TIMEOUT', 'DISK_LIMIT', 'MEMORY_LIMIT', 'RATE_LIMITED',
    'LOGIN_RATE_LIMITED', 'LAB_DEADLINE_PASSED', 'ASSESSMENT_SWITCH_PENDING',
    'DISK_UNAVAILABLE', 'OUTPUT_LIMIT', 'EXECUTION_TIMEOUT', 'LOG_LIMIT',
    'SANDBOX_UNAVAILABLE', 'SANDBOX_CLEANUP_FAILED', 'GPU_UNAVAILABLE', 'GPU_NOT_READY',
})
RUN_STATES = frozenset({'queued', 'running', 'recovering', 'succeeded', 'failed', 'cancelled', 'needs_review'})
TERMINAL_STATES = frozenset({'succeeded', 'failed', 'cancelled', 'needs_review'})


class ManifestError(ValueError):
    """Messages intentionally never contain manifest data."""


@dataclass(repr=False, frozen=True)
class TestStudent:
    username: str
    password: str
    course_id: int
    offering_id: int
    record_id: int
    cell_id: str | None = None


@dataclass(repr=False, frozen=True)
class Manifest:
    base_url: str
    students: tuple[TestStudent, ...]


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_manifest(path: Path, users: int) -> Manifest:
    # Credential files belong in ignored runtime storage, with owner-only access.
    if os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ManifestError('Manifest must have mode 0600 or stricter')
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ManifestError('Manifest exceeds bounded input size')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise ManifestError('Manifest is not valid UTF-8 JSON') from None
    if not isinstance(data, dict) or set(data) != {'base_url', 'allowed_offering_ids', 'students'}:
        raise ManifestError('Manifest requires base_url, allowed_offering_ids, students only')
    try:
        url = urlsplit(data['base_url']) if isinstance(data['base_url'], str) else None
        if url:
            _ = url.port
    except ValueError:
        raise ManifestError('Invalid origin') from None
    if (not url or not url.hostname or url.scheme not in {'http', 'https'} or url.username
            or url.password or url.query or url.fragment or url.path not in {'', '/'}):
        raise ManifestError('base_url must be an HTTP(S) origin without credentials or path')
    if url.scheme == 'http' and url.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ManifestError('Remote verification requires HTTPS')
    allowed = data['allowed_offering_ids']
    raw_students = data['students']
    if not isinstance(allowed, list) or not allowed or not all(_positive_int(x) for x in allowed):
        raise ManifestError('Explicit test offering allow-list is required')
    if not isinstance(raw_students, list) or not 1 <= users <= len(raw_students):
        raise ManifestError('Not enough independent test students for requested user count')
    students = []
    usernames, records = set(), set()
    for row in raw_students:
        if not isinstance(row, dict) or set(row) - {'username', 'password', 'course_id', 'offering_id', 'record_id', 'cell_id'}:
            raise ManifestError('Unknown student manifest fields')
        if (not all(isinstance(row.get(x), str) and row[x] for x in ('username', 'password'))
                or not all(_positive_int(row.get(x)) for x in ('course_id', 'offering_id', 'record_id'))):
            raise ManifestError('Each test student needs credentials and positive scope identifiers')
        if row['offering_id'] not in allowed:
            raise ManifestError('Student references an offering outside test allow-list')
        if row['username'] in usernames or row['record_id'] in records:
            raise ManifestError('Students and experiment records must be independent')
        if row.get('cell_id') is not None and (not isinstance(row['cell_id'], str) or not 1 <= len(row['cell_id']) <= 100):
            raise ManifestError('Invalid Cell identifier')
        usernames.add(row['username'])
        records.add(row['record_id'])
        students.append(TestStudent(**row))
    return Manifest(data['base_url'].rstrip('/'), tuple(students[:users]))


@dataclass
class Latency:
    # Bounded histogram: 1 ms through 2 s, 10 ms through 10 s, 100 ms
    # through 120 s, then exponential buckets for long queue waits.
    # Percentiles are upper bucket bounds, never understated reservoir estimates.
    bins: Counter = field(default_factory=Counter)
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def add(self, seconds):
        value = max(0.0, float(seconds) * 1000)
        if value > 120000:
            upper = 2 ** math.ceil(math.log2(value))
        else:
            width = 1 if value <= 2000 else 10 if value <= 10000 else 100
            upper = math.ceil(value / width) * width
        self.bins[upper] += 1
        self.count += 1
        self.total_ms += value
        self.max_ms = max(self.max_ms, value)

    def percentile(self, fraction):
        if not self.count:
            return None
        target, total = math.ceil(self.count * fraction), 0
        for upper, count in sorted(self.bins.items()):
            total += count
            if total >= target:
                return upper

    def read(self):
        return {'count': self.count, 'mean_ms': round(self.total_ms / self.count, 2) if self.count else None,
                'p95_upper_ms': self.percentile(.95), 'max_ms': round(self.max_ms, 2)}


@dataclass
class Measurements:
    latencies: dict = field(default_factory=dict)
    responses: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    run_events: Counter = field(default_factory=Counter)
    queue_wait: Latency = field(default_factory=Latency)
    authenticated: int = 0
    interrupted_requests: int = 0
    unfinished_jobs: int = 0

    def record(self, group, seconds, status, error=None):
        self.latencies.setdefault(group, Latency()).add(seconds)
        self.responses[f'{group}:{status}'] += 1
        if error:
            self.errors[f'{group}:{error}'] += 1

    def report(self, options, elapsed, interrupted=False):
        read = self.latencies.get('read', Latency()).read()
        submit = self.latencies.get('submit_confirmed', Latency()).read()
        read_ok = read['count'] > 0 and read['p95_upper_ms'] <= 1000 and not any(k.startswith('read:') for k in self.errors)
        submit_ok = submit['count'] > 0 and submit['p95_upper_ms'] <= 2000
        return {
            'schema_version': 1,
            'started_with_independent_authenticated_students': self.authenticated,
            'requested_users': options.users, 'requested_duration_seconds': options.duration,
            'measured_duration_seconds': round(elapsed, 2), 'interrupted': interrupted,
            'traffic_weights': {'read': options.read_weight, 'run': options.run_weight, 'formal_submit': options.submit_weight},
            'formal_submit_enabled': options.allow_formal_submit,
            'latency': {key: value.read() for key, value in self.latencies.items()},
            'http_responses': dict(self.responses), 'safe_error_categories': dict(self.errors),
            'runtime': {'state_transitions': dict(self.run_events), 'queue_wait': self.queue_wait.read(),
                        'unfinished_observed_jobs': self.unfinished_jobs},
            'interrupted_requests_with_unknown_outcome': self.interrupted_requests,
            'targets': {'read_p95_le_1000ms': read_ok, 'submit_confirmation_p95_le_2000ms': submit_ok},
            'evidence_complete': {'200_students_30_minutes': self.authenticated >= 200 and elapsed >= 1800 and not interrupted,
                                  'read_run_formal_submit_observed': read['count'] > 0 and submit['count'] > 0
                                      and self.responses['execute:200'] > 0 and self.responses['check_enqueue:202'] > 0,
                                  'runtime_recovery_and_disk_limits': False},
            'scope': 'Only explicitly allow-listed test offerings; no server addresses, accounts, tokens or response bodies retained.',
            'limits': 'HTTP evidence does not prove resource recovery, disk isolation, backup restore or target-server capacity by itself.',
        }


def safe_error(response):
    if response.is_success:
        return None
    try:
        data = response.json()
        code = (data.get('detail') or data.get('error') or {}).get('code') if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        code = None
    return code if isinstance(code, str) and code in SAFE_ERRORS else f'HTTP_{response.status_code}'


class StudentSession:
    def __init__(self, student, client, measurements):
        self.student, self.client, self.measurements = student, client, measurements
        self.token = None
        self.expires = 0.0
        self.revision = 0
        self.code = ''
        self.jobs = {}  # At most 32 outstanding test-created job IDs per actor.
        self.formal_attempts = 0

    async def authenticate(self, refresh=False):
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(self.client.post('/api/v1/auth/refresh' if refresh else '/api/v1/auth/login',
                json=None if refresh else {'username': self.student.username, 'password': self.student.password}),
                timeout=self.client.timeout.read)
        except (httpx.HTTPError, asyncio.TimeoutError):
            self.measurements.record('auth', time.monotonic() - started, 'transport_error', 'TRANSPORT')
            return False
        self.measurements.record('auth', time.monotonic() - started, response.status_code, safe_error(response))
        if not response.is_success:
            return False
        try:
            data = response.json()
            user = data['user']
            if user['role'] != 'student' or user.get('must_change_password') or not _positive_int(user.get('id')):
                return False
            if not isinstance(data.get('access_token'), str) or not isinstance(data.get('expires_in'), (int, float)):
                return False
            self.token = data['access_token']
            self.expires = time.monotonic() + max(1.0, data['expires_in'] - 30)
            self.user_id = user['id']
            self.measurements.run_events['refresh_succeeded' if refresh else 'login_succeeded'] += 1
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    async def request(self, group, method, path, **kwargs):
        started = time.monotonic()
        try:
            if time.monotonic() >= self.expires and not await self.authenticate(refresh=True):
                self.measurements.record(group, time.monotonic() - started, 'auth_failed', 'SESSION_REFRESH_FAILED')
                return None
            response = await asyncio.wait_for(self.client.request(method, path,
                headers={'Authorization': f'Bearer {self.token}'}, **kwargs), timeout=self.client.timeout.read)
            # A rejected 401 has not crossed the authenticated application boundary.
            # Retry exactly once with the same UUID payload for queue/submit writes.
            if response.status_code == 401 and await self.authenticate(refresh=True):
                response = await asyncio.wait_for(self.client.request(method, path,
                    headers={'Authorization': f'Bearer {self.token}'}, **kwargs), timeout=self.client.timeout.read)
            self.measurements.record(group, time.monotonic() - started, response.status_code, safe_error(response))
            if group == 'submit' and response.is_success:
                self.measurements.latencies.setdefault('submit_confirmed', Latency()).add(time.monotonic() - started)
            return response
        except asyncio.CancelledError:
            self.measurements.interrupted_requests += 1
            raise
        except (httpx.HTTPError, asyncio.TimeoutError):
            self.measurements.record(group, time.monotonic() - started, 'transport_error', 'TRANSPORT')
            return None

    async def prepare(self, run_enabled):
        if not await self.authenticate():
            return False
        s = self.student
        offering = await self.request('preflight', 'GET', f'/api/v1/offerings/{s.offering_id}')
        record = await self.request('preflight', 'GET', f'/api/v1/experiments/records/{s.record_id}')
        if not offering or not record or not offering.is_success or not record.is_success:
            return False
        try:
            own_offering, own_record = offering.json(), record.json()
            # Earlier detail schemas omitted offering_id; the server-owned list
            # projection supplies the missing context, never the client manifest.
            if 'offering_id' not in own_record:
                listing = await self.request('preflight', 'GET', '/api/v1/experiments/records')
                if not listing or not listing.is_success:
                    return False
                linked = next((row for row in listing.json()['items'] if row['id'] == s.record_id), None)
                if not linked or linked['student_id'] != self.user_id:
                    return False
                own_record['offering_id'] = linked['offering_id']
            if (own_offering['id'] != s.offering_id or own_offering['course_id'] != s.course_id
                    or own_offering['status'] != 'active' or own_record['offering_id'] != s.offering_id
                    or own_record['student_id'] != self.user_id or own_record['id'] != s.record_id):
                return False
            self.revision = own_record['record_revision']
            if run_enabled:
                cell = next((x for x in own_record['cells'] if x['id'] == s.cell_id and x['type'] == 'code'), None)
                if not cell or not own_record.get('lab_enabled'):
                    return False
                self.code = cell['source']
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        self.measurements.authenticated += 1
        return True

    def remember_job(self, run):
        if isinstance(run, dict) and _positive_int(run.get('id')) and len(self.jobs) < 32:
            self.jobs.setdefault(run['id'], {'status': None, 'queue_measured': False})

    async def poll_jobs(self):
        # One polling request per turn prevents queue monitoring becoming a burst.
        if not self.jobs:
            return
        ident = next(iter(self.jobs))
        response = await self.request('runtime_poll', 'GET', f'/api/v1/labs/runs/{ident}')
        if not response or not response.is_success:
            return
        try:
            data, previous = response.json(), self.jobs[ident]
            state = data.get('status')
            if state not in RUN_STATES:
                return
            if previous['status'] != state:
                self.measurements.run_events[state] += 1
                previous['status'] = state
            if data.get('created_at') and data.get('started_at') and not previous['queue_measured']:
                wait = (datetime.fromisoformat(data['started_at'].replace('Z', '+00:00'))
                        - datetime.fromisoformat(data['created_at'].replace('Z', '+00:00'))).total_seconds()
                self.measurements.queue_wait.add(wait)
                previous['queue_measured'] = True
            code = data.get('error_code')
            if state in TERMINAL_STATES:
                if code:
                    self.measurements.errors[f'runtime:{code if code in SAFE_ERRORS else "OTHER_RUNTIME_FAILURE"}'] += 1
                del self.jobs[ident]
            else:
                self.jobs[ident] = self.jobs.pop(ident)  # Round-robin pending jobs.
        except (ValueError, KeyError, TypeError, AttributeError):
            self.measurements.errors['runtime:INVALID_STATUS_RESPONSE'] += 1

    async def act(self, action):
        s = self.student
        if action == 'read':
            paths = [f'/api/v1/courses/{s.course_id}', f'/api/v1/courses/{s.course_id}/materials',
                     f'/api/v1/offerings/{s.offering_id}', f'/api/v1/experiments/records/{s.record_id}']
            await self.request('read', 'GET', random.choice(paths))
        elif action == 'run':
            if self.jobs:
                self.measurements.run_events['run_deferred_while_job_pending'] += 1
                return
            response = await self.request('execute', 'POST',
                f'/api/v1/experiments/records/{s.record_id}/cells/{quote(s.cell_id, safe="")}/execute', json={'code': self.code})
            if not response or not response.is_success:
                return
            result = response.json()
            if result.get('diagnostic') or any(x.get('msg_type') == 'error' for x in result.get('outputs', [])):
                self.measurements.errors['execute:PROGRAM_FAILED'] += 1
                return
            response = await self.request('check_enqueue', 'POST',
                f'/api/v1/labs/records/{s.record_id}/cells/{quote(s.cell_id, safe="")}/check',
                json={'client_request_id': str(uuid4()), 'expected_record_revision': self.revision})
            if response and response.is_success:
                self.remember_job(response.json())
        elif action == 'formal_submit':
            self.formal_attempts += 1
            response = await self.request('submit', 'POST', f'/api/v1/experiments/records/{s.record_id}/submit',
                json={'client_request_id': str(uuid4()), 'expected_record_revision': self.revision})
            if response and response.is_success:
                response = await self.request('submission_poll', 'GET', f'/api/v1/labs/submissions/{response.json()["id"]}')
                if response and response.is_success:
                    self.remember_job(response.json().get('run'))


def choose_action(options, rng, formal_attempts):
    actions, weights = ['read', 'run'], [options.read_weight, options.run_weight]
    if options.allow_formal_submit and formal_attempts < options.max_submissions_per_user:
        actions.append('formal_submit')
        weights.append(options.submit_weight)
    if sum(weights) <= 0:
        return None
    return rng.choices(actions, weights=weights, k=1)[0]


async def wait_or_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as output:
        os.chmod(temp, 0o600)
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')
    temp.replace(path)


async def run_verification(manifest, options, stop=None, transport=None):
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    measurements = Measurements()
    clients, sessions = [], []
    for student in manifest.students:
        client = httpx.AsyncClient(base_url=manifest.base_url, timeout=options.timeout,
            follow_redirects=False, limits=httpx.Limits(max_connections=2, max_keepalive_connections=2), transport=transport)
        clients.append(client)
        sessions.append(StudentSession(student, client, measurements))
    tasks = []
    progress_task = None
    started, interrupted = None, False
    try:
        async def prepare(index, session):
            await wait_or_stop(stop, options.ramp_seconds * index / options.users)
            return False if stop.is_set() else await session.prepare(options.run_weight > 0)

        tasks = [asyncio.create_task(prepare(i, session)) for i, session in enumerate(sessions)]
        preparation = asyncio.gather(*tasks, return_exceptions=True)
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait((preparation, stopper), return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            interrupted = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return measurements.report(options, 0, interrupted=True)
        stopper.cancel()
        prepared = [result is True for result in await preparation]
        if not all(prepared):
            measurements.errors['preflight:TEST_SCOPE_OR_LOGIN_REJECTED'] += prepared.count(False)
            return measurements.report(options, 0)
        started = time.monotonic()
        deadline = started + options.duration

        async def persist_progress():
            while not stop.is_set():
                snapshot = measurements.report(options, time.monotonic() - started)
                snapshot['run_state'] = 'running'
                write_report(options.report.with_name('progress.json'), snapshot)
                await wait_or_stop(stop, 15)

        progress_task = asyncio.create_task(persist_progress())

        async def worker(index, session):
            rng = random.Random(index)
            try:
                while not stop.is_set() and time.monotonic() < deadline:
                    await session.poll_jobs()
                    action = choose_action(options, rng, session.formal_attempts)
                    if action:
                        await session.act(action)
                    await wait_or_stop(stop, options.interval * rng.uniform(.75, 1.25))
            except (ValueError, KeyError, TypeError, AttributeError):
                measurements.errors['worker:INVALID_API_RESPONSE'] += 1
            finally:
                measurements.unfinished_jobs += len(session.jobs)

        tasks = [asyncio.create_task(worker(i, session)) for i, session in enumerate(sessions)]
        all_workers = asyncio.gather(*tasks, return_exceptions=True)
        stopper = asyncio.create_task(stop.wait())
        timer = asyncio.create_task(asyncio.sleep(options.duration))
        await asyncio.wait((all_workers, stopper, timer), return_when=asyncio.FIRST_COMPLETED)
        interrupted = stop.is_set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stopper.cancel()
        timer.cancel()
        return measurements.report(options, time.monotonic() - started, interrupted=interrupted)
    finally:
        if progress_task:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(client.aclose() for client in clients))
        for sig in installed:
            loop.remove_signal_handler(sig)


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--users', type=int, default=200)
    parser.add_argument('--duration', type=float, default=1800)
    parser.add_argument('--ramp-seconds', type=float, default=60)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--read-weight', type=float, default=80)
    parser.add_argument('--run-weight', type=float, default=20)
    parser.add_argument('--submit-weight', type=float, default=0)
    parser.add_argument('--allow-formal-submit', action='store_true')
    parser.add_argument('--max-submissions-per-user', type=int, default=1)
    parser.add_argument('--validate-only', action='store_true')
    default = Path(__file__).resolve().parents[1] / '.runtime/school-verification' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') / 'report.json'
    parser.add_argument('--report', type=Path, default=default)
    options = parser.parse_args(argv)
    if (not 1 <= options.users <= 2000 or not 0 < options.duration <= 86400 or not 0 <= options.ramp_seconds <= 3600
            or not .05 <= options.interval <= 60 or not 1 <= options.timeout <= 60
            or not 1 <= options.max_submissions_per_user <= 100
            or not all(math.isfinite(x) and x >= 0 for x in (options.read_weight, options.run_weight, options.submit_weight))
            or options.read_weight + options.run_weight + options.submit_weight <= 0):
        parser.error('Invalid bounded load parameters')
    if options.submit_weight > 0 and not options.allow_formal_submit:
        parser.error('Formal submissions require --allow-formal-submit')
    return options


def main(argv=None):
    options = parse_options(argv)
    try:
        manifest = load_manifest(options.manifest, options.users)
        if options.run_weight > 0 and any(not s.cell_id for s in manifest.students):
            raise ManifestError('Run traffic requires a Cell identifier for every selected test student')
    except (OSError, ManifestError):
        print('Manifest rejected: check schema, permissions, test offering allow-list and independent users.')
        return 2
    if options.validate_only:
        print(f'Manifest validated for {options.users} independent test students; no HTTP requests made.')
        return 0
    report = asyncio.run(run_verification(manifest, options))
    report['run_state'] = 'interrupted' if report['interrupted'] else 'finished'
    write_report(options.report, report)
    print(json.dumps({'authenticated_students': report['started_with_independent_authenticated_students'],
                      'measured_seconds': report['measured_duration_seconds'], 'targets': report['targets'],
                      'evidence_complete': report['evidence_complete'], 'interrupted': report['interrupted']}, ensure_ascii=False))
    # Full mixed acceptance additionally needs the external runtime evidence below.
    return 0 if (all(report['targets'].values()) and all(report['evidence_complete'][key]
                   for key in ('200_students_30_minutes', 'read_run_formal_submit_observed'))
                 and not report['safe_error_categories']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
