"""自主练习契约（批次 5）。

单独成文件（不改 ``app/schemas/__init__.py``，避免并行批次注册面竞争）。
字段名即前端契约：``PracticeCase.input/expected`` 用平台的类型化值编码
（``services/grading_values.encode_value``），管理员接口接受 ``args`` 原始 JSON 后
由后端编码，前端不必理解编码协议。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PracticeCase(BaseModel):
    """判题用例（存储形状）：``input`` 是位置参数元组的编码值。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    input: Any
    expected: Any
    compare: dict[str, Any] | None = None


class PracticeCaseInput(BaseModel):
    """管理员填写形状：``args`` 为原始 JSON 位置参数列表，``expected`` 为原始期望值。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=64)
    args: list[Any] = Field(default_factory=list)
    expected: Any = None
    compare: dict[str, Any] | None = None


class PracticeProblemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    tags: list[str] = Field(default_factory=list, max_length=20)
    prompt: str = Field(default="", max_length=20000)
    starter_code: str | None = Field(default=None, max_length=20000)
    public_cases: list[PracticeCaseInput] = Field(default_factory=list, max_length=64)
    hidden_tests: list[PracticeCaseInput] = Field(default_factory=list, max_length=256)
    time_limit_ms: int = Field(default=10000, ge=500, le=60000)
    memory_limit_kb: int = Field(default=262144, ge=16384, le=2097152)
    function_name: str = Field(default="solution", min_length=1, max_length=120)
    reference_solution: str | None = Field(default=None, max_length=50000)
    teacher_constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_function_name(self) -> PracticeProblemCreate:
        if not self.function_name.isidentifier():
            raise ValueError("入口函数名必须是合法标识符")
        return self


class PracticeProblemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    difficulty: Literal["easy", "medium", "hard"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    prompt: str | None = Field(default=None, max_length=20000)
    starter_code: str | None = Field(default=None, max_length=20000)
    public_cases: list[PracticeCaseInput] | None = Field(default=None, max_length=64)
    hidden_tests: list[PracticeCaseInput] | None = Field(default=None, max_length=256)
    time_limit_ms: int | None = Field(default=None, ge=500, le=60000)
    memory_limit_kb: int | None = Field(default=None, ge=16384, le=2097152)
    function_name: str | None = Field(default=None, min_length=1, max_length=120)
    reference_solution: str | None = Field(default=None, max_length=50000)
    teacher_constraints: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_function_name(self) -> PracticeProblemUpdate:
        if self.function_name is not None and not self.function_name.isidentifier():
            raise ValueError("入口函数名必须是合法标识符")
        return self


class CopyFromAssignmentRequest(BaseModel):
    """从已有作业复制题目：集合级入口，一步生成新的草稿题。"""

    model_config = ConfigDict(extra="forbid")

    assignment_id: int = Field(gt=0)
    question_id: int = Field(gt=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class PracticeSubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=50000)
    client_request_id: str = Field(min_length=1, max_length=64)


class PracticeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grading_mode: Literal["deterministic", "ai"] | None = None
    daily_submission_cap_per_student: int | None = Field(default=None, ge=0, le=10000)
    daily_ai_budget_tokens: int | None = Field(default=None, ge=0, le=100000000)
    daily_ai_budget_amount: float | None = Field(default=None, ge=0, le=1000000)
    ai_amount_per_1k_tokens: float | None = Field(default=None, ge=0, le=1000)
    max_concurrent_practice_jobs: int | None = Field(default=None, ge=1, le=16)


class PracticeAppealHandle(BaseModel):
    """管理员处理练习异议：维持 / 改分 / 重评 / 拒绝。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["uphold", "adjust", "regrade", "reject"]
    comment: str = Field(default="", max_length=2000)
    score: float | None = Field(default=None, ge=0, le=100)
