"""可信比较（批次三 3A）：平台侧对"实际值 vs 期望值"的判定。

信任边界：容器只回传学生代码的**实际值**（或异常标识），从不接触期望值、分值、
通过计数与权重；比较与计分全部在这里完成，stdout 不参与计分。

三种结果（必须区分，不能把平台问题算成学生错误）：

* ``passed`` / ``failed``：学生代码的确定性结论；
* ``platform_error``：结果协议破坏、非法值、超限、不支持的配置——**不计 0 分**，
  交由人工/有界重试处理。

比较规则（版本化保存在评分版本里）：

.. code-block:: text

    {"kind": "literal"}                                    # Python 字面量相等语义
    {"kind": "identity"}                                   # is True / is False / is None
    {"kind": "float", "abs_tol": 1e-9, "rel_tol": 1e-9}
    {"kind": "array", "shape": "exact", "abs_tol": 1e-9, "rel_tol": 1e-9}
    {"kind": "exception", "types": ["ValueError"]}         # 期望抛出指定内置异常
"""

from __future__ import annotations

import math
from typing import Any

from app.services.grading_values import (
    ALLOWED_EXCEPTION_TYPES,
    ValueEncodingError,
    decode_value,
    dtype_kind,
    is_array,
)

PASSED = "passed"
FAILED = "failed"
PLATFORM_ERROR = "platform_error"

DEFAULT_ABS_TOL = 1e-9
DEFAULT_REL_TOL = 1e-9
MAX_TOLERANCE = 1.0


class ComparisonConfigError(ValueError):
    """评分版本里的比较规则不合法（配置问题，属平台侧）。"""


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def close(left: float, right: float, *, abs_tol: float, rel_tol: float) -> bool:
    """浮点容差比较：|a-b| <= abs_tol + rel_tol*|b|（b 为期望值）。"""
    if math.isnan(left) or math.isnan(right):
        return False
    return abs(left - right) <= abs_tol + rel_tol * abs(right)


def _tolerances(rule: dict) -> tuple[float, float]:
    abs_tol = float(rule.get("abs_tol", DEFAULT_ABS_TOL))
    rel_tol = float(rule.get("rel_tol", DEFAULT_REL_TOL))
    for name, value in (("abs_tol", abs_tol), ("rel_tol", rel_tol)):
        if not math.isfinite(value) or value < 0 or value > MAX_TOLERANCE:
            raise ComparisonConfigError(f"{name} 必须在 0~{MAX_TOLERANCE} 之间")
    return abs_tol, rel_tol


def compare_case(actual: Any, expected: Any, rule: dict) -> dict:
    """比较单个用例；返回 ``{"outcome": ..., "reason": ...}``。"""
    if not isinstance(rule, dict):
        return {"outcome": PLATFORM_ERROR, "reason": "比较规则缺失"}
    kind = rule.get("kind")
    try:
        expected_value = decode_value(expected)
    except ValueEncodingError as exc:
        return {"outcome": PLATFORM_ERROR, "reason": f"期望值不符合值协议: {exc}"}

    if kind == "exception":
        return _compare_exception(actual, rule)

    if not isinstance(actual, dict) or actual.get("t") is None:
        return {"outcome": PLATFORM_ERROR, "reason": "实际值不是协议对象"}
    if actual.get("t") == "invalid_request":
        # 用例输入/配置不合法：平台侧问题，绝不计 0 分
        return {"outcome": PLATFORM_ERROR,
                "reason": f"用例请求不合法: {actual.get('reason') or '未说明'}"}
    if actual.get("t") == "exception":
        return {"outcome": FAILED, "reason": f"未预期异常: {actual.get('type')}"}

    try:
        actual_value = decode_value(actual)
    except ValueEncodingError as exc:
        return {"outcome": PLATFORM_ERROR, "reason": f"实际值不符合值协议: {exc}"}

    if kind == "literal":
        return {"outcome": PASSED if actual_value == expected_value else FAILED,
                "reason": None if actual_value == expected_value else "字面量不相等"}
    if kind == "identity":
        # is True / is False / is None：与 == 区分（布尔与整数不混用）
        same = actual_value is expected_value
        return {"outcome": PASSED if same else FAILED,
                "reason": None if same else "身份比较不相等"}
    if kind == "float":
        try:
            abs_tol, rel_tol = _tolerances(rule)
        except ComparisonConfigError as exc:
            return {"outcome": PLATFORM_ERROR, "reason": str(exc)}
        if not _numeric(actual_value) or not _numeric(expected_value):
            return {"outcome": PLATFORM_ERROR, "reason": "float 规则要求两侧都是数值"}
        ok = close(float(actual_value), float(expected_value), abs_tol=abs_tol, rel_tol=rel_tol)
        return {"outcome": PASSED if ok else FAILED,
                "reason": None if ok else "浮点超出容差"}
    if kind == "array":
        return _compare_array(actual_value, expected_value, rule)
    return {"outcome": PLATFORM_ERROR, "reason": f"不支持的比较规则: {kind!r}"}


