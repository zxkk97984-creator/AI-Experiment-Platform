"""AI 输出业务校验——版本、criterion、行号、分数上界、防重复扣分"""
from __future__ import annotations

from typing import Any

from app.services.score_merger import ALGORITHM_DIMENSION_MAX, QUALITY_DIMENSION_MAX

_TOLERANCE = 1e-6


def _as_float(value: Any) -> float | None:
    """容错转 float；非数值返回 None，避免校验自身抛异常变成 500。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _points_by_criterion(criteria: Any) -> dict[str, float]:
    """从 rubric 评分项列表建立 criterion_id -> 满分 的映射。"""
    if not isinstance(criteria, list):
        return {}
    points: dict[str, float] = {}
    for entry in criteria:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        value = _as_float(entry.get("points"))
        if cid is not None and value is not None:
            points[str(cid)] = value
    return points


def _validate_dimension_bounds(
    *,
    label: str,
    dimension: Any,
    criteria_points: dict[str, float],
    fallback_max: float,
    errors: list[str],
) -> None:
    """校验维度满分与分项上界——AI 自报的 dimension_max / max_score 一律不可信。

    缺这层校验时，被 prompt 注入或幻觉的模型可以返回完全自洽的超范围分数
    （"分项和 == dimension_score" 的 Pydantic 校验依然通过），再经
    merge_scores 的 min(raw, cap) 钳制后直接拿到满分，而确定性测试可能一个都没过。
    """
    if not isinstance(dimension, dict):
        errors.append(f"{label}维度评分结果缺失或格式非法")
        return

    # 以 rubric 实际评分项之和为权威上界；rubric 未提供时回退固定公式常量。
    expected_max = (
        round(sum(criteria_points.values()), 6) if criteria_points else fallback_max
    )

    dimension_max = _as_float(dimension.get("dimension_max"))
    if dimension_max is None or abs(dimension_max - expected_max) > _TOLERANCE:
        errors.append(
            f"{label}维度满分无效：期望 {expected_max}，实际 {dimension.get('dimension_max')}"
        )

    dimension_score = _as_float(dimension.get("dimension_score"))
    if dimension_score is not None and dimension_score > expected_max + _TOLERANCE:
        errors.append(
            f"{label}维度得分 {dimension_score} 超过满分 {expected_max}"
        )

    for item in dimension.get("items", []):
        if not isinstance(item, dict):
            continue
        cid = item.get("criterion_id")
        score = _as_float(item.get("score"))
        max_score = _as_float(item.get("max_score"))
        rubric_points = criteria_points.get(str(cid)) if cid is not None else None

        if rubric_points is not None and max_score is not None \
                and abs(max_score - rubric_points) > _TOLERANCE:
            errors.append(
                f"{label}评分项 {cid} 满分与 Rubric 不一致：期望 {rubric_points}，实际 {max_score}"
            )

        # 优先用 rubric 满分作为天花板：即使 AI 谎报 max_score 也拦得住。
        ceiling = rubric_points if rubric_points is not None else max_score
        if score is not None and ceiling is not None and score > ceiling + _TOLERANCE:
            errors.append(f"{label}评分项 {cid} 得分 {score} 超过满分 {ceiling}")


def validate_ai_output(
    rubric: dict[str, Any],
    ai_result: dict[str, Any],
    code_lines: list[int],
) -> list[str]:
    """校验 AI 评分输出——返回错误列表，空列表表示通过"""
    errors = []

    # 0. rubric_version 匹配
    rubric_ver = rubric.get("rubric_version")
    ai_ver = ai_result.get("rubric_version")
    if rubric_ver is not None and ai_ver is not None and rubric_ver != ai_ver:
        errors.append(f"Rubric 版本不匹配：期望 {rubric_ver}，实际 {ai_ver}")

    # 提取 criterion 集合
    rubric_a_ids = {c["id"] for c in rubric.get("algorithm_criteria", [])}
    rubric_q_ids = {"Q1", "Q2", "Q3", "Q4"}

    max_code_line = max(code_lines) if code_lines else 0

    # 1. 校验 A 维度
    algorithm = ai_result.get("algorithm", {})
    a_items = algorithm.get("items", [])

    for item in a_items:
        cid = item.get("criterion_id")
        if cid not in rubric_a_ids:
            errors.append(f"未知的算法 criterion_id: {cid}")
            continue

        # 行号校验
        for line in item.get("code_lines", []):
            if line not in code_lines:
                errors.append(f"行号 {line} 不存在于学生代码中（criterion {cid}）")

        # 分数校验
        level = item.get("level")
        if level not in ("complete", "partial", "missing"):
            errors.append(f"非法的等级 {level}（criterion {cid}）")

    # 2. 校验 Q 维度
    code_quality = ai_result.get("code_quality", {})
    q_items = code_quality.get("items", [])

    for item in q_items:
        cid = item.get("criterion_id")
        if cid not in rubric_q_ids:
            errors.append(f"未知的代码质量 criterion_id: {cid}（只能是 Q1-Q4）")

        for line in item.get("code_lines", []):
            if line not in code_lines:
                errors.append(f"行号 {line} 不存在于学生代码中（criterion {cid}）")

    # 3. 跨维度重复扣分检测
    duplicates = detect_cross_dimension_duplicates(a_items, q_items)
    if duplicates:
        for dup in duplicates:
            errors.append(
                f"跨维度重复扣分：A 的 {dup['a_criterion']} 与 Q 的 {dup['q_criterion']} "
                f"使用相同 reason_code={dup['reason_code']} 且代码行重叠"
            )

    # 4. 分数上界——维度满分与每个分项都不得超过 Rubric 规定的天花板
    _validate_dimension_bounds(
        label="算法",
        dimension=algorithm,
        criteria_points=_points_by_criterion(rubric.get("algorithm_criteria")),
        fallback_max=ALGORITHM_DIMENSION_MAX,
        errors=errors,
    )
    _validate_dimension_bounds(
        label="代码质量",
        dimension=code_quality,
        criteria_points=_points_by_criterion(rubric.get("quality_criteria")),
        fallback_max=QUALITY_DIMENSION_MAX,
        errors=errors,
    )

    return errors


def detect_cross_dimension_duplicates(
    a_items: list[dict],
    q_items: list[dict],
) -> list[dict]:
    """检测 A 和 Q 之间的重复扣分"""
    duplicates = []

    for ai in a_items:
        a_reason = ai.get("reason_code")
        if not a_reason:
            continue
        a_lines = set(ai.get("code_lines", []))

        for qi in q_items:
            q_reason = qi.get("reason_code")
            if not q_reason:
                continue
            q_lines = set(qi.get("code_lines", []))

            if a_reason == q_reason and a_lines & q_lines:
                duplicates.append({
                    "a_criterion": ai.get("criterion_id"),
                    "q_criterion": qi.get("criterion_id"),
                    "reason_code": a_reason,
                    "overlapping_lines": sorted(a_lines & q_lines),
                })

    return duplicates
