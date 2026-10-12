"""pytest → data_v1 转换预检（批次三 3C）。

把现有题目里的 ``hidden_tests`` pytest 源码**静态**分析成数据用例草稿：

* 只做 AST 解析，**绝不执行**题目代码、不导入被测模块、不调用任何函数；
* 只接受可证明等价的形态（见下），其余逐条给出原因（unsupported），
  由教师决定人工改写还是保留 pytest 判题；
* 输出是**草稿**（``source_kind="converted_from_pytest"``），必须经确认才能启用。

可转换形态（保守白名单）：

.. code-block:: text

    assert f(1, 2) == 3                  # 入口函数 + 字面量实参 + 字面量期望
    assert f([1, 2]) == [1, 2, 3]
    assert f(1) is None                  # is None / is True / is False → identity 规则
    with pytest.raises(ValueError):      # 单一调用抛指定内置异常
        f(-1)

其余（控制流、赋值、fixture、辅助函数、非字面量实参、多语句块、外部导入等）一律标记
``unsupported`` 并说明原因——宁可少转，不可错转。
"""

from __future__ import annotations

import ast
from typing import Any

from app.services.grading_values import (
    MAX_CASES_PER_GROUP,
    MAX_STRING_BYTES,
    ValueEncodingError,
    encode_value,
)

MAX_SOURCE_BYTES = 64 * 1024
SUPPORTED_COMPARE_KINDS = ("literal", "identity", "exception")

_UNSUPPORTED_STATEMENTS = (
    ast.For, ast.While, ast.If, ast.Try, ast.With, ast.AsyncFor, ast.AsyncWith,
    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete, ast.Import, ast.ImportFrom,
    ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal, ast.Raise,
)
_UNSUPPORTED_STATEMENT_NAMES = {node.__name__ for node in _UNSUPPORTED_STATEMENTS}

ALLOWED_EXCEPTION_NAMES = {
    "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
    "ArithmeticError", "OverflowError", "StopIteration", "AssertionError",
    "RecursionError", "NotImplementedError", "AttributeError",
}


def _literal(node: ast.AST) -> Any:
    """只接受字面量（含负数字面量与容器字面量）；其余抛 ValueError。"""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal(node.operand)
        if isinstance(inner, bool) or not isinstance(inner, (int, float)):
            raise ValueError("一元负号只能用于数值字面量")
        return -inner
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError) as exc:
        raise ValueError("实参/期望值必须是字面量（不能引用变量、调用或运算）") from exc


def _case_id(test_name: str, index: int) -> str:
    suffix = test_name.removeprefix("test_") or "case"
    safe = "".join(ch if ch.isalnum() else "_" for ch in suffix)[:24].upper() or "CASE"
    return f"{safe}-{index}"


def _call_case(node: ast.Call, entry_function: str, case_id: str) -> dict:
    if not isinstance(node.func, ast.Name):
        raise ValueError("只支持直接调用入口函数（不支持属性/下标调用）")
    if node.func.id != entry_function:
        raise ValueError(f"调用了非入口函数 {node.func.id}()")
    if node.keywords:
        raise ValueError("暂不支持关键字实参")
    args = [_literal(arg) for arg in node.args]
    return {"case_id": case_id, "input": encode_value(tuple(args))}


def _analyze_assert(statement: ast.Assert, entry_function: str, case_id: str) -> dict:
    test = statement.test
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        left, right = test.left, test.comparators[0]
        if isinstance(op, ast.Eq):
            if not isinstance(left, ast.Call):
                raise ValueError("等值断言左侧必须是入口函数调用")
            case = _call_case(left, entry_function, case_id)
            case["expected"] = encode_value(_literal(right))
            case["compare"] = {"kind": "literal"}
            return case
        if isinstance(op, ast.Is) and isinstance(right, ast.Constant) and right.value in (None, True, False):
            if not isinstance(left, ast.Call):
                raise ValueError("is 断言左侧必须是入口函数调用")
            case = _call_case(left, entry_function, case_id)
            case["expected"] = encode_value(right.value)
            case["compare"] = {"kind": "identity"}
            return case
        raise ValueError(f"暂不支持的比较运算符 {type(op).__name__}")
    raise ValueError("只支持 'assert f(...) == 字面量'、'assert f(...) is None/True/False'")


def _analyze_raises(statement: ast.With, entry_function: str, case_id: str) -> dict:
    if len(statement.items) != 1:
        raise ValueError("pytest.raises 只支持单个上下文项")
    item = statement.items[0]
    context = item.context_expr
    if not (isinstance(context, ast.Call) and isinstance(context.func, ast.Attribute)
            and context.func.attr == "raises"):
        raise ValueError("只支持 with pytest.raises(...) 形态")
    if item.optional_vars is not None:
        raise ValueError("pytest.raises 不支持 as 绑定")
    if len(context.args) != 1:
        raise ValueError("pytest.raises 必须且只能指定一个异常类型")
    exception_node = context.args[0]
    if not isinstance(exception_node, ast.Name) or exception_node.id not in ALLOWED_EXCEPTION_NAMES:
        raise ValueError("异常类型必须是受支持的内置异常名")
    body = [stmt for stmt in statement.body if not isinstance(stmt, ast.Pass)]
    if len(body) != 1 or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
        raise ValueError("pytest.raises 代码块必须只包含一次入口函数调用")
    case = _call_case(body[0].value, entry_function, case_id)
    case["expected"] = encode_value(None)
    case["compare"] = {"kind": "exception", "types": [exception_node.id]}
    return case


