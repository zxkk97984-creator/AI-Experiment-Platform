"""data_v1 判题编排（批次三 3E）：平台侧用例规划、帧比较与计分。

信任边界（与 3A 一致）：

* 容器只收到**入口函数与逐例输入**；期望值、比较规则、分值、权重全部留在平台侧；
* 容器回传的帧一律不可信：先过 ``parse_case_frames`` 校验，再逐例 ``compare_case``；
* 平台错误（协议破坏/非法值/超限/配置问题）→ 该组不计分，整体转人工复核，
  **绝不把平台问题算成学生 0 分**。

计分：组内每个用例等权，组得分 = ``max_score × 通过用例数 / 用例总数``；
F 组合计为功能分，R 组合计为边界分（与"功能 60 ＋ 边界 10"一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.grading_compare import PASSED, PLATFORM_ERROR, compare_case, summarize_group
from app.services.grading_runner import (
    build_case_runner_source,
    build_cases_payload,
    parse_case_frames,
    to_actual_value,
)
from app.services.grading_values import encode_value

RUNNER_FILENAME = "dai_case_runner.py"
CASES_FILENAME = "dai_cases.json"


class DataGradingConfigError(ValueError):
    """评分版本配置不合法（平台侧问题，转人工，不计 0 分）。"""


def plan_cases(version) -> tuple[dict, list[dict]]:
    """返回 ``(容器输入, 平台侧期望表)``。

    容器输入**不含** expected/分值/权重（``build_cases_payload`` 会显式拒绝）。
    """
    payload = version.payload or {}
    groups = payload.get("groups") or []
    binary_cases = payload.get("binary_cases") or []
    mode = getattr(version, "grading_mode_snapshot", "active") or "active"
    if not isinstance(groups, list) or not isinstance(binary_cases, list):
        raise DataGradingConfigError("groups 和 binary_cases 必须是数组")
    if not groups and not binary_cases:
        raise DataGradingConfigError("评分版本缺少用例组或 binary_cases")
    if mode == "legacy" and not binary_cases:
        raise DataGradingConfigError("legacy 评分版本缺少 binary_cases")
    if mode == "shadow" and not binary_cases:
        raise DataGradingConfigError("shadow 评分版本缺少 binary_cases")
    if mode in ("active", "shadow") and not groups:
        raise DataGradingConfigError(f"{mode} 评分版本缺少 F/R 用例组")
    container_cases: list[dict] = []
    expectations: list[dict] = []
    if mode in ("legacy", "shadow"):
        for case in binary_cases:
            case_id = str(case.get("id"))
            container_cases.append({"id": case_id, "input": case.get("input")})
            expectations.append({
                "id": case_id, "group_id": "@binary", "dimension": "F",
                "max_score": 100.0, "group_case_count": len(binary_cases),
                "expected": case.get("expected"),
                "compare": case.get("compare") or {"kind": "literal"},
                "binary": True,
            })
    for group in groups:
        group_id = group.get("id")
        dimension = group.get("dimension")
        max_score = group.get("max_score")
        if dimension not in ("F", "R"):
            raise DataGradingConfigError(f"用例组 {group_id} 的 dimension 非法")
        if not isinstance(max_score, (int, float)) or isinstance(max_score, bool) or max_score <= 0:
            raise DataGradingConfigError(f"用例组 {group_id} 的 max_score 非法")
        cases = group.get("cases")
        if not isinstance(cases, list) or not cases:
            raise DataGradingConfigError(f"用例组 {group_id} 没有用例")
        for case in cases:
            case_id = str(case.get("id"))
            container_cases.append({"id": case_id, "input": case.get("input")})
            expectations.append({
                "id": case_id,
                "group_id": group_id,
                "dimension": dimension,
                "max_score": float(max_score),
                "group_case_count": len(cases),
                "expected": case.get("expected"),
                "compare": case.get("compare") or {"kind": "literal"},
            })
    if not container_cases:
        raise DataGradingConfigError("评分版本没有任何用例")
    container_payload = build_cases_payload(version.entry_function, container_cases)
    return container_payload, expectations


def write_runner_files(workdir: Path, *, entry_function: str, container_payload: dict,
                       student_code: str) -> None:
    """写入容器所需文件：runner 源码、用例输入（仅输入）、学生代码。"""
    (workdir / RUNNER_FILENAME).write_text(build_case_runner_source(), encoding="utf-8")
    # 全部用例清单只保存在宿主工作目录；容器启动时由 Worker 为单例复制子集。
    (workdir / CASES_FILENAME).write_text(
        json.dumps({"entry_function": container_payload.get("entry_function", ""),
                    "cases": container_payload.get("cases", [])},
                   ensure_ascii=False), encoding="utf-8")
    (workdir / "user_code.py").write_text(student_code, encoding="utf-8")


def grade_output(protocol_text: str, expectations: list[dict], *, entry_function: str = "",
                 diagnostic_text: str = "") -> dict:
    """比较隔离容器的实际结果帧与宿主期望表，返回逐组得分和平台错误。

    ``protocol_text`` 是 stderr 协议帧；``diagnostic_text`` 是 stdout 学生输出，仅留档。
    """
    expected_ids = [item["id"] for item in expectations]
    frames, frame_errors = parse_case_frames(protocol_text, expected_ids)
    platform_errors = list(frame_errors)

    case_details: list[dict] = []
    groups: dict[str, dict] = {}
    for expectation in expectations:
        group_id = expectation["group_id"]
        bucket = groups.setdefault(group_id, {
            "id": group_id, "dimension": expectation["dimension"],
            "max_score": expectation["max_score"], "passed": 0, "failed": 0,
            "platform_errors": [], "cases": [], "binary": expectation.get("binary", False),
        })
        frame = frames.get(expectation["id"])
        if frame is None:
            # 缺帧已在 frame_errors 中；该用例计为平台错误，不参与计分
            bucket["platform_errors"].append(f"缺少用例结果 {expectation['id']}")
            bucket["cases"].append({"id": expectation["id"], "outcome": PLATFORM_ERROR})
            continue
        actual = to_actual_value(frame)
        outcome = compare_case(actual, expectation["expected"], expectation["compare"])
        bucket["cases"].append({"id": expectation["id"], "outcome": outcome["outcome"],
                                "reason": outcome.get("reason")})
        if outcome["outcome"] == PASSED:
            bucket["passed"] += 1
        elif outcome["outcome"] == PLATFORM_ERROR:
            bucket["platform_errors"].append(
                f"{expectation['id']}: {outcome.get('reason') or '结果不可验证'}")
        else:
            bucket["failed"] += 1
        case_details.append({"id": expectation["id"], "group_id": group_id,
                             "outcome": outcome["outcome"]})

    scored_groups = []
    f_score = r_score = 0.0
    total_passed = total_cases = 0
    for group_id, bucket in groups.items():
        total = bucket["passed"] + bucket["failed"] + len(bucket["platform_errors"])
        total_passed += bucket["passed"]
        total_cases += total
        summary = summarize_group([{"outcome": case["outcome"]} for case in bucket["cases"]])
        earned = 0.0
        if summary["scored"] and total:
            if bucket.get("binary"):
                earned = 100.0 if bucket["passed"] == total else 0.0
            else:
                earned = round(bucket["max_score"] * bucket["passed"] / total, 4)
            if bucket["dimension"] == "F":
                f_score += earned
            else:
                r_score += earned
        scored_groups.append({**bucket, "earned": earned, "scored": summary["scored"],
                              "case_total": total})
        platform_errors.extend(bucket["platform_errors"])

    return {
        "groups": scored_groups,
        "case_details": case_details,
        "f_score": None if platform_errors else round(f_score, 4),
        "r_score": None if platform_errors else round(r_score, 4),
        "binary_score": (None if platform_errors else next(
            (group["earned"] for group in scored_groups if group.get("binary")), None)),
        "tests_passed": total_passed,
        "tests_total": total_cases,
        "platform_errors": platform_errors,
        "needs_review": bool(platform_errors),
    }


def single_case_preview(version, case_id: str, value) -> dict:
    """给编辑器/自测用的单例比较（不落库）。"""
    payload = version.payload or {}
    for group in payload.get("groups") or []:
        for case in group.get("cases") or []:
            if str(case.get("id")) == str(case_id):
                return compare_case(encode_value(value), case.get("expected"),
                                    case.get("compare") or {"kind": "literal"})
    raise DataGradingConfigError(f"用例不存在: {case_id}")
