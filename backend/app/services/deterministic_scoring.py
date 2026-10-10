"""确定性 F/R 评分——测试组执行与计分"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.schemas.ai_grading import TestGroup

# 单个测试组的计分用例数上界——用于拒绝伪造的天文数字计数。
MAX_CASE_COUNT = 100_000

# pytest 结束时必定打印的汇总行（-q 模式：`2 passed in 0.02s` / `no tests ran in 0.01s`）。
# 它的作用是识别"进程在 pytest 汇报之前就退出"的形态（如学生代码 os._exit(0)）。
PYTEST_SUMMARY_RE = re.compile(
    r"(?:\d+\s+(?:passed|failed|error|errors|skipped|deselected|xfailed|xpassed)"
    r"|no tests ran)",
)

# 结果行必须是独占一行的 DAI_RESULT_JSON={...}——行锚定可避免把内嵌在 traceback
# 或失败报告回显里的文本当成结果，也便于做"恰好一条"的唯一性校验。
_RESULT_LINE_RE = re.compile(r"^DAI_RESULT_JSON=(\{.*\})\s*$", re.MULTILINE)


class DeterministicSystemError(RuntimeError):
    """测试组系统错误——不是学生错误"""

    def __init__(self, message: str):
        super().__init__(message)


@dataclass(frozen=True)
class DeterministicGrade:
    functional_score: float
    robustness_score: float
    groups: list[dict] = field(default_factory=list)
    system_errors: list[str] = field(default_factory=list)


def calculate_group_score(max_score: float, counts: dict[str, int]) -> float:
    """根据测试通过比例计算分组分数"""
    denominator = counts["passed"] + counts["failed"] + counts["errors"]
    if denominator <= 0:
        raise DeterministicSystemError("测试组没有可计分用例")
    return round(max_score * counts["passed"] / denominator, 4)


def parse_result_json(output: str) -> dict | None:
    """从 pytest 输出中提取唯一的 DAI_RESULT_JSON={...} 结果行。

    必须**恰好一条**：0 条说明没有可信结果，≥2 条说明存在伪造或重复，都无法判定归属。
    """
    if not output:
        return None
    matches = list(_RESULT_LINE_RE.finditer(output))
    if len(matches) != 1:
        return None
    try:
        data = json.loads(matches[0].group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # 校验计数为非负、有界的整数（bool 是 int 子类，需单独排除）
    for key in ("passed", "failed", "errors", "skipped"):
        val = data.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0 or val > MAX_CASE_COUNT:
            return None
    return data


def parse_group_result(stdout: str, stderr: str = "", returncode: int | None = None) -> dict | None:
    """校验一条测试组执行结果是否**可计分**；不可验证时返回 None。

    交叉校验（全部在可信侧完成）：
    - 结果行恰好一条，计数为有界非负整数；
    - 退出码只能是 0（全通过）或 1（有失败）；124 是超时，其他码是内部/收集错误；
    - 0 与 1 都必须伴随 pytest 汇总行——只有汇总行能证明 pytest 真的跑完并汇报了；
    - rc=0 ⇒ 不得报告失败或错误；rc=1 ⇒ 必须报告至少一个失败或错误。

    返回 None 表示"不可验证"。调用方**不得**为它产生正式成绩，必须记入 system_errors
    并走人工复核或 system_error 路径。

    注意：本函数是**立即减损**层，不是完整安全修复。学生代码仍可同时伪造汇总行与结果行；
    真正的信任边界由 Task 2B 的数据型用例 + 可信侧判定提供（见 judge_trusted_runner）。
    """
    counts = parse_result_json(stdout or "")
    if counts is None:
        return None
    if returncode == 124:
        return None
    if returncode not in (0, 1):
        return None
    if not PYTEST_SUMMARY_RE.search(f"{stdout or ''}\n{stderr or ''}"):
        return None
    if returncode == 0:
        if counts["failed"] or counts["errors"]:
            return None
    elif counts["failed"] + counts["errors"] < 1:
        return None
    return counts


def calculate_deterministic_grade(
    groups: list[TestGroup],
    results: dict[str, dict[str, int]],
) -> DeterministicGrade:
    """按测试组汇总 F 和 R 分数"""
    f_total = 0.0
    r_total = 0.0
    group_details = []
    system_errors = []

    for group in groups:
        counts = results.get(group.id)
        if counts is None:
            system_errors.append(f"测试组 {group.id} 缺少结果")
            continue

        try:
            score = calculate_group_score(group.max_score, counts)
        except DeterministicSystemError as exc:
            system_errors.append(f"测试组 {group.id}: {exc}")
            continue

        group_details.append({
            "id": group.id,
            "name": group.name,
            "dimension": group.dimension,
            "max_score": group.max_score,
            "score": score,
            "counts": counts,
        })

        if group.dimension == "F":
            f_total += score
        elif group.dimension == "R":
            r_total += score

    return DeterministicGrade(
        functional_score=round(f_total, 4),
        robustness_score=round(r_total, 4),
        groups=group_details,
        system_errors=system_errors,
    )
