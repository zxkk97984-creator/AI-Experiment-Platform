"""Fail the opt-in runtime gate if tests were absent, failed, or skipped."""
import sys
import xml.etree.ElementTree as ET


def check(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases)
    if not cases or failed:
        raise ValueError(f'Runtime gate invalid: executed={len(cases)}, failed/error/skipped={failed}')
    print(f'Runtime gate passed: {len(cases)} executed, zero failures/errors/skips')


if __name__ == '__main__':
    check(sys.argv[1])
