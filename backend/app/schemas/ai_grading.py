"""AI 智能代码评分契约——题目配置、Rubric 文档、AI 输出、教师复核"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── 测试组配置 ──


class TestGroup(BaseModel):
    """结构化测试组：ID、名称、维度、满分和 pytest 测试"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Z][A-Z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
    dimension: Literal["F", "R"]
    max_score: float = Field(gt=0, le=60)
    tests: str = Field(min_length=1)


class ScoreCapRule(BaseModel):
    """教师配置的总分上限规则"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=40)
    condition_code: Literal[
        "off_topic",
        "hardcoded_public_examples",
        "required_algorithm_missing",
        "required_complexity_missing",
        "dangerous_operation",
    ]
    cap: float = Field(ge=0, le=100)
    description: str = Field(min_length=1, max_length=300)


def check_test_groups_weights(test_groups: list[TestGroup]) -> None:
    """active/shadow 测试组强校验：非空、F=60、R=10、ID 唯一。

    AIQuestionConfigUpdate 的 model_validator 与「AI 生成测试组」端点共用，
    避免生成结果绕过/漂移于现有配置强校验。
    """
    if not test_groups:
        raise ValueError("active/shadow 模式必须至少包含一个测试组")

    f_total = sum(g.max_score for g in test_groups if g.dimension == "F")
    r_total = sum(g.max_score for g in test_groups if g.dimension == "R")
    if abs(f_total - 60) > 1e-6 or abs(r_total - 10) > 1e-6:
        raise ValueError(
            f"AI V1 测试组必须满足 F=60、R=10，当前 F={f_total}、R={r_total}"
        )
    if len({g.id for g in test_groups}) != len(test_groups):
        raise ValueError("测试组 ID 必须唯一")


class AIQuestionConfigUpdate(BaseModel):
    """教师更新题目 AI 评分配置"""

    grading_mode: Literal["legacy", "shadow", "active"]
    teacher_constraints: dict[str, Any] = Field(default_factory=dict)
    reference_solution: str | None = None
    test_groups: list[TestGroup] = Field(default_factory=list)
    score_cap_rules: list[ScoreCapRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_weights(self) -> AIQuestionConfigUpdate:
        if self.grading_mode != "legacy":
            check_test_groups_weights(self.test_groups)
        return self


# ── AI 测试组生成（只生成、不保存） ──


class TestGroupsGenerateRequest(BaseModel):
    """AI 生成测试组请求——仅接受教师当前未保存草稿字段。

    不允许客户端传 hidden_tests、题干等权威数据，避免伪造或越权。
    """

    model_config = ConfigDict(extra="forbid")

    teacher_constraints: dict[str, Any] | None = None
    reference_solution: str | None = None


class TestGroupsValidationSummary(BaseModel):
    """生成结果的合规摘要"""

    model_config = ConfigDict(extra="forbid")

    f_total: float
    r_total: float
    group_count: int
    f_group_count: int
    r_group_count: int


class TestGroupsGenerateResponse(BaseModel):
    """AI 生成测试组响应——不含 hidden_tests 与原始模型输出"""

    model_config = ConfigDict(extra="forbid")

    test_groups: list[TestGroup]
    validation: TestGroupsValidationSummary
    warnings: list[str] = Field(default_factory=list)
    generation_id: str = Field(min_length=1, max_length=64)


# ── Rubric 文档 ──


class RubricCriterionItem(BaseModel):
    """Rubric 中的单个评分项"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    points: float = Field(gt=0, le=20)
    # 2026-08-09：AI 生成物常带评分项描述（内容合理且有价值），采纳为可空字段；
    # 其余未知字段仍 extra=forbid 拒绝
    description: str | None = Field(default=None, max_length=500)


class RubricDocument(BaseModel):
    """题目专属 Rubric——AI 生成后经校验保存"""

    model_config = ConfigDict(extra="forbid")

    rubric_version: int = Field(ge=1)
    question_type: str = Field(min_length=1, max_length=200)
    learning_objective: str = Field(min_length=1, max_length=500)
    explicit_requirements: list[str] = Field(default_factory=list)
    teacher_constraints: list[str] = Field(default_factory=list)
    accepted_strategies: list[str] = Field(default_factory=list)
    algorithm_criteria: list[RubricCriterionItem] = Field(min_length=1)
    quality_criteria: list[RubricCriterionItem] = Field(
        default_factory=lambda: [
            RubricCriterionItem(id="Q1", name="可读性与命名", points=3),
            RubricCriterionItem(id="Q2", name="代码结构", points=3),
            RubricCriterionItem(id="Q3", name="重复与冗余", points=2),
            RubricCriterionItem(id="Q4", name="接口、规范与安全", points=2),
        ]
    )
    uncertain_items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_totals(self) -> RubricDocument:
        a_total = sum(item.points for item in self.algorithm_criteria)
        if abs(a_total - 20) > 1e-6:
            raise ValueError(f"算法评分项满分之和必须为 20，当前为 {a_total}")

        q_total = sum(item.points for item in self.quality_criteria)
        if abs(q_total - 10) > 1e-6:
            raise ValueError(f"代码质量评分项满分之和必须为 10，当前为 {q_total}")

        # Q 固定为 Q1-Q4
        expected_q_ids = {"Q1", "Q2", "Q3", "Q4"}
        actual_q_ids = {item.id for item in self.quality_criteria}
        if actual_q_ids != expected_q_ids:
            raise ValueError(f"代码质量评分项必须固定为 {expected_q_ids}")

        return self


