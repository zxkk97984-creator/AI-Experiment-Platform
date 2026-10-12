"""分数合并——固定公式、上限、考试折算"""
from __future__ import annotations

from dataclasses import dataclass

# AI V1 固定公式 F60 + A20 + R10 + Q10 中由外部 LLM 给出的两个维度满分。
# F/R 由平台确定性判题产出、且受测试组配置强校验（F=60、R=10），无需在此重复约束。
ALGORITHM_DIMENSION_MAX = 20.0
QUALITY_DIMENSION_MAX = 10.0


@dataclass(frozen=True)
class MergedScore:
    raw_total: float
    final_score_100: float
    scaled_score: float


def merge_scores(
    *,
    f: float,
    a: float,
    r: float,
    q: float,
    cap: float | None,
    exam_points: float | None,
) -> MergedScore:
    """后端固定公式合并 F60 + A20 + R10 + Q10

    A/Q 先按维度满分钳制再合分。越界的 AI 输出本应已被 ai_score_validation
    拦成 review_required 而不落正式分，这里是调用方漏掉校验时的兜底：
    否则超范围维度分会把 raw 顶过 cap，min(raw, cap) 反而凭空产出满分。
    """
    a = min(a, ALGORITHM_DIMENSION_MAX)
    q = min(q, QUALITY_DIMENSION_MAX)
    raw = round(f + a + r + q, 4)
    final_100 = min(raw, cap) if cap is not None else raw
    final_100 = round(final_100, 4)
    scaled = round(final_100 / 100 * exam_points, 4) if exam_points is not None else final_100
    return MergedScore(raw_total=raw, final_score_100=final_100, scaled_score=scaled)
