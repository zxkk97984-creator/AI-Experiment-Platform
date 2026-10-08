"""Bounded student observations are compared with private, versioned rules.

No student-provided pass/fail/score or measured time is accepted. Custom Python
is handled by the independent Docker checker, never executed in this module.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from app.services.lab_archive import LabDataError


def observation_requests(rules):
    return [{"id": rule["id"], "cell_id": rule["cell_id"], "variable": rule.get("variable", ""),
             "kind": rule["kind"]} for rule in rules if rule["kind"] in {"numeric_range", "numeric_close", "shape"}]


def read_observations(directory):
    path = Path(directory) / ".platform-cell-evidence.json"
    if not path.is_file() or path.stat().st_size > 1048576:
        raise LabDataError("未获得有效的 Cell 运行证据，请重新运行或联系教师")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise LabDataError("Cell 运行证据格式无效") from exc
    if not isinstance(raw, dict) or len(raw) > 200:
        raise LabDataError("Cell 运行证据超过限制")
    result = {}
    for cell_id, cell in raw.items():
        if (not isinstance(cell_id, str) or not isinstance(cell, dict)
                or type(cell.get("completed")) is not bool
                or not isinstance(cell.get("stdout", ""), str)
                or len(cell.get("stdout", "")) > 32768
                or not isinstance(cell.get("values", {}), dict)
                or len(cell.get("values", {})) > 30):
            raise LabDataError("Cell 运行证据无效")
        result[cell_id] = {"completed": cell["completed"], "stdout": cell.get("stdout", ""),
                           "values": cell.get("values", {})}
    return result


def template_marks(rules, observations):
    result = {}
    for rule in rules:
        if rule["kind"] == "python":
            continue
        observed = observations.get(rule["cell_id"], {})
        passed = False
        if observed.get("completed") is True:
            kind = rule["kind"]
            value = observed.get("values", {}).get(rule["id"], {})
            if kind == "stdout":
                actual = observed.get("stdout", "").replace("\r\n", "\n").rstrip("\n")
                expected = rule.get("expected_output", "").replace("\r\n", "\n").rstrip("\n")
                passed = expected in actual if rule.get("stdout_mode") == "contains" else expected == actual
            elif kind == "shape":
                shape = value.get("shape") if isinstance(value, dict) else None
                passed = (isinstance(shape, list) and all(type(v) is int for v in shape)
                          and shape == rule.get("shape"))
            else:
                number = value.get("value") if isinstance(value, dict) else None
                if type(number) in (int, float) and math.isfinite(number):
                    if kind == "numeric_range":
                        passed = rule["minimum"] <= number <= rule["maximum"]
                    elif kind == "numeric_close":
                        passed = math.isclose(number, rule["expected_value"], rel_tol=0,
                                              abs_tol=rule.get("tolerance", 1e-6))
        result[rule["id"]] = {"passed": bool(passed),
                              "message": "检查通过" if passed else "尚未满足本单元格要求，请检查代码与运行顺序"}
    return result
