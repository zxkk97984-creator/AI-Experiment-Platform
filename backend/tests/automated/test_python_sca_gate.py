"""A failed audit must not be mistaken for a clean dependency report."""
import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('check_sca', ROOT / 'scripts/check_sca.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.mark.parametrize('status,report,expected', [
    (0, {'dependencies': [{'name': 'safe', 'version': '1', 'vulns': []}]}, 0),
    (1, {'dependencies': [{'name': 'unsafe', 'version': '1', 'vulns': [{'id': 'test-advisory'}]}]}, 1),
    (2, {'dependencies': [{'name': 'safe', 'version': '1', 'vulns': []}]}, 1),
    (1, {'dependencies': [{'name': 'safe', 'version': '1', 'vulns': []}]}, 1),
    (0, {'dependencies': []}, 1),
    (0, [], 1),
])
def test_audit_exit_status_and_complete_result_are_both_required(monkeypatch, status, report, expected):
    monkeypatch.setattr(gate, 'load_accepted', lambda: {})
    monkeypatch.setattr(gate.subprocess, 'run', lambda *args, **kwargs:
                        CompletedProcess([], status, json.dumps(report), ''))
    assert gate.main() == expected


def test_expired_acceptances_are_removed():
    assert gate.load_accepted() == {}
