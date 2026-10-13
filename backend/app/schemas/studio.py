from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _non_empty_cell_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("cell id must not be empty")
    return value


CellId = Annotated[str, AfterValidator(_non_empty_cell_id)]


class StudioCell(BaseModel):
    """The complete, fixed Studio cell contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: CellId
    type: Literal["markdown", "code"]
    source: str = ""
    order: int
    # 历史草稿/版本数据可能缺失这两个字段，读取时按默认值兼容
    student_editable: bool = True
    source_hidden: bool = False

    @model_validator(mode="after")
    def reject_hidden_markdown(self):
        if self.type == "markdown" and self.source_hidden:
            raise ValueError("markdown cells cannot be hidden")
        return self


class StudioTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    lesson_id: int | None = None
    module_id: int | None = None
    # ── 草稿环境绑定（Phase 4：教师选择） ─────────────────────
    # 教师显式选择 available 环境版本；省略时服务层解析 basic 当前可用版本。
    environment_version_id: int | None = None
    import_policy_mode: Literal["unrestricted", "restricted"] = "unrestricted"
    allowed_imports: list[str] = Field(default_factory=list)

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str]) -> list[str]:
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @model_validator(mode="after")
    def one_context_at_most(self):
        if self.lesson_id is not None and self.module_id is not None:
            raise ValueError("lesson_id and module_id are mutually exclusive")
        return self


class StudioTemplateMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class StudioTemplateBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lesson_id: int | None = None
    module_id: int | None = None

    @model_validator(mode="after")
    def one_context_at_most(self):
        if self.lesson_id is not None and self.module_id is not None:
            raise ValueError("lesson_id and module_id are mutually exclusive")
        return self


class StudioDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_revision: int
    cells: list[StudioCell]
    # ── 草稿环境绑定（Phase 4） ───────────────────────────────
    # 与 cells 同一 revision 保存；显式传了才更新（服务层 exclude_unset），
    # 兼容旧调用（不传时保留草稿已有环境）。
    environment_version_id: int | None = None
    import_policy_mode: Literal["unrestricted", "restricted"] | None = None
    allowed_imports: list[str] | None = None
    # ── 运行配置（Q1：发布前配置、发布后锁定） ────────────────
    # 单格执行超时 10–1800 秒；内核空闲回收 300–7200 秒（UI 用分钟）。省略=不改。
    cell_timeout_seconds: int | None = Field(default=None, ge=10, le=1800)
    kernel_idle_seconds: int | None = Field(default=None, ge=300, le=7200)

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @model_validator(mode="after")
    def unique_cell_ids(self):
        ids = [cell.id for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("cell ids must be unique")
        return self


class StudioImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    lesson_id: int | None = None
    module_id: int | None = None
    environment_version_id: int | None = None
    import_policy_mode: Literal["unrestricted", "restricted"] = "unrestricted"
    allowed_imports: list[str] = Field(default_factory=list)

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str]) -> list[str]:
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @model_validator(mode="after")
    def one_context_at_most(self):
        if self.lesson_id is not None and self.module_id is not None:
            raise ValueError("lesson_id and module_id are mutually exclusive")
        return self


class StudioImportExisting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_revision: int


class StudioPreviewRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: CellId


class StudioPreviewRunResponse(BaseModel):
    outputs: list[dict] = Field(default_factory=list)
    execution_time_ms: int | None = None


class StudioAssetRead(BaseModel):
    """Public manifest entry; contains no physical storage path."""

    relative_path: str
    storage_object_id: int | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


class StudioVersionRead(BaseModel):
    id: int
    template_id: int
    version_number: int
    sha256: str
    cells: list[StudioCell]
    cell_order: list[str]
    notebook_metadata: dict
    assets_dir: str | None = None
    asset_manifest_id: int | None = None
    assets: list[StudioAssetRead] = Field(default_factory=list)
    published_at: datetime
    published_by_id: int
    # 发布时从草稿复制的不可变环境快照（Phase 4）
    environment_version_id: int | None = None
    import_policy_mode: str = "unrestricted"
    allowed_imports: list[str] = Field(default_factory=list)
    # 发布时冻结的运行配置（发布后锁定，调整需发布新版本）
    cell_timeout_seconds: int = 120
    kernel_idle_seconds: int = 900


class StudioTemplateRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    status: str
    current_version_id: int | None = None
    owner_id: int
    draft_cells: list[StudioCell]
    draft_revision: int
    draft_metadata: dict
    draft_assets_dir: str | None = None
    draft_asset_manifest_id: int | None = None
    draft_assets: list[StudioAssetRead] = Field(default_factory=list)
    # 草稿环境绑定（Phase 4）：发布时复制到新版本，历史版本不更新
    draft_environment_version_id: int | None = None
    draft_import_policy_mode: str = "unrestricted"
    draft_allowed_imports: list[str] = Field(default_factory=list)
    # 草稿运行配置（Q1）：发布时冻结到版本，发布后锁定
    draft_cell_timeout_seconds: int = 120
    draft_kernel_idle_seconds: int = 900
    lesson_id: int | None = None
    module_id: int | None = None
    current_version: StudioVersionRead | None = None
