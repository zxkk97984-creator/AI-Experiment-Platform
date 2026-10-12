"""Task 7: AI 输出校验测试——A/Q 业务规则、越界行号、重复扣分"""
import pytest


def test_validates_rubric_version_mismatch():
    """Rubric 版本不匹配时拒绝"""
    from app.services.ai_score_validation import validate_ai_output

    rubric = {
        "rubric_version": 2,
        "algorithm_criteria": [
            {"id": "A1", "name": "搜索区间", "points": 10},
            {"id": "A2", "name": "缩小范围", "points": 10},
        ],
    }

    ai_result = {
        "rubric_version": 1,  # 版本不匹配
        "algorithm": {
            "dimension_score": 20,
            "dimension_max": 20,
            "items": [
                {"criterion_id": "A1", "criterion": "搜索区间", "level": "complete", "score": 10, "max_score": 10, "code_lines": [1], "evidence": "ok"},
                {"criterion_id": "A2", "criterion": "缩小范围", "level": "complete", "score": 10, "max_score": 10, "code_lines": [2], "evidence": "ok"},
            ],
        },
        "code_quality": {
            "dimension_score": 10,
            "dimension_max": 10,
            "items": [],
        },
    }

    errors = validate_ai_output(rubric, ai_result, code_lines=[1, 2])
    assert len(errors) > 0
    assert any("不匹配" in e for e in errors)


def test_rejects_unknown_criterion():
    """Rubric 中不存在的 criterion_id 被拒绝"""
    from app.services.ai_score_validation import validate_ai_output

    rubric = {
        "rubric_version": 1,
        "algorithm_criteria": [
            {"id": "A1", "name": "搜索区间", "points": 10},
        ],
    }

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 10,
            "dimension_max": 20,
            "items": [
                {"criterion_id": "A99", "criterion": "不存在", "level": "complete", "score": 10, "max_score": 10, "code_lines": [1], "evidence": "ok"},
            ],
        },
        "code_quality": {
            "dimension_score": 10,
            "dimension_max": 10,
            "items": [],
        },
    }

    errors = validate_ai_output(rubric, ai_result, code_lines=[1])
    assert len(errors) > 0
    assert any("A99" in e for e in errors)


def test_rejects_out_of_range_line():
    """行号超出代码范围被拒绝"""
    from app.services.ai_score_validation import validate_ai_output

    rubric = {
        "rubric_version": 1,
        "algorithm_criteria": [
            {"id": "A1", "name": "搜索区间", "points": 20},
        ],
    }

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 20,
            "dimension_max": 20,
            "items": [
                {"criterion_id": "A1", "criterion": "搜索区间", "level": "complete", "score": 20, "max_score": 20, "code_lines": [999], "evidence": "ok"},
            ],
        },
        "code_quality": {
            "dimension_score": 10,
            "dimension_max": 10,
            "items": [],
        },
    }

    errors = validate_ai_output(rubric, ai_result, code_lines=[1, 2, 3])
    assert len(errors) > 0
    assert any("999" in e or "行号" in e for e in errors)


def test_duplicate_reason_across_aq_triggers_review():
    """A 和 Q 中相同 reason_code 且代码行重叠触发 teacher_review"""
    from app.services.ai_score_validation import detect_cross_dimension_duplicates

    a_items = [
        {"criterion_id": "A1", "reason_code": "wrong_boundary", "code_lines": [5, 6, 7]},
    ]
    q_items = [
        {"criterion_id": "Q2", "reason_code": "wrong_boundary", "code_lines": [6, 7]},
    ]

    duplicates = detect_cross_dimension_duplicates(a_items, q_items)
    assert len(duplicates) > 0


# ── S-1：分数上界校验 ────────────────────────────────────────────────
# AI 自报的 dimension_max / max_score 不可信。缺这层校验时，被 prompt 注入或
# 幻觉的模型可返回完全自洽的超范围分数（"分项和 == dimension_score" 的 Pydantic
# 校验照样通过），经 merge_scores 的 min(raw, cap) 钳制后直接拿到满分。


def _full_rubric():
    return {
        "rubric_version": 1,
        "algorithm_criteria": [
            {"id": "A1", "name": "搜索区间", "points": 10},
            {"id": "A2", "name": "缩小范围", "points": 10},
        ],
        "quality_criteria": [
            {"id": "Q1", "name": "可读性与命名", "points": 3},
            {"id": "Q2", "name": "代码结构", "points": 3},
            {"id": "Q3", "name": "重复与冗余", "points": 2},
            {"id": "Q4", "name": "接口、规范与安全", "points": 2},
        ],
    }


