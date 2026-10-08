from __future__ import annotations

import copy
import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import nbformat
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import api_error
from app.models import (
    Chapter,
    Course,
    ExperimentModule,
    Lesson,
    NotebookTemplate,
    NotebookTemplateVersion,
    StudioAssetManifest,
    User,
)
from app.schemas.studio import (
    StudioAssetRead,
    StudioCell,
    StudioDraftUpdate,
    StudioTemplateBindRequest,
    StudioTemplateCreate,
    StudioTemplateMetadataUpdate,
    StudioTemplateRead,
    StudioVersionRead,
)
from app.services.studio_asset_manifest_service import (
    DraftAssetBindingPlan,
    StagedStudioAssetBundle,
    StudioAssetInfo,
    StudioAssetManifestService,
    VersionAssetBindingPlan,
    validate_relative_asset_path,
)
from app.storage import (
    InvalidStorageKey,
    StorageConflict,
    StorageError,
    StorageNotFound,
)


MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
MAX_ZIP_ENTRY_BYTES = 20 * 1024 * 1024
ALLOWED_ASSET_EXTENSIONS = {
    ".csv",
    ".gif",
    ".ipynb",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".png",
    ".py",
    ".svg",
    ".txt",
}


@dataclass(frozen=True)
class ImportedNotebook:
    cells: list[dict]
    metadata: dict
    assets: tuple[tuple[str, bytes], ...] = ()