def _compare_exception(actual: Any, rule: dict) -> dict:
    allowed = rule.get("types")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(item, str) for item in allowed):
        return {"outcome": PLATFORM_ERROR, "reason": "exception 规则必须给出非空 types 列表"}
    unknown = [item for item in allowed if item not in ALLOWED_EXCEPTION_TYPES]
    if unknown:
        return {"outcome": PLATFORM_ERROR, "reason": f"不支持的异常类型: {unknown}"}
    if not isinstance(actual, dict):
        return {"outcome": PLATFORM_ERROR, "reason": "实际值不是协议对象"}
    if actual.get("t") != "exception":
        return {"outcome": FAILED, "reason": "期望抛出异常但实际返回了值"}
    actual_type = actual.get("type")
    if actual_type not in ALLOWED_EXCEPTION_TYPES:
        # 自定义异常/未列入名单：不按通过处理，也不计 0 分
        return {"outcome": PLATFORM_ERROR, "reason": f"未列入名单的异常类型: {actual_type!r}"}
    if actual_type in allowed:
        return {"outcome": PASSED, "reason": None}
    return {"outcome": FAILED, "reason": f"异常类型不符: {actual_type} 不在 {allowed}"}


def _compare_array(actual: Any, expected: Any, rule: dict) -> dict:
    if not is_array(actual) or not is_array(expected):
        return {"outcome": PLATFORM_ERROR, "reason": "array 规则要求两侧都是数组"}
    shape_rule = rule.get("shape", "exact")
    if shape_rule not in ("exact", "scalar_broadcast"):
        return {"outcome": PLATFORM_ERROR, "reason": f"不支持的形状规则: {shape_rule!r}"}
    try:
        abs_tol, rel_tol = _tolerances(rule)
    except ComparisonConfigError as exc:
        return {"outcome": PLATFORM_ERROR, "reason": str(exc)}

    if shape_rule == "exact" and list(actual["shape"]) != list(expected["shape"]):
        return {"outcome": FAILED, "reason": f"形状不符: {actual['shape']} != {expected['shape']}"}
    actual_kind = dtype_kind(actual["dtype"])
    expected_kind = dtype_kind(expected["dtype"])
    if actual_kind == "bool" or expected_kind == "bool":
        if actual_kind != expected_kind:
            return {"outcome": PLATFORM_ERROR, "reason": "布尔数组与数值数组不可比"}
        same = list(actual["data"]) == list(expected["data"])
        return {"outcome": PASSED if same else FAILED,
                "reason": None if same else "布尔数组元素不一致"}

    a_data, e_data = list(actual["data"]), list(expected["data"])
    if len(a_data) != len(e_data):
        return {"outcome": FAILED, "reason": "元素个数不符"}
    for index, (left, right) in enumerate(zip(a_data, e_data)):
        if not _numeric(left) or not _numeric(right):
            return {"outcome": PLATFORM_ERROR, "reason": f"数组元素 {index} 不是数值"}
        if actual_kind == "float" or expected_kind == "float":
            if not close(float(left), float(right), abs_tol=abs_tol, rel_tol=rel_tol):
                return {"outcome": FAILED, "reason": f"元素 {index} 超出容差"}
        elif left != right:
            return {"outcome": FAILED, "reason": f"元素 {index} 不相等"}
    return {"outcome": PASSED, "reason": None}


def summarize_group(outcomes: list[dict]) -> dict:
    """按组汇总：passed/failed 计数 + 平台错误列表（组内出现平台错误即不产生正式分）。"""
    passed = sum(1 for item in outcomes if item.get("outcome") == PASSED)
    failed = sum(1 for item in outcomes if item.get("outcome") == FAILED)
    platform = [item.get("reason") or "结果不可验证"
                for item in outcomes if item.get("outcome") == PLATFORM_ERROR]
    return {"passed": passed, "failed": failed, "platform_errors": platform,
            "scored": not platform}
