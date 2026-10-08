import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize('body,valid', [
    ('', False), ('<testcase/>', True), ('<testcase><skipped/></testcase>', False),
    ('<testcase><failure/></testcase>', False), ('<testcase><error/></testcase>', False),
])
def test_runtime_gate_rejects_missing_failed_and_skipped_tests(tmp_path, body, valid):
    spec = importlib.util.spec_from_file_location('runtime_gate', Path(__file__).parents[3] / 'scripts/check_runtime_junit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / 'junit.xml'
    report.write_text(f'<testsuites><testsuite>{body}</testsuite></testsuites>')
    if valid:
        module.check(report)
    else:
        with pytest.raises(ValueError):
            module.check(report)


def test_service_queue_factory_uses_same_isolated_redis(redis_client, monkeypatch):
    from app.services import judge_queue
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must not connect to host Redis')
    monkeypatch.setattr(judge_queue._redis.Redis, 'from_url', forbidden)
    assert judge_queue._get_redis() is redis_client
    judge_queue._get_redis().rpush('judge:queue', 'synthetic')
    assert redis_client.lpop('judge:queue') == 'synthetic'
