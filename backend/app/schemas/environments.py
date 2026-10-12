"""环境档位控制面 schema（Phase 1：控制面模型）

校验规则统一委托 app.services.import_policy（函数内延迟导入避免循环依赖）：
- pip_name：只允许字母数字 . _ -，拒绝 URL/路径/参数/换行注入
- locked_version：单个精确 PEP 440 版本，拒绝范围/通配符/marker/extras
- import_names：合法 Python dotted identifier，归一化顶级模块并去重排序
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── 包目录 ─────────────────────────────────────────────────────


class PythonPackageSpec(BaseModel):
    """管理员请求的一个 Python 直接依赖。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    import_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self):
        from app.services.environment_spec import normalize_requested_spec

        normalized = normalize_requested_spec(
            {
                "schema_version": 1,
                "python_packages": [self.model_dump()],
                "system_packages": [],
            }
        )["python_packages"][0]
        self.name = normalized["name"]
        self.version = normalized["version"]
        self.import_names = normalized["import_names"]
        return self


class SystemPackageSpec(BaseModel):
    """管理员请求的一个 apt 直接依赖。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    version: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _normalize(self):
        from app.services.environment_spec import normalize_requested_spec

        normalized = normalize_requested_spec(
            {
                "schema_version": 1,
                "python_packages": [],
                "system_packages": [self.model_dump()],
            }
        )["system_packages"][0]
        self.name = normalized["name"]
        self.version = normalized["version"]
        return self


class RequestedEnvironmentSpec(BaseModel):
    """完整 requested_spec——所有持久化和构建入口共用的输入契约。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    python_packages: list[PythonPackageSpec] = Field(default_factory=list)
    system_packages: list[SystemPackageSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self):
        from app.services.environment_spec import normalize_requested_spec

        normalized = normalize_requested_spec(self.model_dump())
        self.python_packages = [
            PythonPackageSpec.model_validate(item) for item in normalized["python_packages"]
        ]
        self.system_packages = [
            SystemPackageSpec.model_validate(item) for item in normalized["system_packages"]
        ]
        return self


class PackageCatalogCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pip_name: str = Field(max_length=128)
    locked_version: str = Field(max_length=64)
    import_names: list[str] = Field(default_factory=list)
    category_tags: list[str] = Field(default_factory=list)
    source_key: Literal["pypi", "pytorch_cpu"] = "pypi"

    @field_validator("pip_name")
    @classmethod
    def _check_pip_name(cls, v: str) -> str:
        from app.services.import_policy import normalize_pip_name

        normalize_pip_name(v)  # 校验；存储时 normalized_name 单独计算
        return v

    @field_validator("locked_version")
    @classmethod
    def _check_locked_version(cls, v: str) -> str:
        from app.services.import_policy import validate_locked_version

        return validate_locked_version(v)

    @field_validator("import_names")
    @classmethod
    def _check_import_names(cls, v: list[str]) -> list[str]:
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @field_validator("category_tags")
    @classmethod
    def _check_category_tags(cls, v: list[str]) -> list[str]:
        normalized = []
        seen = set()
        for tag in v:
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError("分类标签不能为空")
            if len(tag) > 32:
                raise ValueError("分类标签长度不能超过 32")
            tag = tag.strip()
            if tag not in seen:
                seen.add(tag)
                normalized.append(tag)
        return sorted(normalized)


