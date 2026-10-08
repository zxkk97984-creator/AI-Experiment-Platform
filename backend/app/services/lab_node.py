"""Explicit Docker daemon identity for comparable CPU benchmark execution."""
from __future__ import annotations

import re
import subprocess


def is_benchmark(kind, snapshot):
    return kind in ('baseline', 'evaluate') and any(
        item.get('source') == 'efficiency' for item in
        (snapshot or {}).get('assessment_config', {}).get('criteria', []))


def matches_worker(kind, snapshot, settings):
    benchmark = is_benchmark(kind, snapshot)
    if settings.lab_worker_mode == 'general':
        return not benchmark
    return (benchmark and bool(settings.lab_benchmark_node_id)
        and settings.lab_worker_node_id == settings.lab_benchmark_node_id
        and (snapshot or {}).get('benchmark_node_id') == settings.lab_benchmark_node_id)


def docker_daemon_id():
    try:
        result = subprocess.run(['docker', 'info', '--format', '{{.ID}}'],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('无法确认 Docker 基准节点身份') from exc
    identity = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', identity):
        raise RuntimeError('无法确认 Docker 基准节点身份')
    return identity


def benchmark_daemon_ready(settings):
    if (settings.lab_worker_mode != 'benchmark' or not settings.lab_benchmark_node_id
            or settings.lab_worker_node_id != settings.lab_benchmark_node_id):
        return False
    try:
        if docker_daemon_id() != settings.lab_benchmark_node_id:
            return False
        # Legacy interactive containers may predate the node configuration.
        result = subprocess.run(['docker', 'ps', '-q', '--filter', 'label=dai.record_id'],
            capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and not result.stdout.strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


def require_interactive_node(settings):
    if settings.lab_worker_mode == 'benchmark':
        raise RuntimeError('专用基准节点不提供交互内核')
    if settings.lab_benchmark_node_id and docker_daemon_id() == settings.lab_benchmark_node_id:
        raise RuntimeError('此 Docker 节点专用于效率评测，不提供交互内核')
