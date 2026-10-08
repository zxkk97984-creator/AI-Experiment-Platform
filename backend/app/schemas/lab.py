"""The stable teacher/student contract for versioned lab assessments."""
import math
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabLevel(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    description: str = Field(min_length=1, max_length=1500)
    points: float = Field(ge=0, le=100, allow_inf_nan=False)


class LabCriterion(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=3000)
    source: Literal["metric", "ai", "check", "cell", "efficiency"] = "ai"
    max_score: float = Field(gt=0, le=100, allow_inf_nan=False)
    levels: list[LabLevel] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_levels(self):
        if self.source == "ai":
            if len(self.levels) < 2 or len({l.id for l in self.levels}) != len(self.levels):
                raise ValueError("AI 评分项至少需要两个唯一等级")
            if max(l.points for l in self.levels) != self.max_score or min(l.points for l in self.levels) != 0:
                raise ValueError("AI 等级须包含零分与满分")
        return self


class LabResources(StrictModel):
    cpus: float = Field(default=1, gt=0, le=64, allow_inf_nan=False)
    memory_mb: int = Field(default=4096, ge=256, le=262144)
    timeout_seconds: int = Field(default=1800, ge=10, le=86400)
    disk_mb: int = Field(default=2048, ge=64, le=102400)


class LabCellCheck(StrictModel):
    """Rules are teacher-owned; only the stable IDs are exposed to learners."""
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    cell_id: str = Field(min_length=1, max_length=100)
    kind: Literal["numeric_range", "numeric_close", "stdout", "shape", "python"] = "stdout"
    variable: str = Field(default="", pattern=r"^(?:[a-zA-Z_][a-zA-Z0-9_]{0,99})?$")
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    expected_value: float | None = Field(default=None, allow_inf_nan=False)
    tolerance: float = Field(default=1e-6, ge=0, le=1e6, allow_inf_nan=False)
    expected_output: str = Field(default="", max_length=10000)
    stdout_mode: Literal["exact", "contains"] = "exact"
    shape: list[int] = Field(default_factory=list, max_length=8)
    script: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def coherent(self):
        if self.kind in {"numeric_range", "numeric_close", "shape"} and not self.variable:
            raise ValueError("数值和形状检查须指定变量名称")
        if self.kind == "numeric_range" and (self.minimum is None or self.maximum is None or self.minimum > self.maximum):
            raise ValueError("数值区间须包含有效的最小值和最大值")
        if self.kind == "numeric_close" and self.expected_value is None:
            raise ValueError("近似数值检查须设置期望值")
        if self.kind == "shape" and (not self.shape or any(x < 0 for x in self.shape)):
            raise ValueError("形状须为非负整数维度")
        if self.kind == "python" and not self.script.strip():
            raise ValueError("自定义检查须上传或填写 Python 脚本")
        if self.kind == 'stdout' and self.stdout_mode == 'contains' and not self.expected_output:
            raise ValueError('包含匹配须设置非空的期望输出')
        return self


class LabAssessmentConfig(StrictModel):
    task_type: Literal["notebook", "tabular_regression", "tabular_classification", "image_classification"] = "notebook"
    teaching_mode: Literal["guided", "open"] = "guided"
    requirements: str = Field(default="", max_length=12000)
    dataset_id: int | None = Field(default=None, gt=0)
    environment_version_id: int | None = Field(default=None, gt=0)
    gpu_environment_version_id: int | None = Field(default=None, gt=0)
    allow_external: bool = True
    require_platform_training: bool = False
    max_submissions: int = Field(default=5, ge=1, le=100)
    grade_selection: Literal["highest", "latest"] = "highest"
    metric: Literal["macro_f1", "accuracy", "rmse", "mae"] = "macro_f1"
    zero_baseline: float = Field(default=0, allow_inf_nan=False)
    full_target: float = Field(default=1, allow_inf_nan=False)
    criteria: list[LabCriterion] = Field(default_factory=list, max_length=30)
    resources: LabResources = Field(default_factory=LabResources)
    evaluation_resources: LabResources = Field(default_factory=lambda: LabResources(timeout_seconds=300))
    reference_artifact_id: int | None = Field(default=None, gt=0)
    reference_cells: list[dict] = Field(default_factory=list, max_length=200)
    check_script: str = Field(default="", max_length=20000)
    cell_checks: list[LabCellCheck] = Field(default_factory=list, max_length=30)
    efficiency_full_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    efficiency_zero_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    benchmark_node_id: str = Field(default='', pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?$')
    confirmed: bool = False

    @model_validator(mode="after")
    def coherent(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("评分项 ID 不可重复")
        if self.criteria and not math.isclose(sum(c.max_score for c in self.criteria), 100, abs_tol=1e-6):
            raise ValueError("评分项满分之和必须为100")
        if len({c.id for c in self.cell_checks}) != len(self.cell_checks):
            raise ValueError("Cell 检查 ID 不可重复")
        if {c.id for c in self.criteria if c.source == 'cell'} != {c.id for c in self.cell_checks}:
            raise ValueError("Cell 评分项必须与 Cell 检查逐一对应")
        if self.cell_checks and self.task_type != 'notebook':
            # External source files lack the template's stable Cell identity.
            # Models with Cell marks must carry a platform-produced Notebook.
            self.require_platform_training = True
            self.allow_external = False
        if sum(c.source == 'efficiency' for c in self.criteria) > 1:
            raise ValueError("效率评分项最多一个")
        if self.efficiency_zero_seconds <= self.efficiency_full_seconds:
            raise ValueError("效率零分耗时须大于满分耗时")
        if self.task_type != "notebook":
            if self.metric in ("rmse", "mae") and self.zero_baseline <= self.full_target:
                raise ValueError("误差指标的零分基线必须大于满分目标")
            if self.metric in ("macro_f1", "accuracy") and not 0 <= self.zero_baseline < self.full_target <= 1:
                raise ValueError("分类指标必须满足0≤零分基线<满分目标≤1")
            if (self.task_type == "tabular_regression") != (self.metric in ("rmse", "mae")):
                raise ValueError("指标与实验类型不匹配")
        if self.require_platform_training:
            self.allow_external = False
        return self


class LabTrainRequest(StrictModel):
    client_request_id: UUID
    expected_record_revision: int = Field(ge=1)
    device: Literal["cpu", "gpu"] = "cpu"


class LabCellCheckRequest(StrictModel):
    client_request_id: UUID
    expected_record_revision: int = Field(ge=1)
    all_cells: bool = False


class LabArtifactRequest(StrictModel):
    artifact_id: int = Field(gt=0)
    client_request_id: UUID


class LabReviewRequest(StrictModel):
    score: float = Field(ge=0, le=100, allow_inf_nan=False)
    feedback: str = Field(default="", max_length=20000)
    reason: str = Field(min_length=1, max_length=2000)


class LabReason(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


class LabRegradeRequest(LabReason):
    submission_ids: list[PositiveInt] = Field(min_length=1, max_length=5000)

    @model_validator(mode='after')
    def unique_submission_ids(self):
        if len(set(self.submission_ids)) != len(self.submission_ids):
            raise ValueError('正式提交标识不能重复')
        return self