class PackageCatalogUpdate(BaseModel):
    """编辑包目录条目（Phase 2 扩展：支持可选核心字段）。

    - 仅改分类/状态：直接原地更新。
    - 修改核心字段（包名/版本/import 名/来源）：
      - 包未被任何环境版本引用 → 允许原地修改（normalized_name 重算）；
      - 已被引用 → API 层返回 PACKAGE_IMMUTABLE，要求创建新目录条目（supersedes 关联）。
    """

    model_config = ConfigDict(extra="forbid")

    pip_name: str | None = Field(default=None, max_length=128)
    locked_version: str | None = Field(default=None, max_length=64)
    import_names: list[str] | None = None
    source_key: Literal["pypi", "pytorch_cpu"] | None = None
    category_tags: list[str] | None = None
    status: Literal["active", "inactive"] | None = None

    @field_validator("pip_name")
    @classmethod
    def _check_pip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from app.services.import_policy import normalize_pip_name

        normalize_pip_name(v)  # 校验
        return v

    @field_validator("locked_version")
    @classmethod
    def _check_locked_version(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from app.services.import_policy import validate_locked_version

        return validate_locked_version(v)

    @field_validator("import_names")
    @classmethod
    def _check_import_names(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @field_validator("category_tags")
    @classmethod
    def _check_category_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        normalized = []
        seen = set()
        for tag in v:
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError("分类标签不能为空")
            if len(tag) > 32:
                raise ValueError("分类标签长度不能超过 32")
            tag = tag.strip()
            if tag not in seen:
                seen.add(tag)
                normalized.append(tag)
        return sorted(normalized)


class PackageCatalogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    normalized_name: str
    pip_name: str
    locked_version: str
    import_names: list[str]
    category_tags: list[str]
    source_key: str
    status: str
    supersedes_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── 环境档位 ───────────────────────────────────────────────────


class EnvironmentProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # V2 generates env-<short id> when omitted.  The legacy endpoint still
    # validates a supplied slug in its service layer.
    slug: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class EnvironmentProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "inactive"] | None = None


class EnvironmentProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    display_name: str
    description: str | None = None
    status: str
    created_at: datetime | None = None


# ── 环境版本 ───────────────────────────────────────────────────


class EnvironmentVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version_id: int | None = None
    package_ids: list[int] = Field(default_factory=list)
    minimum_memory_mb: int = Field(ge=64, le=65536)


class EnvironmentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    version_number: int
    source_version_id: int | None = None
    status: str
    base_image_ref: str
    image_tag: str | None = None
    image_digest: str | None = None
    python_version: str | None = None
    minimum_memory_mb: int
    manifest_sha256: str
    dockerfile_sha256: str | None = None
    resolved_packages: dict | None = None
    available_at: datetime | None = None
    created_at: datetime | None = None


class PackageSummary(BaseModel):
    """包摘要——学生/教师列表展示用，不含供应链内部字段"""

    pip_name: str
    locked_version: str
    import_names: list[str]


class SystemPackageSummary(BaseModel):
    name: str
    version: str | None = None


class EnvironmentOptionRead(BaseModel):
    """教师可选环境选项——只含 available 版本，不含 digest/tag/构建日志"""

    profile_id: int
    environment_version_id: int
    slug: str
    display_name: str
    description: str | None = None
    version_number: int
    packages: list[PackageSummary] = Field(default_factory=list)
    system_packages: list[SystemPackageSummary] = Field(default_factory=list)
    minimum_memory_mb: int


class EnvironmentSummaryRead(BaseModel):
    """学生响应中的环境摘要——绝不包含 tag、digest、基础镜像或构建日志"""

    display_name: str
    version_label: str
    python_version: str
    imports: list[str] = Field(default_factory=list)
    import_policy_mode: str
    allowed_imports: list[str] = Field(default_factory=list)


# ── 构建任务 ───────────────────────────────────────────────────


class EnvironmentBuildCreate(BaseModel):
    """发起构建请求——无任何 Dockerfile/requirements/pip 参数输入面"""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=200)


class EnvironmentBuildRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    environment_version_id: int
    status: str
    attempt_number: int
    retry_of_id: int | None = None
    worker_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    queue_wakeup_pending: bool = False


class EnvironmentBuildLogRead(BaseModel):
    """构建日志——入库前已脱敏并截断（60 KiB 尾部），仅 admin 可见"""

    job_id: int
    status: str
    log_text: str = ""


class EnvironmentBuildListRead(EnvironmentBuildRead):
    """管理端构建任务列表项——附加版本摘要（UI 展示档位版本与短 digest）"""

    profile_display_name: str | None = None
    profile_slug: str | None = None
    version_number: int | None = None
    version_status: str | None = None
    image_digest_short: str | None = None


# ── import 策略与诊断 ──────────────────────────────────────────