def preview_session_key(template_id: int, user_id: int) -> int:
    """Collision-free negative Cantor pairing, disjoint from record IDs."""
    total = template_id + user_id
    return -(((total * (total + 1)) // 2) + user_id + 1)


def _normalize_cell(cell: dict) -> dict:
    """兼容历史数据：早期草稿/版本可能缺少 student_editable / source_hidden 字段，读取时补齐默认值。

    默认值语义与前端创建 Cell 时一致：code cell 可编辑，source_hidden 默认为 False（避免旧
    code cell 被误判为隐藏初始化 Cell）。
    """
    cell_type = cell.get("type") if cell.get("type") in ("markdown", "code") else "markdown"
    return {
        "id": str(cell.get("id", "")).strip(),
        "type": cell_type,
        "source": cell.get("source", ""),
        "order": int(cell.get("order", 0)),
        "student_editable": bool(cell.get("student_editable", cell_type == "code")),
        "source_hidden": bool(cell.get("source_hidden", False)),
    }


def _normalize_cells(cells: list[dict] | None) -> list[dict]:
    return [_normalize_cell(cell) for cell in (cells or [])]


def _asset_reads(assets: tuple[StudioAssetInfo, ...]) -> list[StudioAssetRead]:
    return [
        StudioAssetRead(
            relative_path=asset.relative_path,
            storage_object_id=asset.storage_object_id,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            sha256=asset.sha256,
        )
        for asset in assets
    ]


def _version_read(
    version: NotebookTemplateVersion,
    *,
    manifest_id: int | None = None,
    assets: tuple[StudioAssetInfo, ...] = (),
) -> StudioVersionRead:
    return StudioVersionRead(
        id=version.id,
        template_id=version.template_id,
        version_number=version.version_number,
        sha256=version.sha256,
        cells=_normalize_cells(version.cells),
        cell_order=version.cell_order,
        notebook_metadata=version.notebook_metadata or {},
        assets_dir=StudioAssetManifestService.public_legacy_prefix(version.assets_dir),
        asset_manifest_id=manifest_id,
        assets=_asset_reads(assets),
        published_at=version.published_at,
        published_by_id=version.published_by_id,
        environment_version_id=version.environment_version_id,
        import_policy_mode=version.import_policy_mode,
        allowed_imports=list(version.allowed_imports or []),
    )


def version_read(
    db: Session, settings: Settings, version: NotebookTemplateVersion
) -> StudioVersionRead:
    assets_service = StudioAssetManifestService.from_settings(settings)
    manifest_id, assets = assets_service.version_assets(
        db, version.id, version.assets_dir
    )
    return _version_read(version, manifest_id=manifest_id, assets=assets)


def _binding_ids(db: Session, template_id: int) -> tuple[int | None, int | None]:
    lesson_id = db.scalar(
        select(Lesson.id).where(Lesson.template_id == template_id).limit(1)
    )
    module_id = db.scalar(
        select(ExperimentModule.id)
        .where(ExperimentModule.template_id == template_id)
        .limit(1)
    )
    return lesson_id, module_id


def template_read(
    db: Session,
    template: NotebookTemplate,
    settings: Settings | None = None,
) -> StudioTemplateRead:
    settings = settings or Settings()
    assets_service = StudioAssetManifestService.from_settings(settings)
    lesson_id, module_id = _binding_ids(db, template.id)
    current = None
    if template.current_version_id:
        version = db.get(NotebookTemplateVersion, template.current_version_id)
        if version and version.template_id == template.id:
            current = version_read(db, settings, version)
    draft_manifest_id, draft_assets = assets_service.draft_assets(db, template)
    return StudioTemplateRead(
        id=template.id,
        name=template.name,
        description=template.description,
        status=template.status,
        current_version_id=template.current_version_id,
        owner_id=template.owner_id,
        draft_cells=_normalize_cells(template.draft_cells),
        draft_revision=template.draft_revision,
        draft_metadata=template.draft_metadata or {},
        draft_assets_dir=StudioAssetManifestService.public_legacy_prefix(
            template.draft_assets_dir
        ),
        draft_asset_manifest_id=draft_manifest_id,
        draft_assets=_asset_reads(draft_assets),
        draft_environment_version_id=template.draft_environment_version_id,
        draft_import_policy_mode=template.draft_import_policy_mode,
        draft_allowed_imports=list(template.draft_allowed_imports or []),
        lesson_id=lesson_id,
        module_id=module_id,
        current_version=current,
    )


def require_manager(template: NotebookTemplate, user: User) -> None:
    if user.role != "admin" and template.owner_id != user.id:
        raise api_error(403, "FORBIDDEN", "无权管理其他用户的 Notebook 模板")


def get_managed_template(
    db: Session, template_id: int, user: User
) -> NotebookTemplate:
    template = db.get(NotebookTemplate, template_id)
    if not template:
        raise api_error(404, "TEMPLATE_NOT_FOUND", "Notebook 模板不存在")
    require_manager(template, user)
    return template


def list_templates(
    db: Session, user: User, settings: Settings | None = None
) -> list[StudioTemplateRead]:
    query = select(NotebookTemplate)
    if user.role != "admin":
        query = query.where(NotebookTemplate.owner_id == user.id)
    templates = db.scalars(query.order_by(NotebookTemplate.id)).all()
    return [template_read(db, template, settings) for template in templates]


def _authorize_context(
    db: Session,
    user: User,
    lesson_id: int | None,
    module_id: int | None,
    *,
    creating: bool,
) -> Lesson | ExperimentModule | None:
    if lesson_id is not None:
        lesson = db.get(Lesson, lesson_id)
        if not lesson:
            raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
        chapter = db.get(Chapter, lesson.chapter_id)
        course = db.get(Course, chapter.course_id) if chapter else None
        if user.role == "teacher" and (
            course is None or course.teacher_id != user.id
        ):
            raise api_error(403, "FORBIDDEN", "只能绑定自己课程中的课时")
        return lesson

    if module_id is not None:
        module = db.get(ExperimentModule, module_id)
        if not module:
            raise api_error(
                404, "EXPERIMENT_MODULE_NOT_FOUND", "独立实验模块不存在"
            )
        if user.role == "teacher" and module.owner_id != user.id:
            raise api_error(403, "FORBIDDEN", "只能绑定自己创建的实验模块")
        return module

    if user.role == "teacher" and creating:
        raise api_error(403, "LESSON_REQUIRED", "教师模板必须绑定自己课程的课时")
    return None


def _bind(
    db: Session,
    template: NotebookTemplate,
    user: User,
    lesson_id: int | None,
    module_id: int | None,
    *,
    creating: bool = False,
) -> None:
    target = _authorize_context(
        db, user, lesson_id, module_id, creating=creating
    )
    # 换绑会把旧宿主的 template_id 置空；不允许让「已发布」的实验入口变成学生不可用的孤儿
    bound_lessons = db.scalars(
        select(Lesson).where(Lesson.template_id == template.id)
    ).all()
    for old in bound_lessons:
        if isinstance(target, Lesson) and old.id == target.id:
            continue
        if old.content_type == "notebook" and old.status == "published":
            raise api_error(
                409,
                "REBIND_CONFLICT",
                f"该模板已绑定到已发布的 Notebook 课时「{old.title}」。"
                "请先在 Studio 为其更换其他模板，或将该课时下线后再重新绑定",
            )
    bound_modules = db.scalars(
        select(ExperimentModule).where(ExperimentModule.template_id == template.id)
    ).all()
    for old in bound_modules:
        if isinstance(target, ExperimentModule) and old.id == target.id:
            continue
        if old.status == "published":
            raise api_error(
                409,
                "REBIND_CONFLICT",
                f"该模板已绑定到已发布的实验模块「{old.name}」。请先将其下线后再重新绑定",
            )
    db.execute(
        update(Lesson)
        .where(Lesson.template_id == template.id)
        .values(template_id=None)
    )
    db.execute(
        update(ExperimentModule)
        .where(ExperimentModule.template_id == template.id)
        .values(template_id=None)
    )
    if isinstance(target, Lesson):
        target.template_id = template.id
    elif isinstance(target, ExperimentModule):
        target.template_id = template.id


def _resolve_draft_environment(
    db: Session,
    environment_version_id: int | None,
    import_policy_mode: str,
    allowed_imports: list[str],
    settings: Settings | None = None,
) -> dict:
    """创建路径的环境解析（Phase 4）——显式选择必须 available；省略时解析 basic 当前可用版本。

    返回 draft_environment_version_id / draft_import_policy_mode / draft_allowed_imports 三件套。
    """
    from app.services.environment_service import (
        resolve_basic_available_version,
        validate_environment_selection,
    )

    validate_environment_selection(db, environment_version_id, settings=settings)
    env_id = environment_version_id
    if env_id is None:
        basic = resolve_basic_available_version(db, settings=settings)
        env_id = basic.id if basic else None
    return {
        "draft_environment_version_id": env_id,
        "draft_import_policy_mode": import_policy_mode,
        "draft_allowed_imports": list(allowed_imports or []),
    }


def _create_template_record(
    db: Session,
    payload: StudioTemplateCreate,
    user: User,
    settings: Settings | None = None,
) -> NotebookTemplate:
    _authorize_context(
        db,
        user,
        payload.lesson_id,
        payload.module_id,
        creating=True,
    )
    template = NotebookTemplate(
        name=payload.name.strip(),
        description=payload.description,
        owner_id=user.id,
        status="draft",
        draft_cells=[],
        draft_revision=1,
        draft_metadata={},
        **_resolve_draft_environment(
            db,
            payload.environment_version_id,
            payload.import_policy_mode,
            payload.allowed_imports,
            settings,
        ),
    )
    db.add(template)
    db.flush()
    db.add(StudioAssetManifest(template_id=template.id, revision=1))
    db.flush()
    _bind(
        db,
        template,
        user,
        payload.lesson_id,
        payload.module_id,
        creating=True,
    )
    return template


def create_template_record(
    db: Session,
    payload: StudioTemplateCreate,
    user: User,
    settings: Settings | None = None,
) -> NotebookTemplate:
    """在调用方事务中创建模板；调用方负责 commit/rollback。"""
    return _create_template_record(db, payload, user, settings)


def create_template(
    db: Session,
    payload: StudioTemplateCreate,
    user: User,
    settings: Settings | None = None,
) -> NotebookTemplate:
    try:
        template = create_template_record(db, payload, user, settings)
        db.commit()
        db.refresh(template)
        return template
    except Exception:
        db.rollback()
        raise


def ensure_module_template(
    db: Session,
    module: ExperimentModule,
    user: User,
    settings: Settings | None = None,
) -> NotebookTemplate:
    """确保模块有一个属于当前所有者的可编辑 Notebook 草稿。"""
    if user.role == "teacher" and module.owner_id != user.id:
        raise api_error(403, "FORBIDDEN", "只能管理自己创建的实验模块")

    if module.template_id is not None:
        template = db.get(NotebookTemplate, module.template_id)
        if template is None:
            raise api_error(
                409,
                "MODULE_TEMPLATE_INCONSISTENT",
                "实验模块关联的 Notebook 模板不存在",
            )
        if template.owner_id != module.owner_id:
            raise api_error(
                409,
                "MODULE_TEMPLATE_INCONSISTENT",
                "实验模块与 Notebook 模板所有者不一致",
            )
        return template

    try:
        template = _create_template_record(
            db,
            StudioTemplateCreate(
                name=module.name,
                description=module.description,
                module_id=module.id,
            ),
            user,
            settings,
        )
        db.commit()
        db.refresh(template)
        db.refresh(module)
        return template
    except Exception:
        db.rollback()
        raise


def update_metadata(
    db: Session,
    template: NotebookTemplate,
    payload: StudioTemplateMetadataUpdate,
) -> NotebookTemplate:
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        changes["name"] = changes["name"].strip()
    for key, value in changes.items():
        setattr(template, key, value)
    db.commit()
    db.refresh(template)
    return template


def bind_template(
    db: Session,
    template: NotebookTemplate,
    payload: StudioTemplateBindRequest,
    user: User,
) -> NotebookTemplate:
    if user.role == "teacher" and payload.lesson_id is None and payload.module_id is None:
        raise api_error(
            403,
            "CONTEXT_REQUIRED",
            "教师模板必须绑定自己课程的课时或自己的独立实验模块",
        )
    try:
        _bind(
            db,
            template,
            user,
            payload.lesson_id,
            payload.module_id,
        )
        db.commit()
        db.refresh(template)
        return template
    except Exception:
        db.rollback()
        raise


def normalize_cells(cells: list[StudioCell]) -> list[dict]:
    ordered = sorted(cells, key=lambda cell: (cell.order, cell.id))
    return [
        {**cell.model_dump(), "order": index}
        for index, cell in enumerate(ordered)
    ]


def _revision_conflict(db: Session, template_id: int):
    db.rollback()
    current = db.get(NotebookTemplate, template_id)
    raise api_error(
        409,
        "REVISION_CONFLICT",
        "草稿已被其他会话修改，请刷新后重试",
        {"current_revision": current.draft_revision if current else None},
    )


def atomic_replace_draft(
    db: Session,
    template: NotebookTemplate,
    expected_revision: int,
    cells: list[dict],
    *,
    metadata: dict | None = None,
    assets_dir: str | None | object = ...,
    environment: dict | None = None,
) -> NotebookTemplate:
    """草稿原子替换——cells 与草稿环境（Phase 4）在同一 revision 内更新，避免内容成功而环境失败。

    environment 为 draft_environment_version_id/draft_import_policy_mode/draft_allowed_imports 三件套；
    为 None 表示不更新环境（兼容旧调用）。
    """
    values = {
        "draft_cells": copy.deepcopy(cells),
        "draft_revision": expected_revision + 1,
        "updated_at": func.now(),
    }
    if metadata is not None:
        values["draft_metadata"] = copy.deepcopy(metadata)
    if assets_dir is not ...:
        values["draft_assets_dir"] = assets_dir
    if environment is not None:
        values.update(environment)
    manifest = db.scalar(
        select(StudioAssetManifest).where(
            StudioAssetManifest.template_id == template.id
        )
    )
    if manifest is not None:
        manifest.revision = expected_revision + 1
    result = db.execute(
        update(NotebookTemplate)
        .where(
            NotebookTemplate.id == template.id,
            NotebookTemplate.draft_revision == expected_revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        _revision_conflict(db, template.id)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(template)
    return template


def save_draft(
    db: Session,
    template: NotebookTemplate,
    payload: StudioDraftUpdate,
    settings: Settings | None = None,
) -> NotebookTemplate:
    environment = None
    updates = payload.model_dump(exclude_unset=True)
    env_fields = {
        k: v
        for k, v in updates.items()
        if k in ("environment_version_id", "import_policy_mode", "allowed_imports")
    }
    if env_fields:
        # 显式选择的环境必须 available（省略时保留草稿已有环境，不重新解析）
        from app.services.environment_service import validate_environment_selection

        validate_environment_selection(
            db,
            env_fields.get("environment_version_id"),
            settings=settings,
        )
        environment = {
            "draft_environment_version_id": env_fields.get(
                "environment_version_id", template.draft_environment_version_id
            ),
            "draft_import_policy_mode": env_fields.get(
                "import_policy_mode", template.draft_import_policy_mode
            ),
            "draft_allowed_imports": list(
                env_fields.get(
                    "allowed_imports", list(template.draft_allowed_imports or [])
                )
            ),
        }
    return atomic_replace_draft(
        db,
        template,
        payload.draft_revision,
        normalize_cells(payload.cells),
        environment=environment,
    )


def _stable_cell_id(index: int, cell_type: str, source: str) -> str:
    digest = hashlib.sha256(
        f"{index}\0{cell_type}\0{source}".encode("utf-8")
    ).hexdigest()[:20]
    return f"cell-{digest}"


def _parse_notebook(data: bytes) -> tuple[list[dict], dict]:
    try:
        text = data.decode("utf-8-sig")
        notebook = nbformat.reads(text, as_version=4)
    except Exception as exc:
        raise api_error(400, "IMPORT_INVALID", f"Notebook 文件无效：{exc}")

    result: list[dict] = []
    seen_ids: set[str] = set()
    for index, cell in enumerate(notebook.cells):
        cell_type = str(cell.get("cell_type", ""))
        if cell_type not in {"markdown", "code"}:
            raise api_error(
                400,
                "IMPORT_UNSUPPORTED_CELL",
                f"不支持的 Notebook cell 类型：{cell_type}",
            )
        source = str(cell.get("source", ""))
        cell_id = str(cell.get("id", "")).strip()
        if not cell_id:
            cell_id = _stable_cell_id(index, cell_type, source)
        if cell_id in seen_ids:
            raise api_error(400, "IMPORT_DUPLICATE_CELL_ID", "Notebook cell ID 重复")
        seen_ids.add(cell_id)

        dai_metadata = cell.get("metadata", {}).get("dai", {})
        if not isinstance(dai_metadata, dict):
            dai_metadata = {}
        editable = dai_metadata.get("student_editable", cell_type == "code")
        hidden = dai_metadata.get("source_hidden", False)
        if not isinstance(editable, bool):
            editable = cell_type == "code"
        if not isinstance(hidden, bool):
            hidden = False
        try:
            normalized = StudioCell(
                id=cell_id,
                type=cell_type,
                source=source,
                order=index,
                student_editable=editable,
                source_hidden=hidden,
            )
        except Exception as exc:
            raise api_error(400, "IMPORT_INVALID_CELL", str(exc))
        result.append(normalized.model_dump())

    try:
        metadata = json.loads(json.dumps(dict(notebook.metadata)))
    except (TypeError, ValueError) as exc:
        raise api_error(400, "IMPORT_INVALID_METADATA", str(exc))
    return result, metadata


def _safe_zip_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or name.startswith(("/", "\\")):
        raise api_error(400, "ZIP_UNSAFE_PATH", "ZIP 包含不安全路径")
    raw_parts = name.split("/")
    if any(not part or part == "." or part == ".." for part in raw_parts):
        raise api_error(400, "ZIP_UNSAFE_PATH", "ZIP 包含不安全路径")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise api_error(400, "ZIP_UNSAFE_PATH", "ZIP 包含目录穿越路径")
    if path.parts and ":" in path.parts[0]:
        raise api_error(400, "ZIP_UNSAFE_PATH", "ZIP 包含绝对路径")
    return path


def parse_import(filename: str, data: bytes) -> ImportedNotebook:
    if len(data) > MAX_IMPORT_BYTES:
        raise api_error(400, "IMPORT_TOO_LARGE", "导入文件超过大小限制")
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".ipynb":
        cells, metadata = _parse_notebook(data)
        return ImportedNotebook(cells=cells, metadata=metadata)
    if suffix != ".zip":
        raise api_error(400, "IMPORT_UNSUPPORTED", "仅支持 .ipynb 或 .zip")

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise api_error(400, "IMPORT_INVALID_ZIP", f"ZIP 文件无效：{exc}")

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise api_error(400, "ZIP_TOO_MANY_ENTRIES", "ZIP 文件条目过多")
        total_size = 0
        notebooks = []
        assets: list[tuple[str, bytes]] = []
        seen_asset_paths: set[str] = set()
        for info in infos:
            if info.is_dir():
                _safe_zip_path(info.filename.rstrip("/"))
                continue
            path = _safe_zip_path(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise api_error(400, "ZIP_SYMLINK", "ZIP 不允许符号链接")
            if info.flag_bits & 0x1:
                raise api_error(400, "ZIP_ENCRYPTED", "ZIP 不允许加密条目")
            if info.file_size > MAX_ZIP_ENTRY_BYTES:
                raise api_error(400, "ZIP_ENTRY_TOO_LARGE", "ZIP 单个条目过大")
            total_size += info.file_size
            if total_size > MAX_IMPORT_BYTES:
                raise api_error(400, "ZIP_TOO_LARGE", "ZIP 解压总大小过大")
            suffix = path.suffix.lower()
            try:
                relative_path = validate_relative_asset_path(path.as_posix())
            except InvalidStorageKey as exc:
                raise api_error(400, "ZIP_UNSAFE_PATH", "ZIP 包含不安全路径") from exc
            if suffix not in ALLOWED_ASSET_EXTENSIONS:
                raise api_error(
                    400, "ZIP_UNSUPPORTED_FILE", f"ZIP 不支持文件类型：{suffix}"
                )
            try:
                content = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise api_error(400, "IMPORT_INVALID_ZIP", str(exc))
            if len(content) > MAX_ZIP_ENTRY_BYTES:
                raise api_error(400, "ZIP_ENTRY_TOO_LARGE", "ZIP 单个条目过大")
            if suffix == ".ipynb":
                notebooks.append((relative_path, content))
            else:
                if relative_path in seen_asset_paths:
                    raise api_error(400, "ZIP_DUPLICATE_ASSET", "ZIP 包含重复资源路径")
                seen_asset_paths.add(relative_path)
                assets.append((relative_path, content))
        if len(notebooks) != 1:
            raise api_error(
                400, "ZIP_AMBIGUOUS_NOTEBOOK", "ZIP 必须且只能包含一个 Notebook"
            )
        cells, metadata = _parse_notebook(notebooks[0][1])
        return ImportedNotebook(
            cells=cells, metadata=metadata, assets=tuple(assets)
        )


def create_imported_template(
    db: Session,
    settings: Settings,
    user: User,
    *,
    name: str,
    description: str | None,
    lesson_id: int | None,
    module_id: int | None,
    environment_version_id: int | None = None,
    import_policy_mode: str = "unrestricted",
    allowed_imports: list[str] | None = None,
    imported: ImportedNotebook,
) -> NotebookTemplate:
    _authorize_context(
        db, user, lesson_id, module_id, creating=True
    )
    template = NotebookTemplate(
        name=name.strip(),
        description=description,
        owner_id=user.id,
        status="draft",
        draft_cells=copy.deepcopy(imported.cells),
        draft_revision=1,
        draft_metadata=copy.deepcopy(imported.metadata),
        **_resolve_draft_environment(
            db,
            environment_version_id,
            import_policy_mode,
            list(allowed_imports or []),
            settings,
        ),
    )
    assets_service = StudioAssetManifestService.from_settings(settings)
    staged: StagedStudioAssetBundle | None = None
    binding: DraftAssetBindingPlan | None = None
    try:
        db.add(template)
        db.flush()
        staged = assets_service.stage_bundle(
            template.id, 1, imported.assets
        )
        binding = assets_service.bind_draft_assets(
            db,
            template,
            revision=1,
            staged=staged,
            created_by_id=user.id,
        )
        _bind(
            db,
            template,
            user,
            lesson_id,
            module_id,
            creating=True,
        )
        db.commit()
        db.refresh(template)
        if binding is not None:
            assets_service.finalize_draft_assets(db, binding)
        return template
    except Exception:
        db.rollback()
        assets_service.cleanup_staged_bundle(staged)
        raise


def import_into_template(
    db: Session,
    settings: Settings,
    template: NotebookTemplate,
    *,
    expected_revision: int,
    imported: ImportedNotebook,
) -> NotebookTemplate:
    assets_service = StudioAssetManifestService.from_settings(settings)
    staged = assets_service.stage_bundle(
        template.id, expected_revision + 1, imported.assets
    )
    binding: DraftAssetBindingPlan | None = None
    try:
        binding = assets_service.bind_draft_assets(
            db,
            template,
            revision=expected_revision + 1,
            staged=staged,
            created_by_id=template.owner_id,
        )
        updated = atomic_replace_draft(
            db,
            template,
            expected_revision,
            imported.cells,
            metadata=imported.metadata,
            assets_dir=staged.prefix,
        )
        if binding is not None:
            assets_service.finalize_draft_assets(db, binding)
        return updated
    except Exception:
        db.rollback()
        assets_service.cleanup_staged_bundle(staged)
        raise


def canonical_snapshot(cells: list[dict], metadata: dict) -> bytes:
    return json.dumps(
        {"cells": cells, "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def publish_template(
    db: Session,
    settings: Settings,
    template_id: int,
    user: User,
    *,
    activate: bool = True,
    commit: bool = True,
) -> NotebookTemplateVersion:
    template = db.scalar(
        select(NotebookTemplate)
        .where(NotebookTemplate.id == template_id)
        .with_for_update()
    )
    if not template:
        raise api_error(404, "TEMPLATE_NOT_FOUND", "Notebook 模板不存在")
    require_manager(template, user)
    if activate:
        from app.models import LabAssessment
        active_lab = db.scalar(select(LabAssessment.id).join(
            NotebookTemplateVersion, NotebookTemplateVersion.id == LabAssessment.template_version_id
        ).where(NotebookTemplateVersion.template_id == template.id, LabAssessment.status == "published").limit(1))
        if active_lab:
            raise api_error(409, "LAB_ASSESSMENT_PUBLISH_REQUIRED", "该实验已启用自动评测，请通过实验配置试评后发布新版本")
    # The draft already owns an exact environment binding.  It may be a
    # historical version after a profile publication/rollback, so publishing
    # the notebook checks image availability without requiring the version to
    # remain teacher-selectable.  Publish-time failures are state conflicts;
    # actual preview/runtime failures keep the 503 runnable gate.
    from app.services.environment_service import require_publishable_version

    if activate and template.draft_environment_version_id is not None:
        require_publishable_version(db, template.draft_environment_version_id)
    latest = db.scalar(
        select(func.max(NotebookTemplateVersion.version_number)).where(
            NotebookTemplateVersion.template_id == template.id
        )
    )
    version_number = (latest or 0) + 1
    cells = copy.deepcopy(_normalize_cells(template.draft_cells))
    metadata = copy.deepcopy(template.draft_metadata or {})
    digest = hashlib.sha256(canonical_snapshot(cells, metadata)).hexdigest()
    assets_service = StudioAssetManifestService.from_settings(settings)
    asset_plan: VersionAssetBindingPlan | None = None
    try:
        version = NotebookTemplateVersion(
            template_id=template.id,
            version_number=version_number,
            sha256=digest,
            cells=cells,
            cell_order=[cell["id"] for cell in cells],
            notebook_metadata=metadata,
            assets_dir=None,
            published_by_id=user.id,
            # 从草稿复制不可变环境快照；已发布版本与已开始实验不随新发布自动升级
            environment_version_id=template.draft_environment_version_id,
            import_policy_mode=template.draft_import_policy_mode,
            allowed_imports=list(template.draft_allowed_imports or []),
        )
        db.add(version)
        db.flush()
        asset_plan = assets_service.create_version_snapshot(
            db,
            template_id=template.id,
            version_id=version.id,
            version_number=version_number,
            legacy_prefix=template.draft_assets_dir,
            created_by_id=user.id,
        )
        version.assets_dir = asset_plan.prefix
        if activate:
            template.current_version_id = version.id
            template.status = "published"
        if commit:
            db.commit()
            db.refresh(version)
        return version
    except IntegrityError:
        db.rollback()
        assets_service.cleanup_version_snapshot(asset_plan)
        raise api_error(409, "PUBLISH_CONFLICT", "并发发布冲突，请重试")
    except StorageNotFound as exc:
        db.rollback()
        assets_service.cleanup_version_snapshot(asset_plan)
        raise api_error(500, "ASSET_MISSING", "草稿资源不存在") from exc
    except StorageConflict as exc:
        db.rollback()
        assets_service.cleanup_version_snapshot(asset_plan)
        raise api_error(409, "PUBLISH_CONFLICT", "版本资源已存在") from exc
    except StorageError as exc:
        db.rollback()
        assets_service.cleanup_version_snapshot(asset_plan)
        raise api_error(500, "ASSET_STORAGE_FAILED", "版本资源保存失败") from exc
    except Exception:
        db.rollback()
        assets_service.cleanup_version_snapshot(asset_plan)
        raise


def list_versions(
    db: Session,
    template: NotebookTemplate,
    settings: Settings | None = None,
) -> list[StudioVersionRead]:
    versions = db.scalars(
        select(NotebookTemplateVersion)
        .where(NotebookTemplateVersion.template_id == template.id)
        .order_by(NotebookTemplateVersion.version_number)
    ).all()
    assets_service = StudioAssetManifestService.from_settings(settings or Settings())
    result: list[StudioVersionRead] = []
    for version in versions:
        manifest_id, assets = assets_service.version_assets(
            db, version.id, version.assets_dir
        )
        result.append(_version_read(version, manifest_id=manifest_id, assets=assets))
    return result


def get_version(
    db: Session, template: NotebookTemplate, version_id: int
) -> NotebookTemplateVersion:
    version = db.get(NotebookTemplateVersion, version_id)
    if not version or version.template_id != template.id:
        raise api_error(404, "VERSION_NOT_FOUND", "模板版本不存在")
    return version


def export_notebook(cells: list[dict], metadata: dict) -> bytes:
    notebook_cells = []
    for definition in sorted(_normalize_cells(cells), key=lambda cell: cell["order"]):
        cell_metadata = {
            "dai": {
                "student_editable": definition["student_editable"],
                "source_hidden": definition["source_hidden"],
            }
        }
        if definition["type"] == "markdown":
            cell = nbformat.v4.new_markdown_cell(
                source=definition["source"],
                metadata=cell_metadata,
                id=definition["id"],
            )
        else:
            cell = nbformat.v4.new_code_cell(
                source=definition["source"],
                metadata=cell_metadata,
                execution_count=None,
                outputs=[],
                id=definition["id"],
            )
        notebook_cells.append(cell)
    notebook = nbformat.v4.new_notebook(
        cells=notebook_cells,
        metadata=copy.deepcopy(metadata),
    )
    return nbformat.writes(notebook).encode("utf-8")


def export_template_zip(
    db: Session,
    settings: Settings,
    template: NotebookTemplate,
    *,
    version_id: int | None,
    scope: str,
) -> tuple[bytes, str]:
    assets_service = StudioAssetManifestService.from_settings(settings)
    if version_id is not None:
        version = get_version(db, template, version_id)
        cells = version.cells
        metadata = version.notebook_metadata or {}
        _, assets = assets_service.version_assets(
            db, version.id, version.assets_dir
        )
        suffix = f"v{version.version_number}"
    elif scope == "draft":
        cells = template.draft_cells or []
        metadata = template.draft_metadata or {}
        _, assets = assets_service.draft_assets(db, template)
        suffix = "draft"
    else:
        raise api_error(422, "VERSION_REQUIRED", "请选择要导出的模板版本")
    notebook = export_notebook(cells, metadata)
    return assets_service.export_zip(notebook, assets), suffix


def preview_run(template: NotebookTemplate, user: User, cell_id: str, manager,
                db: Session | None = None):
    cells = _normalize_cells(template.draft_cells)
    requested = next(
        (
            cell
            for cell in cells
            if cell["id"] == cell_id
            and cell["type"] == "code"
            and not cell["source_hidden"]
        ),
        None,
    )
    if requested is None:
        raise api_error(
            404,
            "CELL_NOT_FOUND",
            "可见的代码 Cell 不存在（草稿可能尚未保存，请先保存草稿后重试）",
        )
    # Phase 5（计划 9.3）：草稿环境切换时 preview session 检测不一致并重建——
    # 传草稿环境的 digest 与环境版本，get_or_create_session 比对 Redis/内存记录自动重建。
    env_id = template.draft_environment_version_id
    image_ref = None
    if env_id is not None and db is not None:
        from app.services.environment_service import resolve_run_image_ref

        try:
            image_ref = resolve_run_image_ref(db, env_id)
        except Exception:
            raise api_error(503, "ENVIRONMENT_IMAGE_MISSING", "运行环境暂不可用，请稍后重试")
    session_id = preview_session_key(template.id, user.id)
    try:
        from app.services.lab_workspace import ensure_preview_kernel
        ensure_preview_kernel(manager, template, session_id, db, image_ref=image_ref,
                              environment_version_id=env_id)
    except Exception as exc:
        raise api_error(500, "KERNEL_ERROR", f"预览 Kernel 创建失败：{exc}")

    if not manager.is_template_initialized(session_id, template.draft_revision):
        hidden = sorted(
            (
                cell
                for cell in cells
                if cell["type"] == "code" and cell["source_hidden"]
            ),
            key=lambda cell: cell["order"],
        )
        for cell in hidden:
            try:
                manager.execute(session_id, cell["source"])
            except Exception:
                manager.destroy(session_id)
                raise api_error(
                    500,
                    "KERNEL_INIT_FAILED",
                    f"隐藏初始化 Cell {cell['id']} 执行失败，Kernel 已销毁",
                )
        try:
            manager.mark_template_initialized(
                session_id, template.draft_revision
            )
        except Exception as exc:
            manager.destroy(session_id)
            raise api_error(
                500,
                "KERNEL_INIT_FAILED",
                f"预览初始化状态保存失败，Kernel 已销毁：{exc}",
            )
    try:
        return manager.execute(session_id, requested["source"])
    except Exception as exc:
        raise api_error(500, "KERNEL_ERROR", f"预览执行失败：{exc}")