# ── AI 评分输出 ──


class GradeItem(BaseModel):
    """AI 对单个评分项的输出"""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1)
    criterion: str = Field(min_length=1, max_length=300)
    level: Literal["complete", "partial", "missing"]
    score: float = Field(ge=0)
    max_score: float = Field(ge=0)
    code_lines: list[int] = Field(default_factory=list)
    evidence: str = Field(min_length=1)
    reason_code: str | None = None
    deduction_reason: str | None = None


class GradeDimension(BaseModel):
    """AI 对某个维度的评分"""

    model_config = ConfigDict(extra="forbid")

    dimension_score: float = Field(ge=0)
    dimension_max: float = Field(ge=0)
    items: list[GradeItem] = Field(default_factory=list)


class CodeSuggestion(BaseModel):
    """AI 给出的具体代码修改建议（unified diff）"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    diff: str = Field(min_length=1)


class StudentFeedback(BaseModel):
    """学生可解释反馈"""

    model_config = ConfigDict(extra="forbid")

    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    code_suggestions: list[CodeSuggestion] = Field(default_factory=list)


class AIGradeResponse(BaseModel):
    """AI 评分完整返回——后端校验前"""

    model_config = ConfigDict(extra="forbid")

    rubric_version: int = Field(ge=1)
    algorithm: GradeDimension
    code_quality: GradeDimension
    triggered_cap_rule_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    needs_teacher_review: bool = False
    review_reason: str | None = None
    student_feedback: StudentFeedback

    @model_validator(mode="after")
    def validate_dimension_consistency(self) -> AIGradeResponse:
        # 校验 A 维度和
        a_sum = sum(item.score for item in self.algorithm.items)
        if abs(a_sum - self.algorithm.dimension_score) > 1e-4:
            raise ValueError("算法维度：分项和与 dimension_score 不一致")

        # 校验 Q 维度和
        q_sum = sum(item.score for item in self.code_quality.items)
        if abs(q_sum - self.code_quality.dimension_score) > 1e-4:
            raise ValueError("代码质量维度：分项和与 dimension_score 不一致")

        return self


# ── 教师复核 ──


class GradeOverrideCreate(BaseModel):
    """教师覆盖评分请求"""

    algorithm_score: float | None = Field(default=None, ge=0, le=20)
    quality_score: float | None = Field(default=None, ge=0, le=10)
    final_score_100: float | None = Field(default=None, ge=0, le=100)
    # 确定性分（功能/边界）：仅在评分记录里缺失时才需要教师显式给出——
    # 不允许把"缺失"当成 0 参与合分（会把 60 分权重凭空抹掉）。
    functional_score: float | None = Field(default=None, ge=0, le=60)
    robustness_score: float | None = Field(default=None, ge=0, le=10)
    reason: str = Field(min_length=3, max_length=1000)

    @model_validator(mode="after")
    def validate_at_least_one(self) -> GradeOverrideCreate:
        if self.algorithm_score is None and self.quality_score is None and self.final_score_100 is None:
            raise ValueError("至少需要指定 algorithm_score、quality_score 或 final_score_100 之一")
        return self


# ── 安全导出（学生可见） ──


class CodeGradeItemRead(BaseModel):
    """单条评分项——供学生查看"""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    criterion: str
    level: str
    score: float
    max_score: float
    code_lines: list[int] = Field(default_factory=list)
    evidence: str
    deduction_reason: str | None = None


class ActiveCodeGradeRead(BaseModel):
    """学生可见的评分拆解——仅 active 模式返回"""

    model_config = ConfigDict(extra="forbid")

    mode: str
    status: str
    functional_score: float
    algorithm_score: float | None = None
    robustness_score: float
    quality_score: float | None = None
    raw_total: float | None = None
    score_cap: float | None = None
    final_score_100: float | None = None
    scaled_score: float | None = None

    algorithm_items: list[CodeGradeItemRead] = Field(default_factory=list)
    quality_items: list[CodeGradeItemRead] = Field(default_factory=list)

    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


# ── 一键 AI 配置与草稿试判（批次 2） ────────────────────────────────


class AISetupJobCreate(BaseModel):
    """一键配置任务：填写参考解法与评分侧重后异步生成草稿。"""

    model_config = ConfigDict(extra="forbid")

    question_kind: Literal["assignment", "exam"]
    question_id: int = Field(gt=0)
    reference_solution: str | None = Field(default=None, max_length=50000)
    teacher_constraints: dict[str, Any] | None = None
    grading_mode: Literal["legacy", "shadow", "active"] | None = None
    # 是否同时用 AI 生成 F/R 测试组；关闭时只用评分标准草稿（测试组走现有高级配置）
    generate_test_groups: bool = True


class AISetupTrialRunCreate(BaseModel):
    """草稿试判：样例代码只用于预览草稿规则，不生成任何正式提交。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=50000)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=64)


class AISetupEnableRequest(BaseModel):
    """保存并启用：可携带微调后的草稿正文、测试组与题目配置。"""

    model_config = ConfigDict(extra="forbid")

    rubric: RubricDocument | None = None
    test_groups: list[TestGroup] | None = None
    teacher_constraints: dict[str, Any] | None = None
    score_cap_rules: list[ScoreCapRule] | None = None
    compare_rules: dict[str, Any] | None = None
    entry_function: str | None = Field(default=None, min_length=1, max_length=120)
    acknowledge_in_flight: bool = False
