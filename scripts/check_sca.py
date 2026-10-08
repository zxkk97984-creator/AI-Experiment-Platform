#!/usr/bin/env python3
"""TASK-026：Python SCA 门禁——pip-audit 结果与接受记录比对。

规则：
- 依赖（含传递依赖）存在漏洞且 (name, version) 不在 docs/security/sca-accepted.json
  的接受清单中 → 阻断，提示去 sca-acceptances.md 补接受记录（责任人+到期日）或修复。
- 已接受项不阻断；接受清单随到期复核更新，版本升级后若仍被审计报出须同步改清单。
- 用法：python scripts/check_sca.py [pip-audit 额外参数...]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "backend" / "requirements.txt"
ACCEPTED_FILE = REPO_ROOT / "docs" / "security" / "sca-accepted.json"
DOC = "docs/security/sca-acceptances.md"


def load_accepted() -> dict[str, list[str]]:
    with ACCEPTED_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return {name: list(versions) for name, versions in data["accepted"].items()}


def main() -> int:
    accepted = load_accepted()
    result = subprocess.run(
        [sys.executable, "-m", "pip_audit", "-r", str(REQUIREMENTS),
         "--format", "json", *sys.argv[1:]],
        capture_output=True, text=True,
    )
    # pip-audit 发现漏洞时返回 1 但 JSON 仍写 stdout；无 JSON 才视为工具故障
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode or 1

    if result.returncode not in (0, 1) or not isinstance(report, dict) or not report.get("dependencies"):
        print("SCA 审计未成功完成，不能放行发布。", file=sys.stderr)
        return 1

    offenders: list[str] = []
    for dep in report.get("dependencies", []):
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        name, version = dep["name"], dep["version"]
        if name in accepted and version in accepted[name]:
            print(f"[accepted] {name}=={version}（{len(vulns)} 项，见 {DOC}）")
        else:
            ids = sorted({v["id"] for v in vulns})
            offenders.append(f"{name}=={version} ({', '.join(ids)})")

    if offenders:
        print(
            f"SCA 门禁阻断：以下依赖存在未记录的漏洞（共 {len(offenders)} 项）。\n"
            f"修复可升级的版本，或按 {DOC} 的格式补充接受记录"
            f"（责任人+到期日+不可达性论证）并同步 {ACCEPTED_FILE.relative_to(REPO_ROOT)}。",
            file=sys.stderr,
        )
        for line in offenders:
            print(f"  - {line}", file=sys.stderr)
        return 1
    if result.returncode == 1 and not any(dep.get("vulns") for dep in report["dependencies"]):
        print("SCA 审计返回失败且没有完整漏洞结果，不能放行发布。", file=sys.stderr)
        return 1
    print(f"SCA 与接受记录一致（接受清单：{len(accepted)} 包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
