"""Kernel 执行结果的整理与归类（API 与 worker 共用）。

- ModuleNotFoundError 兜底归类：restricted 环境下绕过静态预检的动态 import，
  不把裸 traceback 当策略错误返回（计划 9.4）。
- 可见 cell 的 execution_count 递增规则（hidden cell 不计入）。
"""
import re

from app.schemas.environments import ImportDiagnosticRead


def classify_kernel_module_not_found(outputs: list[dict], policy, installed: set[str]):
    """Kernel 输出中的 ModuleNotFoundError 兜底归类。"""
    if not policy.restricted:
        return None
    for out in outputs:
        if out.get("msg_type") != "error":
            continue
        text = (out.get("content") or {}).get("text", "") or ""
        match = re.search(r"No module named '([^']+)'", text)
        if not match:
            continue
        module = match.group(1).split(".")[0]
        if module not in policy.allowed_imports:
            return ImportDiagnosticRead(
                code="IMPORT_NOT_ALLOWED",
                module=module,
                message=f"导入限制：{module} 未在本实验允许范围内",
            )
        if module not in installed:
            return ImportDiagnosticRead(
                code="IMPORT_NOT_INSTALLED",
                module=module,
                message=f"环境配置错误：本实验允许 {module}，但当前环境未安装",
            )
    return None


def replace_error_output(outputs: list[dict], message: str) -> list[dict]:
    """把第一个 error 输出替换为无 traceback 的合成中文诊断输出。"""
    replaced = False
    result = []
    for out in outputs:
        if not replaced and out.get("msg_type") == "error":
            result.append({"msg_type": "error", "content": {"text": message}})
            replaced = True
        else:
            result.append(out)
    if not replaced:
        result.append({"msg_type": "error", "content": {"text": message}})
    return result


def next_visible_execution_count(record, version) -> int:
    """可见 cell 的最大 execution_count + 1（hidden cell 的计数不暴露给学生）。"""
    if not version:
        return 1
    hidden_ids = {c["id"] for c in version.cells if c.get("source_hidden")}
    max_count = 0
    for cid, output in (record.cells_outputs or {}).items():
        if cid not in hidden_ids and isinstance(output, dict):
            count = output.get("execution_count", 0)
            if count > max_count:
                max_count = count
    return max_count + 1
