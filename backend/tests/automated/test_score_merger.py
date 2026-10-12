"""Task 7: 分数合并测试——固定公式、上限、考试折算"""
from app.services.score_merger import merge_scores


def test_merge_basic_100_scale():
    """F54 + A13 + R7 + Q5 = 79"""
    result = merge_scores(f=54, a=13, r=7, q=5, cap=None, exam_points=None)
    assert result.raw_total == 79
    assert result.final_score_100 == 79
    assert result.scaled_score == 79


def test_exam_score_is_scaled():
    """考试 25 分题：79/100 * 25 = 19.75"""
    result = merge_scores(f=54, a=13, r=7, q=5, cap=None, exam_points=25)
    assert result.raw_total == 79
    assert result.final_score_100 == 79
    assert result.scaled_score == 19.75


def test_cap_limits_score():
    """上限 80：raw=87 → final=80"""
    result = merge_scores(f=60, a=18, r=9, q=8, cap=80, exam_points=None)
    assert result.raw_total == 95
    assert result.final_score_100 == 80
    assert result.scaled_score == 80


def test_cap_not_exceeded():
    """raw=79，上限=80，不触发上限"""
    result = merge_scores(f=54, a=13, r=7, q=5, cap=80, exam_points=None)
    assert result.raw_total == 79
    assert result.final_score_100 == 79


def test_rounded_scores():
    """分数保留 4 位小数"""
    result = merge_scores(f=54.1234, a=13.5678, r=7.9999, q=5.0001, cap=None, exam_points=None)
    assert result.raw_total == round(54.1234 + 13.5678 + 7.9999 + 5.0001, 4)


# ── S-1 兜底：A/Q 维度钳制 ───────────────────────────────────────────
# 越界的 AI 输出本应已被 ai_score_validation 拦成 review_required；
# 这里保证即使调用方漏掉校验，超范围维度分也无法凭空产出满分。


def test_inflated_ai_dimensions_cannot_reach_full_mark():
    """注入攻击：F=0 但 AI 自称 A=100、Q=10，不得钳到 cap 变成满分"""
    result = merge_scores(f=0, a=100, r=0, q=10, cap=100, exam_points=None)
    assert result.raw_total == 30
    assert result.final_score_100 == 30


def test_algorithm_dimension_clamped_to_20():
    """A 维度上限 20"""
    result = merge_scores(f=60, a=35, r=10, q=10, cap=None, exam_points=None)
    assert result.raw_total == 100
    assert result.final_score_100 == 100


def test_quality_dimension_clamped_to_10():
    """Q 维度上限 10"""
    result = merge_scores(f=60, a=20, r=10, q=25, cap=None, exam_points=None)
    assert result.raw_total == 100


def test_clamped_scaled_exam_score():
    """钳制后再按题目分值折算：30/100 × 25 = 7.5，而非满分 25"""
    result = merge_scores(f=0, a=100, r=0, q=10, cap=100, exam_points=25)
    assert result.scaled_score == 7.5


def test_in_range_dimensions_untouched():
    """合法区间内的分数不受钳制影响"""
    result = merge_scores(f=54, a=20, r=9, q=10, cap=None, exam_points=None)
    assert result.raw_total == 93