def _item(cid, score, max_score, line=1):
    return {
        "criterion_id": cid, "criterion": cid, "level": "complete",
        "score": score, "max_score": max_score, "code_lines": [line], "evidence": "ok",
    }


def test_wellformed_full_score_passes():
    """合法的满分输出不应产生任何错误（防误报回归）"""
    from app.services.ai_score_validation import validate_ai_output

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 20, "dimension_max": 20,
            "items": [_item("A1", 10, 10, 1), _item("A2", 10, 10, 2)],
        },
        "code_quality": {
            "dimension_score": 10, "dimension_max": 10,
            "items": [_item("Q1", 3, 3, 1), _item("Q2", 3, 3, 1),
                      _item("Q3", 2, 2, 1), _item("Q4", 2, 2, 1)],
        },
    }

    assert validate_ai_output(_full_rubric(), ai_result, code_lines=[1, 2]) == []


def test_rejects_prompt_injection_full_score():
    """注入攻击：自洽的超范围分数必须被拦下。

    复现路径——学生在代码里写注入指令，AI 返回 A=100 且分项和恰好等于
    dimension_score，criterion_id 在白名单内、行号真实存在、level 合法，
    旧版全部校验通过，最终 min(110, cap=100) 给出满分。
    """
    from app.services.ai_score_validation import validate_ai_output

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 100, "dimension_max": 100,
            "items": [_item("A1", 100, 100, 1)],
        },
        "code_quality": {
            "dimension_score": 10, "dimension_max": 10, "items": [],
        },
    }

    errors = validate_ai_output(_full_rubric(), ai_result, code_lines=[1, 2])
    assert errors, "超范围 AI 分数必须被拒绝"
    assert any("超过满分" in e for e in errors)


def test_rejects_dimension_max_mismatch():
    """AI 谎报维度满分（按百分制打分）即使得分未越界也要拦下"""
    from app.services.ai_score_validation import validate_ai_output

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 20, "dimension_max": 100,
            "items": [_item("A1", 10, 10, 1), _item("A2", 10, 10, 2)],
        },
        "code_quality": {"dimension_score": 10, "dimension_max": 10, "items": []},
    }

    errors = validate_ai_output(_full_rubric(), ai_result, code_lines=[1, 2])
    assert any("维度满分无效" in e for e in errors)


def test_rejects_item_score_above_item_max():
    """分项得分超过分项满分"""
    from app.services.ai_score_validation import validate_ai_output

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 25, "dimension_max": 20,
            "items": [_item("A1", 15, 10, 1), _item("A2", 10, 10, 2)],
        },
        "code_quality": {"dimension_score": 10, "dimension_max": 10, "items": []},
    }

    errors = validate_ai_output(_full_rubric(), ai_result, code_lines=[1, 2])
    assert any("A1" in e and "超过满分" in e for e in errors)


def test_rejects_item_max_score_inconsistent_with_rubric():
    """分项满分与 Rubric 规定不一致——即使得分未超过自报满分"""
    from app.services.ai_score_validation import validate_ai_output

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 20, "dimension_max": 20,
            "items": [_item("A1", 50, 50, 1), _item("A2", -30, 10, 2)],
        },
        "code_quality": {"dimension_score": 10, "dimension_max": 10, "items": []},
    }

    errors = validate_ai_output(_full_rubric(), ai_result, code_lines=[1, 2])
    assert any("A1" in e and "与 Rubric 不一致" in e for e in errors)


def test_rejects_quality_dimension_overflow():
    """Q 维度同样受 10 分上限约束"""
    from app.services.ai_score_validation import validate_ai_output

    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 20, "dimension_max": 20,
            "items": [_item("A1", 10, 10, 1), _item("A2", 10, 10, 2)],
        },
        "code_quality": {
            "dimension_score": 60, "dimension_max": 60,
            "items": [_item("Q1", 60, 60, 1)],
        },
    }

    errors = validate_ai_output(_full_rubric(), ai_result, code_lines=[1, 2])
    assert any("代码质量" in e for e in errors)


def test_quality_bounds_fall_back_to_formula_constant():
    """rubric 未提供 quality_criteria 时，回退固定公式常量 10"""
    from app.services.ai_score_validation import validate_ai_output

    rubric = {"rubric_version": 1, "algorithm_criteria": [{"id": "A1", "name": "x", "points": 20}]}
    ai_result = {
        "rubric_version": 1,
        "algorithm": {
            "dimension_score": 20, "dimension_max": 20, "items": [_item("A1", 20, 20, 1)],
        },
        "code_quality": {"dimension_score": 55, "dimension_max": 10, "items": []},
    }

    errors = validate_ai_output(rubric, ai_result, code_lines=[1])
    assert any("代码质量" in e and "超过满分" in e for e in errors)
