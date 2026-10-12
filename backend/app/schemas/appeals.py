"""单次成绩异议契约（批次 2）：学生申请/撤回/补充说明 + 教师与管理员处理。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AppealCreate(BaseModel):
    """学生申请：只覆盖实际参与计分的 AI 评分结果。"""

    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["submission", "exam_answer", "lab_submission", "practice_submission"]
    target_id: int = Field(gt=0)
    reason: str = Field(min_length=20, max_length=2000)


class AppealReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)


class AppealHandle(BaseModel):
    """处理动作：维持 / 拒绝 / 改分（走既有 override 通道）/ 重评（走既有 retry 通道）。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["uphold", "adjust", "regrade", "reject"]
    comment: str = Field(default="", max_length=2000)
    score: float | None = Field(default=None, ge=0, le=100)
    functional_score: float | None = Field(default=None, ge=0, le=60)
    robustness_score: float | None = Field(default=None, ge=0, le=10)
    algorithm_score: float | None = Field(default=None, ge=0, le=20)
    quality_score: float | None = Field(default=None, ge=0, le=10)
    final_score_100: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_adjust_payload(self) -> AppealHandle:
        if self.action == "adjust" and not any(value is not None for value in (
            self.score, self.functional_score, self.robustness_score,
            self.algorithm_score, self.quality_score, self.final_score_100,
        )):
            raise ValueError("改分必须给出总分或至少一个维度分")
        return self