class ImportPolicyInput(BaseModel):
    """作业/实验的 import 教学策略——不是安全边界，安全由 Docker 隔离负责"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["unrestricted", "restricted"]
    allowed_imports: list[str] = Field(default_factory=list)

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str]) -> list[str]:
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)


class ImportDiagnosticRead(BaseModel):
    """结构化 import 诊断——学生 API 只返回安全中文信息

    - IMPORT_NOT_ALLOWED：教学策略不允许（学生错误，明确扣分）
    - IMPORT_NOT_INSTALLED：策略允许但环境未安装（平台配置问题，不扣分）
    - ENVIRONMENT_DRIFT：manifest 声称安装但镜像实际缺失（平台问题）
    - ENVIRONMENT_IMAGE_MISSING：绑定环境版本没有可用镜像（平台问题，不扣分）
    - 评分基线/版本/Rubric 不可验证：安全提示教师复核，不包含隐藏测试或内部日志
    """

    code: Literal[
        "IMPORT_NOT_ALLOWED",
        "IMPORT_NOT_INSTALLED",
        "ENVIRONMENT_DRIFT",
        "ENVIRONMENT_IMAGE_MISSING",
        "MISSING_GRADING_BASELINE",
        "MISSING_SCORE_BASELINE",
        "MANUAL_REVIEW_REQUIRED",
        "DATA_VERSION_MODE_MISMATCH",
        "RUBRIC_REQUIRED",
    ]
    module: str
    message: str


# ── 管理员视角扩展（Phase 2） ──────────────────────────────────


class PackageCatalogAdminRead(PackageCatalogRead):
    """管理端包目录条目——附加是否被环境版本引用（供 UI 提示不可变语义）"""

    referenced: bool = False


class PackageSummaryAdmin(PackageSummary):
    """管理端包摘要——附加包目录 id（UI 复制版本时预选勾选用）"""

    id: int


class EnvironmentVersionListRead(EnvironmentVersionRead):
    """管理端版本列表项——附加包摘要（UI 展示档位包集合用）"""

    packages: list[PackageSummaryAdmin] = Field(default_factory=list)


class EnvironmentProfileListRead(EnvironmentProfileRead):
    """管理端档位列表项——附加当前可用版本（最新可用版本摘要，UI 列表展示用）"""

    latest_version: EnvironmentVersionListRead | None = None


# ── Environment editor V2 ─────────────────────────────────────


class EnvironmentCapabilities(BaseModel):
    """Server-authoritative actions for the administrator editor."""

    can_edit_profile: bool = False
    can_create_draft: bool = False
    can_edit_draft: bool = False
    can_build: bool = False
    can_retry: bool = False
    can_publish: bool = False
    can_abandon_draft: bool = False
    can_rollback: bool = False
    can_archive: bool = False
    can_restore: bool = False


class EnvironmentEditorOptionsRead(BaseModel):
    python_versions: list[str]
    default_python_version: str
    minimum_memory_mb: int
    maximum_memory_mb: int
    default_memory_mb: int
    max_python_packages: int
    max_system_packages: int
    source_display_names: dict[str, str] = Field(default_factory=dict)


class BuildReadinessRead(BaseModel):
    ready: bool
    checks: dict[str, dict]


class EnvironmentVersionEditorRead(BaseModel):
    id: int
    profile_id: int
    version_number: int
    source_version_id: int | None = None
    status: str
    python_version: str
    minimum_memory_mb: int
    requested_spec: dict
    resolved_spec: dict | None = None
    image_digest: str | None = None
    image_size_bytes: int | None = None
    first_published_at: datetime | None = None
    first_published_by_id: int | None = None
    available_at: datetime | None = None
    created_at: datetime | None = None
    published: bool = False
    current: bool = False
    diff: dict | None = None
    build_report: dict | None = None


class EnvironmentDraftRead(BaseModel):
    profile_id: int
    source_version_id: int | None = None
    candidate_version_id: int | None = None
    active_build_job_id: int | None = None
    revision: int
    state: Literal["editing", "building", "ready", "failed"]
    python_version: str
    minimum_memory_mb: int
    requested_spec: RequestedEnvironmentSpec
    capabilities: EnvironmentCapabilities = Field(default_factory=EnvironmentCapabilities)


class EnvironmentDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    python_version: str
    minimum_memory_mb: int = Field(ge=64, le=65536)
    requested_spec: RequestedEnvironmentSpec


class EnvironmentProfileEditorListRead(BaseModel):
    id: int
    slug: str
    display_name: str
    description: str | None = None
    status: str
    current_version: EnvironmentVersionEditorRead | None = None
    draft: EnvironmentDraftRead | None = None
    recent_build: "EnvironmentBuildEditorRead | None" = None
    capabilities: EnvironmentCapabilities = Field(default_factory=EnvironmentCapabilities)


class EnvironmentProfileEditorRead(EnvironmentProfileEditorListRead):
    versions: list[EnvironmentVersionEditorRead] = Field(default_factory=list)


class EnvironmentPublicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_version_id: int = Field(gt=0)
    # Required even when the expected pointer is NULL.  This distinguishes
    # an intentional first-publish compare from an omitted stale-page token.
    expected_current_version_id: int | None


class EnvironmentPublicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    version_id: int
    previous_version_id: int | None = None
    action: Literal["publish", "rollback", "migration_baseline"]
    published_by_id: int | None = None
    created_at: datetime | None = None


class EnvironmentBuildEditorRead(BaseModel):
    id: int
    environment_version_id: int
    profile_display_name: str | None = None
    profile_slug: str | None = None
    version_number: int | None = None
    version_status: str | None = None
    image_digest_short: str | None = None
    status: str
    phase: str
    attempt_number: int
    retry_of_id: int | None = None
    worker_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_detail: dict | None = None
    result_summary: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    queue_wakeup_pending: bool = False
    capabilities: EnvironmentCapabilities = Field(default_factory=EnvironmentCapabilities)


class PackageCandidateRead(BaseModel):
    manager: Literal["pip", "apt"]
    name: str
    versions: list[str] = Field(default_factory=list)
    description: str | None = None
    compatible: bool | None = None
    denied: bool = False
    deny_reason: str | None = None
    indexing: bool = False
