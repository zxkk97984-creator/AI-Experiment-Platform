"""只读静态分析——AST、Ruff、Radon"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


# 学生代码质量分析使用的固定规则集（与仓库高信号配置解耦）
RUFF_SELECT = "F401,F811,F841,E711,E712,E713,E714,E721,E722,E902"

# Radon 复杂度输出允许的复杂度等级
_RADON_RANKS = {"A", "B", "C", "D", "E", "F"}


def _valid_ruff_diagnostics(payload) -> bool:
    """校验 ruff --output-format json 的结构：[{code, message, location}, ...]。"""
    if not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("code"), str) or not item.get("code"):
            return False
        if not isinstance(item.get("message"), str):
            return False
        if not isinstance(item.get("location"), dict):
            return False
    return True


def _valid_radon_output(payload) -> bool:
    """校验 radon cc -j 的结构：{文件名: [ {complexity, rank, ...}, ... ]}。"""
    if not isinstance(payload, dict):
        return False
    for blocks in payload.values():
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            if not isinstance(block, dict):
                return False
            complexity = block.get("complexity")
            rank = block.get("rank")
            if not isinstance(complexity, int) or isinstance(complexity, bool):
                return False
            if complexity < 1:
                return False
            if rank is not None and rank not in _RADON_RANKS:
                return False
    return True


def analyze_python(code: str) -> dict:
    """对 Python 代码进行只读静态分析——不执行代码"""
    lines = code.splitlines()
    line_count = len(lines)

    result = {
        "parseable": True,
        "syntax_error": None,
        "line_count": line_count,
        "diagnostics": [],
        "complexity": {},
        # 每个分析器的显式状态：ok / unavailable / timeout / invalid_result / error / not_run
        # 调用方据此区分"分析成功"与"平台工具故障"，绝不把缺失当作"没有问题"。
        "tool_status": {"ruff": "not_run", "radon": "not_run"},
        # 总体：ok（全部必需分析器成功）/ degraded（部分不可用）/ unavailable（全部不可用）
        "status": "ok",
    }

    # AST 解析
    try:
        tree = ast.parse(code)
        # 基本统计
        func_count = sum(1 for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        max_nesting = _max_nesting(tree)
        result["function_count"] = func_count
        result["max_nesting"] = max_nesting
    except SyntaxError as exc:
        result["parseable"] = False
        result["syntax_error"] = str(exc)
        return result

    # Ruff 诊断（子进程，5 秒超时）
    try:
        # A16：--isolated 忽略仓库 pyproject/ruff 配置（否则学生代码会继承平台的高信号
        # 规则集，F401 之类的质量问题在 backend cwd 下反而得到空诊断）；
        # 规则集显式固定，保证分析口径与部署位置无关。
        ruff_result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache",
             "--select", RUFF_SELECT, "--output-format", "json", "-"],
            input=code,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ruff_result.returncode in (0, 1):  # 0=clean, 1=issues found
            try:
                diagnostics = json_loads_limited(ruff_result.stdout, max_items=100)
                # A16：结构校验——必须是诊断对象列表（含 code/message/location），
                # 合法但与语义无关的 JSON（如 [1,2,3]）不得记成 ok。
                if not _valid_ruff_diagnostics(diagnostics):
                    raise TypeError("ruff 输出不是诊断列表")
                result["diagnostics"] = diagnostics
                result["tool_status"]["ruff"] = "ok"
            except (ValueError, TypeError):
                result["tool_status"]["ruff"] = "invalid_result"
        else:
            result["tool_status"]["ruff"] = "error"
    except subprocess.TimeoutExpired:
        result["tool_status"]["ruff"] = "timeout"
    except (FileNotFoundError, OSError):
        result["tool_status"]["ruff"] = "unavailable"

    # Radon 圈复杂度
    try:
        radon_result = subprocess.run(
            [sys.executable, "-m", "radon", "cc", "-j", "-"],
            input=code,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if radon_result.returncode == 0 and radon_result.stdout.strip():
            try:
                complexity = json_loads_limited(radon_result.stdout, max_items=50)
                # A16：结构校验——必须是"文件名 → 块列表"的映射，且每块含合法复杂度字段；
                # 任意外形正确但语义无关的 JSON 不得记成 ok。
                if not _valid_radon_output(complexity):
                    raise TypeError("radon 输出结构不合法")
                result["complexity"] = complexity
                result["tool_status"]["radon"] = "ok"
            except (ValueError, TypeError):
                result["tool_status"]["radon"] = "invalid_result"
        else:
            result["tool_status"]["radon"] = "error"
    except subprocess.TimeoutExpired:
        result["tool_status"]["radon"] = "timeout"
    except (FileNotFoundError, OSError):
        result["tool_status"]["radon"] = "unavailable"

    states = set(result["tool_status"].values())
    if states == {"ok"}:
        result["status"] = "ok"
    elif "ok" in states:
        result["status"] = "degraded"
    else:
        result["status"] = "unavailable"
    return result


def _max_nesting(tree: ast.AST) -> int:
    """计算 AST 最大嵌套深度"""
    max_depth = 0

    def walk(node, depth=0):
        nonlocal max_depth
        max_depth = max(max_depth, depth)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.For, ast.While, ast.If, ast.With, ast.Try, ast.ExceptHandler)):
                walk(child, depth + 1)
            else:
                walk(child, depth)

    walk(tree)
    return max_depth


def json_loads_limited(text: str, max_items: int = 100) -> list | dict:
    """受限 JSON 解析——截断超长结果"""
    import json
    data = json.loads(text)
    if isinstance(data, list) and len(data) > max_items:
        return data[:max_items]
    return data
