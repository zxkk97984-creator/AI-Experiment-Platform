from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/check_secrets.py'
TARGETS = ('backend', 'frontend', 'scripts', 'docs', '.github')


@pytest.fixture()
def repository(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    for directory in TARGETS:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'example.py').write_text('safe = True\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    return tmp_path


def scan(root, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root)],
                          cwd=cwd, capture_output=True, text=True)


def test_scan_from_backend_and_ignore_untracked_runtime(repository):
    (repository / 'backend/runtime.py').write_text('sk-' + 'A' * 30)
    assert scan(repository, repository / 'backend').returncode == 0


@pytest.mark.parametrize('directory', TARGETS)
def test_each_target_detects_without_printing_secret(repository, directory):
    secret = 'sk-' + 'A' * 30
    (repository / directory / 'example.py').write_text(secret)
    result = scan(repository)
    assert result.returncode == 1
    assert directory in result.stdout
    assert secret not in result.stdout + result.stderr


def test_opencode_key_detects_without_printing_secret(repository):
    secret = 'oc' + '_sk_' + 'A' * 30
    (repository / 'backend/example.py').write_text(secret)
    result = scan(repository)
    assert result.returncode == 1
    assert 'backend' in result.stdout
    assert secret not in result.stdout + result.stderr


def test_missing_directory_and_non_repository_fail_closed(repository, tmp_path):
    (repository / 'docs/example.py').unlink()
    (repository / 'docs').rmdir()
    assert scan(repository).returncode == 2
    assert scan(tmp_path / 'missing').returncode == 2


def test_missing_tracked_file_fails_closed(repository):
    (repository / 'backend/example.py').unlink()
    assert scan(repository).returncode == 2