def analyze_pytest_source(source: str, *, entry_function: str) -> dict:
    """静态分析 pytest 源码，返回逐条预检报告（不执行任何代码）。"""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("源码为空")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError(f"源码超过 {MAX_SOURCE_BYTES} 字节")
    if not isinstance(entry_function, str) or not entry_function.isidentifier():
        raise ValueError("entry_function 必须是合法标识符")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"源码语法错误: {exc.msg}") from exc

    cases: list[dict] = []
    functions: list[dict] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) or isinstance(node, ast.Import):
            continue  # 导入语句不影响用例提取（我们从不执行）
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        function_report = {"name": node.name, "convertible": 0, "unsupported": 0}
        blocked = [stmt for stmt in node.body if type(stmt).__name__ in _UNSUPPORTED_STATEMENT_NAMES]
        for index, statement in enumerate(node.body, start=1):
            if len(cases) >= MAX_CASES_PER_GROUP:
                break
            case_id = _case_id(node.name, index)
            if isinstance(statement, ast.Assert):
                try:
                    case = _analyze_assert(statement, entry_function, case_id)
                except (ValueError, ValueEncodingError) as exc:
                    cases.append({"case_id": case_id, "test": node.name, "status": "unsupported",
                                  "reason": str(exc)})
                    function_report["unsupported"] += 1
                    continue
                cases.append({"case_id": case_id, "test": node.name, "status": "convertible", **case})
                function_report["convertible"] += 1
                continue
            if isinstance(statement, ast.With):
                try:
                    case = _analyze_raises(statement, entry_function, case_id)
                except (ValueError, ValueEncodingError) as exc:
                    cases.append({"case_id": case_id, "test": node.name, "status": "unsupported",
                                  "reason": str(exc)})
                    function_report["unsupported"] += 1
                    continue
                cases.append({"case_id": case_id, "test": node.name, "status": "convertible", **case})
                function_report["convertible"] += 1
                continue
            reason = ("包含控制流/赋值/导入等语句，无法静态证明等价"
                      if type(statement).__name__ in _UNSUPPORTED_STATEMENT_NAMES
                      else f"不支持的语句类型 {type(statement).__name__}")
            cases.append({"case_id": case_id, "test": node.name, "status": "unsupported",
                          "reason": reason})
            function_report["unsupported"] += 1
        if blocked:
            function_report["blocked_by"] = sorted({type(stmt).__name__ for stmt in blocked})
        functions.append(function_report)

    convertible = [case for case in cases if case["status"] == "convertible"]
    unsupported = [case for case in cases if case["status"] == "unsupported"]
    return {
        "schema_version": 1,
        "entry_function": entry_function,
        "cases": cases,
        "functions": functions,
        "convertible_count": len(convertible),
        "unsupported_count": len(unsupported),
        "fully_convertible": bool(convertible) and not unsupported,
        "notes": _notes(functions, convertible, unsupported),
    }


def _notes(functions: list[dict], convertible: list[dict], unsupported: list[dict]) -> list[str]:
    notes = []
    if not convertible:
        notes.append("没有可转换用例：请人工改写为数据用例，或继续使用 pytest 判题")
    if unsupported:
        notes.append("存在不可静态证明的用例，转换后需人工补齐；未确认前不会生效")
    if any(function.get("blocked_by") for function in functions):
        notes.append("部分测试函数包含控制流/赋值，整体无法自动转换")
    return notes


def build_conversion_payload(report: dict, *, entry_function: str, group_id: str = "F1",
                             group_name: str = "功能用例", max_score: float = 60.0,
                             dimension: str = "F") -> dict:
    """把预检报告转成评分版本 payload（只包含 convertible 用例）。"""
    if report.get("entry_function") != entry_function:
        raise ValueError("报告与入口函数不一致")
    cases = [case for case in report.get("cases", []) if case.get("status") == "convertible"]
    if not cases:
        raise ValueError("没有可转换用例")
    if len(cases) > MAX_CASES_PER_GROUP:
        raise ValueError(f"用例数超过 {MAX_CASES_PER_GROUP}")
    if dimension not in ("F", "R"):
        raise ValueError("dimension 必须是 F 或 R")
    return {
        "schema_version": 2,
        "entry_function": entry_function,
        "execution_model": "isolated_case_v2",
        "binary_cases": [],
        "groups": [{
            "id": group_id,
            "name": group_name,
            "dimension": dimension,
            "max_score": float(max_score),
            "cases": [{"id": f"{group_id}:{case['case_id']}", "input": case["input"],
                       "expected": case["expected"], "compare": case["compare"]}
                      for case in cases],
        }],
    }


def conversion_summary(report: dict) -> dict:
    """给教师看的简明摘要（不含源码片段）。"""
    return {
        "convertible_count": report.get("convertible_count", 0),
        "unsupported_count": report.get("unsupported_count", 0),
        "fully_convertible": report.get("fully_convertible", False),
        "reasons": sorted({case.get("reason") for case in report.get("cases", [])
                           if case.get("status") == "unsupported" and case.get("reason")}),
        "notes": report.get("notes", []),
    }
