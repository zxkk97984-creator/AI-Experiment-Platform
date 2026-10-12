#!/usr/bin/env python3
"""Fail-closed high-signal scan of tracked source; never print matched contents."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

TARGETS = ('backend', 'frontend', 'scripts', 'docs', '.github')
EXTENSIONS = {'.py', '.yml', '.yaml', '.js', '.vue', '.md', '.sh', '.json', '.toml'}
PATTERN = re.compile(
    rb'(?:sk-[A-Za-z0-9]{20,}|oc_sk_[A-Za-z0-9_-]{20,})'
    rb'|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'
)


def scan(root):
    root = root.resolve(strict=True)
    if any(not (root / target).is_dir() for target in TARGETS):
        raise ValueError('A required scan directory is missing')
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--', *TARGETS],
                            check=True, capture_output=True)
    candidates = [Path(path.decode('utf-8')) for path in result.stdout.split(b'\0') if path]
    candidates = [path for path in candidates if path.suffix in EXTENSIONS]
    if not candidates:
        raise ValueError('No tracked source files found')
    found = []
    for relative in candidates:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError('A tracked source path escapes the repository')
        if PATTERN.search(path.read_bytes()):
            found.append(relative.as_posix())
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        matches = scan(args.root)
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Secret scan could not complete; refusing to pass.', file=sys.stderr)
        return 2
    if matches:
        for path in matches:
            print(f'Potential secret in tracked file: {path}')
        return 1
    print('Secret scan clean (tracked source files).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
