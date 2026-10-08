"""统一实验 API — 模块管理 + 记录管理 + Cell 操作 + 提交（v5 统一模型）"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import get_current_user, get_db, require_roles, PaginationParams, pagination
from app.errors import api_error
from app.models import (
    AcademicTerm,
    Chapter,
    Course,
    CourseEnrollment,
    ExperimentModule,
    ExperimentRecord,
    ExperimentSubmission,
    Lesson,
    LabRecord, LabSubmission, LabRun,
    NotebookTemplate,
    NotebookTemplateVersion,
    TeachingClass,
    User,
)
from app.schemas import (
    ExperimentCellExecuteRequest,
    ExperimentCellMetadata,
    ExperimentCellExecuteResponse,
    ExperimentCellOut,
    ExperimentCellsSaveRequest,
    ExperimentModuleCreate,
    ExperimentModuleRead,
    ExperimentModuleUpdate,
    ExperimentRecordDetailResponse,
    ExperimentRecordRead,
    ExperimentReviewUpdate,
    ExperimentSubmissionDetailRead,
    ExperimentSubmissionFilterOption,
    ExperimentSubmissionFilterOptions,
    ExperimentSubmissionListRead,
    ExperimentSubmissionRead,
    ExperimentSubmissionSummary,
    ExperimentSubmitRequest,
    PaginatedResponse,
    StudentExperimentCatalogRead,
    StudentExperimentCatalogSummary,
    StudentExperimentModuleRead,
)
from app.schemas.studio import StudioTemplateCreate
from app.services.kernel_manager import get_kernel_manager
from app.services import studio_service

from app.models.teaching import CourseOffering, OfferingExperiment
from app.services import offering_service as offering_scope

router = APIRouter(prefix="/experiments", tags=["experiments"])

# ── 工具函数 ──────────────────────────────────────────────────


def _record_read(record: ExperimentRecord) -> ExperimentRecordRead:
    return ExperimentRecordRead(
        id=record.id,
        offering_id=record.offering_id,
        lesson_id=record.lesson_id,
        module_id=record.module_id,
        student_id=record.student_id,
        status=record.status,
        template_version_id=record.template_version_id,
        record_revision=record.record_revision,
        cells_sources=record.cells_sources,
        started_at=record.started_at,
        submitted_at=record.submitted_at,
    )


def _require_owner(record: ExperimentRecord, current_user: User) -> None:
    """确保当前用户是该实验记录的所有者"""
    if record.student_id != current_user.id:
        raise api_error(403, "FORBIDDEN", "无权操作此实验记录")


def _require_record_accessible(record: ExperimentRecord, current_user: User, db: Session) -> None:
    _require_owner(record, current_user)
    writing = db.info.get("teaching_request_method") not in ("GET", "HEAD")
    offering_scope.record_teaching_access(db, record, current_user, write=writing)
    offering_scope.require_delivery(db, record.offering_id, 'lesson' if record.lesson_id else 'module', record.lesson_id or record.module_id, current_user, write=writing)
    if record.lesson_id:
        _check_lesson_access(record.lesson_id, current_user.id, db)
    else:
        _check_module_access(record.module_id, db)


def _ensure_template_version(
    lesson_id: int | None,
    module_id: int | None,
    db: Session,
) -> NotebookTemplateVersion:
    if lesson_id:
        lesson = db.get(Lesson, lesson_id)
        if not lesson or not lesson.template_id:
            raise api_error(404, "TEMPLATE_NOT_FOUND", "该课时未关联 Notebook 模板")
        template = db.get(NotebookTemplate, lesson.template_id)
    elif module_id:
        module = db.get(ExperimentModule, module_id)
        if not module or not module.template_id:
            raise api_error(404, "TEMPLATE_NOT_FOUND", "该模块未关联 Notebook 模板")
        template = db.get(NotebookTemplate, module.template_id)
    else:
        raise api_error(400, "INVALID_ENTRY", "必须指定 lesson_id 或 module_id")

    if not template or not template.current_version_id:
        raise api_error(404, "VERSION_NOT_FOUND", "该模板尚未发布版本")

    version = db.get(NotebookTemplateVersion, template.current_version_id)
    if not version:
        raise api_error(404, "VERSION_NOT_FOUND", "模板版本不存在")
    if version.template_id != template.id:
        raise api_error(500, "VERSION_MISMATCH", "模板版本不匹配")
    return version


def _check_lesson_access(lesson_id: int, student_id: int, db: Session) -> Lesson:
    """校验学生有权限访问课时：课程 published + 选课 enrolled"""
    writing = db.info.get("teaching_request_method") not in ("GET", "HEAD")
    lesson = (db.scalar(select(Lesson).where(Lesson.id == lesson_id).with_for_update()
                        .execution_options(populate_existing=True)) if writing else db.get(Lesson, lesson_id))
    if not lesson:
        raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
    chapter = lesson.chapter
    if not chapter:
        raise api_error(404, "CHAPTER_NOT_FOUND", "章节不存在")
    course = chapter.course
    if not course or course.status != "published":
        raise api_error(403, "COURSE_NOT_AVAILABLE", "课程未发布")
    enrollment = db.scalar(
        select(CourseEnrollment).where(
            CourseEnrollment.course_id == course.id,
            CourseEnrollment.student_id == student_id,
            CourseEnrollment.status == "enrolled",
        )
    )
    from app.api.courses import can_access_course_content
    if lesson.status != "published" or not can_access_course_content(course, db.get(User, student_id), db):
        raise api_error(403, "LESSON_NOT_AVAILABLE", "课时未发布或无授课授权")
    return lesson


def _check_module_access(module_id: int, db: Session) -> ExperimentModule:
    writing = db.info.get("teaching_request_method") not in ("GET", "HEAD")
    module = (db.scalar(select(ExperimentModule).where(ExperimentModule.id == module_id).with_for_update()
                        .execution_options(populate_existing=True)) if writing else db.get(ExperimentModule, module_id))
    if not module:
        raise api_error(404, "EXPERIMENT_MODULE_NOT_FOUND", "实验模块不存在")
    if module.status != "published":
        raise api_error(403, "MODULE_NOT_PUBLISHED", "实验模块未发布")
    return module


# ── 实验模块 API（Notebook 风格 kernel 端点）───────────────────


def _managed_module_for_write(db, module_id, current_user):
    from app.services.teaching_query_policy import lock_current_actor
    actor = lock_current_actor(db, current_user)
    module = db.scalar(select(ExperimentModule).where(ExperimentModule.id == module_id)
                       .with_for_update().execution_options(populate_existing=True))
    if not module:
        raise api_error(404, "EXPERIMENT_MODULE_NOT_FOUND", "实验模块不存在")
    if actor.role not in ("teacher", "admin") or (actor.role == "teacher" and module.owner_id != actor.id):
        raise api_error(403, "FORBIDDEN", "无权管理其他用户创建的实验模块")
    return module


@router.get("/modules", response_model=PaginatedResponse)
def list_modules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(ExperimentModule)
    if current_user.role == "student":
        query = query.where(ExperimentModule.status == "published", ExperimentModule.id.in_(select(OfferingExperiment.entry_id).where(OfferingExperiment.entry_kind == "module", OfferingExperiment.status == "published", OfferingExperiment.offering_id.in_(offering_scope.student_offering_ids(db, current_user.id)))))
    elif current_user.role == "teacher":
        query = query.where(ExperimentModule.owner_id == current_user.id)
    modules = db.scalars(query.order_by(ExperimentModule.id)).all()
    return PaginatedResponse(
        items=[ExperimentModuleRead.model_validate(m) for m in modules],
        page=1, page_size=max(len(modules), 1), total=len(modules),
    )


@router.get("/modules/student-catalog", response_model=StudentExperimentCatalogRead)
def list_student_module_catalog(
    q: str | None = None,
    status_filter: Literal["not_started", "started", "submitted", "graded"] | None = Query(
        default=None, alias="status"
    ),
    sort: Literal["default", "recent_desc", "name_asc"] = "default",
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    """学生实验目录：合并已发布模块与当前学生自己的实验状态。"""
    page, page_size = pagination.page, pagination.page_size
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    learning_status = case(
        (ExperimentRecord.id.is_(None), "not_started"),
        (select(LabRecord.record_id).where(LabRecord.record_id == ExperimentRecord.id, LabRecord.selected_submission_id.is_not(None)).exists(), "graded"),
        (ExperimentRecord.status.in_(("graded", "completed")), "graded"),
        (ExperimentRecord.status == "submitted", "submitted"),
        else_="started",
    ).label("learning_status")

    join_condition = and_(
        ExperimentRecord.module_id == ExperimentModule.id,
        ExperimentRecord.student_id == current_user.id,
        ExperimentRecord.offering_id == OfferingExperiment.offering_id,
    )
    base_query = (
        select(
            ExperimentModule.id,
            ExperimentModule.name,
            OfferingExperiment.offering_id,
            Course.title.label("course_title"),
            AcademicTerm.name.label("term_name"),
            TeachingClass.name.label("class_name"),
            learning_status,
            ExperimentRecord.updated_at.label("last_learning_at"),
        )
        .select_from(OfferingExperiment)
        .join(ExperimentModule, and_(OfferingExperiment.entry_kind == "module",
                                    OfferingExperiment.entry_id == ExperimentModule.id))
        .join(CourseOffering, CourseOffering.id == OfferingExperiment.offering_id)
        .join(Course, Course.id == CourseOffering.course_id)
        .join(AcademicTerm, AcademicTerm.id == CourseOffering.academic_term_id)
        .join(TeachingClass, TeachingClass.id == CourseOffering.teaching_class_id)
        .outerjoin(ExperimentRecord, join_condition)
        .where(ExperimentModule.status == "published",
               OfferingExperiment.status == "published",
               OfferingExperiment.offering_id.in_(offering_scope.student_offering_ids(db, current_user.id)))
    )

    # Aggregate the scoped status projection rather than a correlated CASE.
    # MySQL ONLY_FULL_GROUP_BY otherwise rejects its LabRecord EXISTS branch.
    # This projection precedes search/filter/pagination, keeping totals stable.
    summary_source = base_query.subquery()
    summary_rows = db.execute(
        select(summary_source.c.learning_status, func.count(summary_source.c.id))
        .group_by(summary_source.c.learning_status)
    ).all()
    summary_counts = {key: value for key, value in summary_rows}
    summary = StudentExperimentCatalogSummary(
        total=sum(summary_counts.values()),
        not_started=summary_counts.get("not_started", 0),
        started=summary_counts.get("started", 0),
        submitted=summary_counts.get("submitted", 0),
        graded=summary_counts.get("graded", 0),
    )

    normalized_q = (q or "").strip()
    if normalized_q:
        base_query = base_query.where(ExperimentModule.name.ilike(f"%{normalized_q}%"))
    if status_filter:
        base_query = base_query.where(learning_status == status_filter)

    count_query = select(func.count()).select_from(base_query.order_by(None).subquery())
    total = db.scalar(count_query) or 0

    if sort == "recent_desc":
        # 跨方言排序：MySQL 8 不支持 NULLS LAST（1064），用 CASE 排序键实现同一语义——
        # 有学习记录的在前（键 0）、从未学过的（updated_at 为 NULL）沉底（键 1）。
        base_query = base_query.order_by(
            case((ExperimentRecord.updated_at.is_(None), 1), else_=0),
            ExperimentRecord.updated_at.desc(),
            ExperimentModule.id.asc(),
            OfferingExperiment.offering_id.asc(),
        )
    elif sort == "name_asc":
        base_query = base_query.order_by(ExperimentModule.name.asc(), ExperimentModule.id.asc(),
                                         OfferingExperiment.offering_id.asc())
    else:
        base_query = base_query.order_by(ExperimentModule.id.asc(), OfferingExperiment.offering_id.asc())

    rows = db.execute(
        base_query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return StudentExperimentCatalogRead(
        items=[
            StudentExperimentModuleRead(
                id=row.id,
                name=row.name,
                offering_id=row.offering_id,
                course_title=row.course_title,
                term_name=row.term_name,
                class_name=row.class_name,
                learning_status=row.learning_status,
                last_learning_at=row.last_learning_at,
            )
            for row in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
        summary=summary,
    )


@router.post("/modules", response_model=ExperimentModuleRead, status_code=status.HTTP_201_CREATED)
def create_module(
    payload: ExperimentModuleCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("teacher")),
):
    from app.services.teaching_query_policy import lock_current_actor
    current_user = lock_current_actor(db, current_user)
    if current_user.role == "teacher" and payload.template_id is not None:
        raise api_error(
            422,
            "MODULE_TEMPLATE_MANAGED",
            "教师实验的 Notebook 模板由系统自动创建",
        )

    try:
        module = ExperimentModule(
            **payload.model_dump(),
            owner_id=current_user.id,
        )
        db.add(module)
        db.flush()

        if current_user.role == "teacher":
            studio_service.create_template_record(
                db,
                StudioTemplateCreate(
                    name=module.name,
                    description=module.description,
                    module_id=module.id,
                ),
                current_user,
                settings,
            )

        db.commit()
        db.refresh(module)
        return module
    except Exception:
        db.rollback()
        raise


@router.post("/modules/{module_id}/template", response_model=ExperimentModuleRead)
def ensure_module_template(
    module_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("teacher")),
):
    """为历史孤立模块幂等初始化空白 Notebook 草稿。"""
    module = _managed_module_for_write(db, module_id, current_user)
    if module.owner_id != current_user.id:
        raise api_error(403, "FORBIDDEN", "只能初始化自己创建的实验模块")

    studio_service.ensure_module_template(db, module, current_user, settings)
    db.refresh(module)
    return module


@router.get("/modules/{module_id}", response_model=ExperimentModuleRead)
def get_module(
    module_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    module = db.get(ExperimentModule, module_id)
    if not module:
        raise api_error(404, "EXPERIMENT_MODULE_NOT_FOUND", "实验模块不存在")
    if current_user.role == "student":
        delivery = db.scalar(select(OfferingExperiment.id).where(OfferingExperiment.entry_kind == "module", OfferingExperiment.entry_id == module.id, OfferingExperiment.status == "published", OfferingExperiment.offering_id.in_(offering_scope.student_offering_ids(db, current_user.id))))
        if module.status != "published" or not delivery:
            raise api_error(403, "FORBIDDEN", "无权查看未分配本班的实验模块")
    return module


@router.patch("/modules/{module_id}", response_model=ExperimentModuleRead)
def patch_module(
    module_id: int,
    payload: ExperimentModuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "teacher")),
):
    """更新模块元数据：强类型 Schema，status 不可修改，未知字段拒绝。"""
    module = _managed_module_for_write(db, module_id, current_user)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(module, key, value)
    db.commit()
    db.refresh(module)
    return module


def _ensure_module_publishable(module: ExperimentModule, db: Session) -> None:
    """发布前校验：绑定可用模板且模板存在已发布版本。"""
    if module.template_id is None:
        raise api_error(422, "MODULE_TEMPLATE_REQUIRED", "发布前必须绑定 Notebook 模板")
    template = db.get(NotebookTemplate, module.template_id)
    if not template or not template.current_version_id:
        raise api_error(422, "MODULE_TEMPLATE_NOT_READY", "模板不存在或尚未发布版本")


@router.post("/modules/{module_id}/publish", response_model=ExperimentModuleRead)
def publish_module(
    module_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "teacher")),
):
    """发布模块：要求模板就绪；状态变更只能通过本端点。"""
    module = _managed_module_for_write(db, module_id, current_user)
    _ensure_module_publishable(module, db)
    module.status = "published"
    db.commit()
    db.refresh(module)
    return module


@router.post("/modules/{module_id}/unpublish", response_model=ExperimentModuleRead)
def unpublish_module(
    module_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "teacher")),
):
    """取消发布：published → draft，学生端立即不可见。"""
    module = _managed_module_for_write(db, module_id, current_user)
    if module.status != "published":
        raise api_error(409, "MODULE_NOT_PUBLISHED", "仅已发布的模块可取消发布")
    module.status = "draft"
    db.commit()
    db.refresh(module)
    return module


# ── 记录管理 ──────────────────────────────────────────────────


@router.post("/records/ensure-for-lesson/{lesson_id}", response_model=ExperimentRecordRead)
def ensure_record_for_lesson(
    lesson_id: int,
    offering_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    from app.services.account_service import lock_policy
    lock_policy(db)
    lesson = _check_lesson_access(lesson_id, current_user.id, db)
    offering = offering_scope.resolve_student_offering(db, lesson.chapter.course_id, current_user, offering_id)
    _, delivery = offering_scope.require_delivery(db, offering.id, "lesson", lesson_id, current_user)

    existing = db.scalar(
        select(ExperimentRecord).where(
            ExperimentRecord.lesson_id == lesson_id,
            ExperimentRecord.student_id == current_user.id,
            ExperimentRecord.offering_id == offering.id,
        )
    )
    if existing:
        return _record_read(existing)
    _, delivery = offering_scope.require_delivery(db, offering.id, "lesson", lesson_id, current_user, lock=True)
    if delivery.pending_assessment_id:
        raise api_error(409, "ASSESSMENT_REGRADING_PENDING", "本班评分标准正在更新，请等待教师完成重评")

    version = db.get(NotebookTemplateVersion, delivery.template_version_id)
    record = ExperimentRecord(
        offering_id=offering.id, scope_key=f"offering:{offering.id}",
        lesson_id=lesson_id, module_id=None,
        template_version_id=version.id,
        # Phase 5（计划 9.2）：从模板版本复制环境版本——已存在记录不因模板发布 v2 自动升级
        environment_version_id=version.environment_version_id,
        student_id=current_user.id,
        status="started",
        started_at=datetime.now(timezone.utc),
        cells_sources={
            cell["id"]: cell["source"]
            for cell in version.cells
            if (cell.get("type") == "code" or cell.get("student_editable", False)) and not cell.get("source_hidden")
        },
    )
    db.add(record)
    db.flush()
    from app.services.lab_assessment_service import bind_new_record
    bind_new_record(db, record)
    db.commit()
    db.refresh(record)
    return _record_read(record)


@router.post("/records/ensure-for-module/{module_id}", response_model=ExperimentRecordRead)
def ensure_record_for_module(
    module_id: int,
    offering_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    from app.services.account_service import lock_policy
    lock_policy(db)
    _check_module_access(module_id, db)
    candidates = list(db.scalars(select(OfferingExperiment.offering_id).where(OfferingExperiment.entry_kind == "module", OfferingExperiment.entry_id == module_id, OfferingExperiment.status == "published", OfferingExperiment.offering_id.in_(offering_scope.student_offering_ids(db, current_user.id)))))
    if offering_id is None and len(candidates) == 1:
        offering_id = candidates[0]
    if offering_id not in candidates:
        raise api_error(409, "OFFERING_REQUIRED", "请选择已发布本实验的授课实例")
    offering = offering_scope.require_offering(db, offering_id, current_user, write=True)
    _, delivery = offering_scope.require_delivery(db, offering.id, "module", module_id, current_user)

    existing = db.scalar(
        select(ExperimentRecord).where(
            ExperimentRecord.module_id == module_id,
            ExperimentRecord.student_id == current_user.id,
            ExperimentRecord.offering_id == offering.id,
        )
    )
    if existing:
        return _record_read(existing)
    _, delivery = offering_scope.require_delivery(db, offering.id, "module", module_id, current_user, lock=True)
    if delivery.pending_assessment_id:
        raise api_error(409, "ASSESSMENT_REGRADING_PENDING", "本班评分标准正在更新，请等待教师完成重评")

    version = db.get(NotebookTemplateVersion, delivery.template_version_id)
    record = ExperimentRecord(
        offering_id=offering.id, scope_key=f"offering:{offering.id}",
        lesson_id=None, module_id=module_id,
        template_version_id=version.id,
        # Phase 5（计划 9.2）：从模板版本复制环境版本——已存在记录不因模板发布 v2 自动升级
        environment_version_id=version.environment_version_id,
        student_id=current_user.id,
        status="started",
        started_at=datetime.now(timezone.utc),
        cells_sources={
            cell["id"]: cell["source"]
            for cell in version.cells
            if (cell.get("type") == "code" or cell.get("student_editable", False)) and not cell.get("source_hidden")
        },
    )
    db.add(record)
    db.flush()
    from app.services.lab_assessment_service import bind_new_record
    bind_new_record(db, record)
    db.commit()
    db.refresh(record)
    return _record_read(record)


# ── Notebook 风格实验 API ──────────────────────────────────────


@router.get("/records/{record_id}", response_model=ExperimentRecordDetailResponse)
def get_record_detail(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.get(ExperimentRecord, record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")
    _require_record_accessible(record, current_user, db)

    version = db.get(NotebookTemplateVersion, record.template_version_id)
    if not version:
        raise api_error(500, "VERSION_MISSING", "模板版本丢失")

    cells_out = []
    for cell in version.cells:
        if cell.get("source_hidden"):
            continue
        cell_id = cell["id"]
        outputs = record.cells_outputs.get(cell_id)
        cells_out.append(ExperimentCellOut(
            id=cell_id,
            type=cell.get("type", "code"),
            source=record.cells_sources.get(cell_id, cell.get("source", "")),
            order=cell.get("order", 0),
            student_editable=cell.get("student_editable", cell.get("type", "code") == "code"),
            outputs=outputs,
            is_running=False,
        ))

    entry_name = ""
    entry_description = None
    if record.lesson_id and record.lesson:
        entry_name = record.lesson.title
    elif record.module_id and record.module:
        entry_name = record.module.name
        entry_description = record.module.description

    visible_count = sum(
        1 for cell in cells_out
        if cell.id in record.cells_outputs
        and not any(c.get("id") == cell.id and c.get("source_hidden") for c in version.cells)
    )

    # Phase 5：学生可见环境摘要（模板版本绑定的环境 + 模板发布时冻结的 import 策略）
    from app.services.environment_service import public_environment_summary
    from app.services.import_policy import ImportPolicy

    env_summary = None
    if version.environment_version_id is not None:
        env_summary = public_environment_summary(
            db, version.environment_version_id,
            ImportPolicy.from_mode(version.import_policy_mode, list(version.allowed_imports or [])),
        )

    return ExperimentRecordDetailResponse(
        id=record.id, offering_id=record.offering_id, lesson_id=record.lesson_id, module_id=record.module_id,
        lab_enabled=db.get(LabRecord, record.id) is not None,
        student_id=record.student_id, status=record.status,
        template_version_id=record.template_version_id,
        record_revision=record.record_revision,
        entry_name=entry_name, entry_description=entry_description,
        cells=cells_out, execution_count=visible_count,
        environment_summary=env_summary,
    )


@router.get("/records", response_model=PaginatedResponse)
def list_records(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(ExperimentRecord)
    if current_user.role == "student":
        query = query.where(ExperimentRecord.student_id == current_user.id)
    records = db.scalars(query.order_by(ExperimentRecord.id)).all()

    # 过滤已无权访问的记录（退课/课程下架等）
    visible = []
    for r in records:
        try:
            _require_record_accessible(r, current_user, db)
            visible.append(r)
        except Exception:
            pass

    return PaginatedResponse(
        items=[_record_read(r) for r in visible],
        page=1, page_size=max(len(visible), 1), total=len(visible),
    )


@router.put("/records/{record_id}/cells")
def save_cells(
    record_id: int,
    payload: ExperimentCellsSaveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.get(ExperimentRecord, record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")
    _require_record_accessible(record, current_user, db)

    if payload.record_revision != record.record_revision:
        raise api_error(409, "REVISION_CONFLICT", "记录已被他人修改，请刷新后重试")

    version = db.get(NotebookTemplateVersion, record.template_version_id)
    if version:
        editable_ids = {
            cell["id"] for cell in version.cells
            if cell.get("type") in ("code", "markdown")
            and cell.get("student_editable", cell.get("type") == "code")
            and not cell.get("source_hidden")
        }
        for cell_id in payload.cells:
            if cell_id not in editable_ids:
                raise api_error(403, "CELL_NOT_EDITABLE", f"Cell {cell_id} 不允许修改")

    merged = dict(record.cells_sources)
    merged.update(payload.cells)
    changed = db.execute(update(ExperimentRecord).where(
        ExperimentRecord.id == record_id,
        ExperimentRecord.record_revision == payload.record_revision,
    ).values(cells_sources=merged, record_revision=payload.record_revision + 1)).rowcount
    if changed != 1:
        db.rollback()
        raise api_error(409, "REVISION_CONFLICT", "记录已被其他操作修改，请刷新后重试")
    db.commit()
    return {"record_id": record_id, "record_revision": payload.record_revision + 1}


def _visible_execution_count(record: ExperimentRecord, version) -> int:
    """计算可见 cell 的最大 execution_count（排除 hidden cells）"""
    if not version:
        return 0
    hidden_ids = {c["id"] for c in version.cells if c.get("source_hidden")}
    max_count = 0
    for cid, output in record.cells_outputs.items():
        if cid not in hidden_ids and isinstance(output, dict):
            cnt = output.get("execution_count", 0)
            if cnt > max_count:
                max_count = cnt
    return max_count


@router.post("/records/{record_id}/cells/{cell_id}/execute", response_model=ExperimentCellExecuteResponse)
def execute_cell(
    record_id: int,
    cell_id: str,
    payload: ExperimentCellExecuteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.get(ExperimentRecord, record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")
    _require_record_accessible(record, current_user, db)

    version = db.get(NotebookTemplateVersion, record.template_version_id)
    if version:
        cell_def = next((c for c in version.cells if c["id"] == cell_id), None)
        if cell_def is None:
            raise api_error(404, "CELL_NOT_FOUND", f"Cell {cell_id} 不存在")
        if cell_def.get("source_hidden"):
            raise api_error(403, "CELL_HIDDEN", f"Cell {cell_id} 不可直接执行")
        if cell_def.get("type") != "code":
            raise api_error(400, "CELL_NOT_CODE", f"Cell {cell_id} 不是代码 cell")
        if not cell_def.get("student_editable", True):
            expected = record.cells_sources.get(cell_id, cell_def.get("source", ""))
            if payload.code != expected:
                raise api_error(403, "CELL_READONLY", f"Cell {cell_id} 为只读，不允许修改源码")

    if len(payload.code) > 50000:
        raise api_error(400, "CODE_TOO_LONG", "代码过长，最多 50000 字符")

    # Phase 5（计划 9.3/9.4）：解析实验运行环境（记录快照 digest + 模板版本 import 策略）
    env_id = getattr(record, "environment_version_id", None)
    image_ref = None
    installed: set[str] = set()
    if env_id is not None:
        from app.services.environment_service import (
            installed_imports_for_version,
            resolve_run_image_ref,
        )

        try:
            image_ref = resolve_run_image_ref(db, env_id)
        except Exception:
            raise api_error(503, "ENVIRONMENT_IMAGE_MISSING",
                            "运行环境暂不可用，请稍后重试")
        installed = installed_imports_for_version(db, env_id)

    from app.services.import_policy import ImportPolicy, classify_imports

    policy = ImportPolicy.from_mode(
        version.import_policy_mode if version else "unrestricted",
        list(version.allowed_imports or []) if version else [],
    )

    # import 预检（计划 9.4）：只分析学生代码；IMPORT_NOT_ALLOWED → 422，IMPORT_NOT_INSTALLED → 500
    if policy.restricted:
        diagnostics = classify_imports(payload.code, policy, installed)
        if diagnostics:
            diagnostic = diagnostics[0]
            if diagnostic.code == "IMPORT_NOT_ALLOWED":
                raise api_error(422, "IMPORT_NOT_ALLOWED", diagnostic.message)
            if diagnostic.code == "IMPORT_NOT_INSTALLED":
                raise api_error(500, "IMPORT_NOT_INSTALLED", diagnostic.message)

    execution_revision = record.record_revision
    execution_context = (record.offering_id, record.student_id, record.template_version_id,
                         record.environment_version_id)
    # Admission metadata was checked with current locks. Release every policy,
    # account and teaching lock before workspace copying or Docker execution.
    db.commit()
    _init_hidden_cells_once(record, version, db)

    try:
        km = get_kernel_manager()
        from app.services.lab_workspace import ensure_record_kernel
        ensure_record_kernel(km, record, version, db, image_ref=image_ref,
                             environment_version_id=env_id)
        result = km.execute(record_id, payload.code)
    except RuntimeError as e:
        raise api_error(409, "KERNEL_BUSY", str(e))
    except Exception as e:
        raise api_error(500, "KERNEL_ERROR", f"执行失败：{e}")

    # Workspace preparation may have opened a read snapshot. Completion starts
    # a new short metadata transaction and rechecks current account and class
    # authority before storing or returning the execution's feedback.
    db.rollback()
    _require_record_accessible(record, current_user, db)
    if (record.record_revision != execution_revision
            or (record.offering_id, record.student_id, record.template_version_id,
                record.environment_version_id) != execution_context):
        raise api_error(409, "REVISION_CONFLICT", "作答或授课归属在运行期间发生变化，请刷新后重试")

    # Kernel 输出中的 ModuleNotFoundError 兜底归类（计划 9.4）：不把裸 traceback 当策略错误
    kernel_diagnostic = _classify_kernel_module_not_found(result["outputs"], policy, installed)

    outputs = record.cells_outputs.copy()
    execution_count = _visible_execution_count(record, version) + 1
    final_outputs = result["outputs"]
    if kernel_diagnostic is not None:
        final_outputs = _replace_error_output(final_outputs, kernel_diagnostic.message)
    outputs[cell_id] = {
        "execution_count": execution_count,
        "outputs": final_outputs,
        "execution_time_ms": result["execution_time_ms"],
    }
    record.cells_outputs = outputs
    db.commit()

    return ExperimentCellExecuteResponse(
        outputs=final_outputs,
        execution_time_ms=result["execution_time_ms"],
        execution_count=execution_count,
        diagnostic=kernel_diagnostic,
    )


def _classify_kernel_module_not_found(outputs: list[dict], policy, installed: set[str]):
    """Kernel 输出中的 ModuleNotFoundError 兜底归类（计划 9.4）。

    预检已拦截策略错误；若代码绕过静态分析（动态拼接 import 名等）在 Kernel 内触发
    ModuleNotFoundError，则按白名单/安装清单归为 IMPORT_NOT_ALLOWED 或
    IMPORT_NOT_INSTALLED，不把裸 traceback 作为策略错误直接返回。
    """
    from app.schemas.environments import ImportDiagnosticRead
    import re as _re

    if not policy.restricted:
        return None
    for out in outputs:
        if out.get("msg_type") != "error":
            continue
        text = (out.get("content") or {}).get("text", "") or ""
        match = _re.search(r"No module named '([^']+)'", text)
        if not match:
            continue
        module = match.group(1).split(".")[0]
        if module not in policy.allowed_imports:
            return ImportDiagnosticRead(
                code="IMPORT_NOT_ALLOWED",
                module=module,
                message=f"导入限制：{module} 未在本实验允许范围内",
            )
        if module not in installed:
            return ImportDiagnosticRead(
                code="IMPORT_NOT_INSTALLED",
                module=module,
                message=f"环境配置错误：本实验允许 {module}，但当前环境未安装",
            )
    return None


def _replace_error_output(outputs: list[dict], message: str) -> list[dict]:
    """把第一个 error 输出替换为无 traceback 的合成中文诊断输出。"""
    replaced = False
    result = []
    for out in outputs:
        if not replaced and out.get("msg_type") == "error":
            result.append({"msg_type": "error", "content": {"text": message}})
            replaced = True
        else:
            result.append(out)
    if not replaced:
        result.append({"msg_type": "error", "content": {"text": message}})
    return result


def _init_hidden_cells_once(record: ExperimentRecord, version, db: Session) -> None:
    """仅在 kernel 首次创建时按 order 执行 hidden cells。成功后在 Redis + session 标记。"""
    if not version:
        return
    hidden_cells = sorted(
        [c for c in version.cells if c.get("source_hidden") and c.get("type") == "code"],
        key=lambda c: c.get("order", 0),
    )
    if not hidden_cells:
        return

    km = get_kernel_manager()
    env_id = getattr(record, "environment_version_id", None)
    image_ref = None
    if env_id is not None:
        from app.services.environment_service import resolve_run_image_ref

        try:
            image_ref = resolve_run_image_ref(db, env_id)
        except Exception:
            raise api_error(503, "ENVIRONMENT_IMAGE_MISSING", "运行环境暂不可用，请稍后重试")
    try:
        from app.services.lab_workspace import ensure_record_kernel
        ensure_record_kernel(km, record, version, db, image_ref=image_ref,
                             environment_version_id=env_id)
    except Exception as e:
        raise api_error(500, "KERNEL_INIT_FAILED", f"Kernel 创建失败：{e}")

    version_id = record.template_version_id
    if km.is_template_initialized(record.id, version_id):
        return

    for cell in hidden_cells:
        try:
            km.execute(record.id, cell["source"])
        except Exception:
            km.destroy(record.id)
            raise api_error(500, "KERNEL_INIT_FAILED",
                            f"隐藏初始化 cell {cell['id']} 执行失败，Kernel 已销毁")

    try:
        km.mark_template_initialized(record.id, version_id)
    except Exception as e:
        km.destroy(record.id)
        raise api_error(
            500,
            "KERNEL_INIT_FAILED",
            f"Kernel 初始化状态保存失败，Kernel 已销毁：{e}",
        )


@router.post("/records/{record_id}/interrupt")
def interrupt_kernel(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.get(ExperimentRecord, record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")
    _require_record_accessible(record, current_user, db)

    db.commit()
    km = get_kernel_manager()
    from app.services.kernel_manager import KernelManager
    from sqlalchemy.orm import sessionmaker
    try:
        if isinstance(km, KernelManager):
            km.interrupt(record_id, resource_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False))
        else:
            km.interrupt(record_id)
    except RuntimeError as exc:
        raise api_error(409, "KERNEL_BUSY", str(exc))
    db.rollback()
    _require_record_accessible(record, current_user, db)
    db.commit()
    return {"status": "interrupted"}


@router.post("/records/{record_id}/restart")
def restart_kernel(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.get(ExperimentRecord, record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")
    _require_record_accessible(record, current_user, db)

    record.cells_outputs = {}
    db.commit()

    km = get_kernel_manager()
    from app.services.environment_service import resolve_run_image_ref
    from app.services.lab_workspace import ensure_record_kernel
    env_id = record.environment_version_id
    version = db.get(NotebookTemplateVersion, record.template_version_id)
    try:
        image_ref = resolve_run_image_ref(db, env_id) if env_id is not None else None
        ensure_record_kernel(km, record, version, db, image_ref=image_ref,
                             environment_version_id=env_id, restart=True)
    except RuntimeError as exc:
        raise api_error(409, "KERNEL_BUSY", str(exc))
    db.rollback()
    _require_record_accessible(record, current_user, db)
    db.commit()
    return {"status": "restarted"}


# ── 实验提交（快照）─────────────────────────────────────────────


@router.post("/records/{record_id}/submit", response_model=ExperimentSubmissionRead, status_code=status.HTTP_201_CREATED)
def submit_record(
    record_id: int,
    payload: ExperimentSubmitRequest,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    """学生提交实验：保存 cells 不可变快照，更新记录状态。

    幂等：同一 client_request_id 多次请求返回已有提交。
    """
    from app.services.account_service import lock_policy
    lock_policy(db)
    client_request_id = str(payload.client_request_id)  # UUID → str（兼容 SQLite）

    # 1. 加载 record 并验证所有权（幂等检查之前），防止越权
    record = db.get(ExperimentRecord, record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")
    _require_record_accessible(record, current_user, db)

    # 2. 快速幂等检查（无锁）：同一 client_request_id 已存在 → 直接返回
    existing = db.scalar(
        select(ExperimentSubmission).where(
            ExperimentSubmission.record_id == record_id,
            ExperimentSubmission.client_request_id == client_request_id,
        )
    )
    if existing:
        response = ExperimentSubmissionRead.model_validate(existing)
        lab = db.get(LabSubmission, existing.id)
        response.lab_enabled = lab is not None
        run = db.get(LabRun, lab.current_run_id) if lab and lab.current_run_id else None
        response.lab_status = run.status if run else None
        return response

    if db.get(LabRecord, record.id):
        from app.services.lab_submission_service import submit_lab
        _require_record_accessible(record, current_user, db)
        if not settings.lab_enabled:
            raise api_error(503, "LAB_DISABLED", "实验评测暂未启用")
        result = submit_lab(db, record, current_user, payload, settings)
        db.commit()
        response = ExperimentSubmissionRead.model_validate(result)
        response.lab_enabled = True
        response.lab_status = "queued"
        return response

    # 3. 仅终态（graded/completed）禁止重新提交
    if record.status not in ("started", "submitted"):
        raise api_error(400, "ALREADY_GRADED", "实验已评分，不能再次提交")

    # 4. 锁定 record 行，防止并发提交计算出相同 attempt_number
    record = db.scalar(
        select(ExperimentRecord)
        .where(ExperimentRecord.id == record_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    _require_record_accessible(record, current_user, db)
    _, delivery = offering_scope.require_delivery(db, record.offering_id,
        'lesson' if record.lesson_id else 'module', record.lesson_id or record.module_id,
        current_user, lock=True)
    if delivery.pending_assessment_id:
        raise api_error(409, 'ASSESSMENT_SWITCH_PENDING', '本班评测版本正在整批核对，请稍后正式提交')

    # 5. 锁内二次幂等检查（另一个请求可能在步骤2和步骤4之间插入了相同 client_request_id）
    existing_locked = db.scalar(
        select(ExperimentSubmission).where(
            ExperimentSubmission.record_id == record_id,
            ExperimentSubmission.client_request_id == client_request_id,
        )
    )
    if existing_locked:
        return ExperimentSubmissionRead.model_validate(existing_locked)

    # 6. 锁内计算下一个 attempt_number（避免幻读）
    max_attempt = db.scalar(
        select(ExperimentSubmission.attempt_number)
        .where(ExperimentSubmission.record_id == record_id)
        .order_by(ExperimentSubmission.attempt_number.desc())
    )
    attempt_number = (max_attempt or 0) + 1

    # 7. 深复制源码与输出快照（确保后续运行/保存不改变历史提交）
    import copy
    version = db.get(NotebookTemplateVersion, record.template_version_id)
    snapshot = copy.deepcopy(record.cells_sources)
    if version:
        snapshot = {
            cell["id"]: record.cells_sources.get(cell["id"], cell.get("source", ""))
            for cell in version.cells
            if not cell.get("source_hidden")
        }
    outputs_snapshot = copy.deepcopy(record.cells_outputs)

    now = datetime.now(timezone.utc)
    submission = ExperimentSubmission(
        record_id=record_id,
        attempt_number=attempt_number,
        client_request_id=client_request_id,
        cells_snapshot=snapshot,
        outputs_snapshot=outputs_snapshot,
        submitted_at=now,
    )

    db.add(submission)
    record.status = "submitted"
    record.submitted_at = now
    record.record_revision += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        _require_record_accessible(record, current_user, db)
        # 并发冲突（SQLite 无真正行锁）：另一个线程先插入了相同 attempt_number
        # 重新获取锁并计算 attempt_number 重试
        db.execute(
            select(ExperimentRecord)
            .where(ExperimentRecord.id == record_id)
            .with_for_update()
        )
        # 锁内重新计算
        max_attempt_retry = db.scalar(
            select(ExperimentSubmission.attempt_number)
            .where(ExperimentSubmission.record_id == record_id)
            .order_by(ExperimentSubmission.attempt_number.desc())
        )
        attempt_number = (max_attempt_retry or 0) + 1
        submission.attempt_number = attempt_number
        # 重新检查是否已有相同 client_request_id 的提交
        existing_retry = db.scalar(
            select(ExperimentSubmission).where(
                ExperimentSubmission.record_id == record_id,
                ExperimentSubmission.client_request_id == client_request_id,
            )
        )
        if existing_retry:
            return ExperimentSubmissionRead.model_validate(existing_retry)
        db.add(submission)
        record.status = "submitted"
        record.submitted_at = now
        record.record_revision += 1
        db.commit()

    db.refresh(submission)
    return ExperimentSubmissionRead.model_validate(submission)


def _submission_records_query(*columns):
    """提交列表统一的记录上下文，供权限、筛选与筛选项复用。"""
    return (
        select(*columns)
        .select_from(ExperimentRecord)
        .join(User, ExperimentRecord.student_id == User.id)
        .outerjoin(Lesson, ExperimentRecord.lesson_id == Lesson.id)
        .outerjoin(Chapter, Lesson.chapter_id == Chapter.id)
        .outerjoin(Course, Course.id == func.coalesce(Chapter.course_id, select(CourseOffering.course_id).where(CourseOffering.id == ExperimentRecord.offering_id).correlate(ExperimentRecord).scalar_subquery()))
        .outerjoin(ExperimentModule, ExperimentRecord.module_id == ExperimentModule.id)
    )


def _apply_submission_visibility(query, current_user: User):
    if current_user.role == "student":
        return query.where(ExperimentRecord.student_id == current_user.id)
    if current_user.role == "teacher":
        # 教师可见两类提交：自己课程的课时实验（lesson→chapter→course 链路）、
        # 自己创建的实验模块（module 链路，owner_id 归属）。
        # 模块实验的 lesson_id 为 NULL，仅按 Course.teacher_id 过滤会被全部排除。
        return query.where(offering_scope.teacher_record_condition(current_user.id))
    if current_user.role == "admin":
        return query
    # get_current_user rejects unsupported persisted roles; keep this helper
    # fail-closed as an additional guard for direct/unit-level callers.
    return query.where(ExperimentRecord.id == -1)


@router.get("/submissions", response_model=ExperimentSubmissionListRead)
def list_submissions(
    record_id: int | None = None,
    offering_id: int | None = None,
    q: str | None = None,
    course_id: int | None = None,
    entry_id: int | None = None,
    review_status: Literal["pending", "graded"] | None = None,
    sort: Literal["submitted_desc", "submitted_asc"] = "submitted_desc",
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看实验提交列表。

    权限：
    - 学生：仅自己的提交
    - 教师：仅自己课程（通过 lesson→chapter→course）的提交
    - 管理员：全部
    """
    page, page_size = pagination.page, pagination.page_size
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    # 汇总与筛选项只受权限（及 record_id 上下文）影响，不跟随当前筛选变化。
    scope_ids = _apply_submission_visibility(
        _submission_records_query(ExperimentRecord.id), current_user
    )
    if offering_id is not None:
        offering_scope.require_offering(db, offering_id, current_user)
        scope_ids = scope_ids.where(ExperimentRecord.offering_id == offering_id)
    if record_id is not None:
        scope_ids = scope_ids.where(ExperimentRecord.id == record_id)

    summary_total = db.scalar(
        select(func.count()).select_from(ExperimentSubmission).where(
            ExperimentSubmission.record_id.in_(scope_ids)
        )
    ) or 0
    summary_pending = db.scalar(
        select(func.count()).select_from(ExperimentSubmission).where(
            ExperimentSubmission.record_id.in_(scope_ids),
            ExperimentSubmission.score.is_(None),
        )
    ) or 0
    summary_graded = summary_total - summary_pending

    option_query = _apply_submission_visibility(
        _submission_records_query(
            Course.id,
            Course.title,
            Lesson.id,
            Lesson.title,
            ExperimentModule.id,
            ExperimentModule.name,
        ),
        current_user,
    )
    if record_id is not None:
        option_query = option_query.where(ExperimentRecord.id == record_id)
    option_rows = db.execute(option_query.distinct()).all()
    course_options = {}
    entry_options = {}
    for course_row_id, course_title, lesson_id, lesson_title, module_id, module_name in option_rows:
        if course_row_id is not None:
            course_options[course_row_id] = course_title
        if lesson_id is not None:
            entry_options[lesson_id] = lesson_title
        elif module_id is not None:
            entry_options[module_id] = module_name

    visible_ids = _apply_submission_visibility(
        _submission_records_query(ExperimentRecord.id), current_user
    )
    if offering_id is not None:
        visible_ids = visible_ids.where(ExperimentRecord.offering_id == offering_id)
    if record_id is not None:
        visible_ids = visible_ids.where(ExperimentRecord.id == record_id)
    if course_id is not None:
        visible_ids = visible_ids.where(Course.id == course_id)
    if entry_id is not None:
        visible_ids = visible_ids.where(or_(
            ExperimentRecord.lesson_id == entry_id,
            ExperimentRecord.module_id == entry_id,
        ))
    normalized_q = (q or "").strip()
    if normalized_q:
        pattern = f"%{normalized_q}%"
        visible_ids = visible_ids.where(or_(
            User.real_name.ilike(pattern),
            User.username.ilike(pattern),
            Lesson.title.ilike(pattern),
            ExperimentModule.name.ilike(pattern),
        ))

    query = select(ExperimentSubmission).where(
        ExperimentSubmission.record_id.in_(visible_ids)
    )
    count_query = select(func.count()).select_from(ExperimentSubmission).where(
        ExperimentSubmission.record_id.in_(visible_ids)
    )

    if record_id is not None:
        query = query.where(ExperimentSubmission.record_id == record_id)
        count_query = count_query.where(ExperimentSubmission.record_id == record_id)
    if review_status == "pending":
        query = query.where(ExperimentSubmission.score.is_(None))
        count_query = count_query.where(ExperimentSubmission.score.is_(None))
    elif review_status == "graded":
        query = query.where(ExperimentSubmission.score.is_not(None))
        count_query = count_query.where(ExperimentSubmission.score.is_not(None))

    total = db.scalar(count_query) or 0
    order_by = (
        ExperimentSubmission.submitted_at.asc()
        if sort == "submitted_asc"
        else ExperimentSubmission.submitted_at.desc()
    )
    submissions = db.scalars(
        query.order_by(order_by, ExperimentSubmission.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    # 批量填充学生姓名和入口名称
    record_ids = [s.record_id for s in submissions]
    records_map = {}
    if record_ids:
        records = db.scalars(
            select(ExperimentRecord).where(ExperimentRecord.id.in_(record_ids))
        ).all()
        records_map = {r.id: r for r in records}

    student_ids = list(set(r.student_id for r in records_map.values()))
    students_map = {}
    if student_ids:
        students = db.scalars(
            select(User).where(User.id.in_(student_ids))
        ).all()
        students_map = {u.id: u for u in students}

    lesson_ids = [r.lesson_id for r in records_map.values() if r.lesson_id]
    module_ids = [r.module_id for r in records_map.values() if r.module_id]
    lesson_map = {}
    module_map = {}
    chapter_map = {}
    course_map = {}
    if lesson_ids:
        lessons = db.scalars(
            select(Lesson).where(Lesson.id.in_(lesson_ids))
        ).all()
        lesson_map = {l.id: l for l in lessons}
        chapter_ids = list({lesson.chapter_id for lesson in lessons})
        chapters = db.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids))).all()
        chapter_map = {chapter.id: chapter for chapter in chapters}
        course_ids = list({chapter.course_id for chapter in chapters})
        courses = db.scalars(select(Course).where(Course.id.in_(course_ids))).all()
        course_map = {course.id: course for course in courses}
    if module_ids:
        modules = db.scalars(
            select(ExperimentModule).where(ExperimentModule.id.in_(module_ids))
        ).all()
        module_map = {m.id: m for m in modules}

    items = []
    for s in submissions:
        item = ExperimentSubmissionRead.model_validate(s)
        lab = db.get(LabSubmission, s.id)
        item.lab_enabled = lab is not None
        run = db.get(LabRun, lab.current_run_id) if lab and lab.current_run_id else None
        item.lab_status = "needs_review" if lab and lab.review_requested else run.status if run else None
        record = records_map.get(s.record_id)
        if record:
            student = students_map.get(record.student_id)
            if student:
                item.student_name = student.real_name or student.username
                item.student_username = student.username
            if record.lesson_id and record.lesson_id in lesson_map:
                lesson = lesson_map[record.lesson_id]
                item.entry_name = lesson.title
                item.entry_id = lesson.id
                item.entry_type = "lesson"
                chapter = chapter_map.get(lesson.chapter_id)
                course = course_map.get(chapter.course_id) if chapter else None
                if course:
                    item.course_id = course.id
                    item.course_name = course.title
            elif record.module_id and record.module_id in module_map:
                module = module_map[record.module_id]
                item.entry_name = module.name
                item.entry_id = module.id
                item.entry_type = "module"
        items.append(item)

    return ExperimentSubmissionListRead(
        items=items, page=page, page_size=page_size, total=total,
        summary=ExperimentSubmissionSummary(
            total=summary_total,
            pending=summary_pending,
            graded=summary_graded,
        ),
        filter_options=ExperimentSubmissionFilterOptions(
            courses=[
                ExperimentSubmissionFilterOption(id=option_id, name=name)
                for option_id, name in sorted(course_options.items(), key=lambda item: item[1])
            ],
            entries=[
                ExperimentSubmissionFilterOption(id=option_id, name=name)
                for option_id, name in sorted(entry_options.items(), key=lambda item: item[1])
            ],
        ),
    )


def _teacher_owns_record(db: Session, record: ExperimentRecord, user: User) -> bool:
    return bool(record.offering_id and record.offering_id in offering_scope.teacher_offering_ids(db, user.id))


@router.get("/submissions/{submission_id}", response_model=ExperimentSubmissionDetailRead)
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看单次提交详情"""
    submission = db.get(ExperimentSubmission, submission_id)
    if not submission:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "提交记录不存在")

    record = db.get(ExperimentRecord, submission.record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")

    # 权限检查
    if current_user.role == "student":
        if record.student_id != current_user.id:
            raise api_error(403, "FORBIDDEN", "无权查看该提交")
    elif current_user.role == "teacher":
        if not _teacher_owns_record(db, record, current_user):
            raise api_error(403, "FORBIDDEN", "无权查看该提交")
    elif current_user.role == "admin":
        pass
    else:
        raise api_error(403, "FORBIDDEN", "无权查看该提交")

    # 快照结构归一化：历史种子数据曾存为 {"cells": {cell_id: source}}，
    # API 提交为扁平 {cell_id: source}；统一解包为扁平结构再下发。
    snapshot = submission.cells_snapshot or {}
    if isinstance(snapshot, dict) and "cells" in snapshot and isinstance(snapshot.get("cells"), dict):
        snapshot = snapshot["cells"]

    version = db.get(NotebookTemplateVersion, record.template_version_id)
    metadata = {}
    if version:
        metadata = {
            cell["id"]: ExperimentCellMetadata(
                type=cell.get("type", "code"),
                order=cell.get("order", index),
            )
            for index, cell in enumerate(version.cells)
            if cell["id"] in snapshot
        }

    detail = ExperimentSubmissionDetailRead.model_validate(submission)
    lab = db.get(LabSubmission, submission.id)
    detail.lab_enabled = lab is not None
    run = db.get(LabRun, lab.current_run_id) if lab and lab.current_run_id else None
    detail.lab_status = "needs_review" if lab and lab.review_requested else run.status if run else None
    detail.cells_snapshot = snapshot
    detail.outputs_snapshot = submission.outputs_snapshot or {}
    detail.cell_metadata = metadata
    student = db.get(User, record.student_id)
    if student:
        detail.student_name = student.real_name or student.username
        detail.student_username = student.username
    if record.lesson_id:
        lesson = db.get(Lesson, record.lesson_id)
        if lesson:
            detail.entry_id = lesson.id
            detail.entry_type = "lesson"
            detail.entry_name = lesson.title
            chapter = db.get(Chapter, lesson.chapter_id)
            course = db.get(Course, chapter.course_id) if chapter else None
            if course:
                detail.course_id = course.id
                detail.course_name = course.title
    elif record.module_id:
        module = db.get(ExperimentModule, record.module_id)
        if module:
            detail.entry_id = module.id
            detail.entry_type = "module"
            detail.entry_name = module.name
    return detail


@router.patch("/submissions/{submission_id}/review", response_model=ExperimentSubmissionRead)
def review_submission(
    submission_id: int,
    payload: ExperimentReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """教师/管理员对实验提交评分和反馈"""
    # 校验：至少提供 score 或 feedback 之一
    if payload.score is None and payload.feedback is None:
        raise api_error(422, "EMPTY_REVIEW", "至少需要提供评分或反馈")
    if payload.score is not None and not (0 <= payload.score <= 100):
        raise api_error(422, "INVALID_SCORE", "评分必须在 0-100 之间")

    submission = db.get(ExperimentSubmission, submission_id)
    if not submission:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "提交记录不存在")

    record = db.get(ExperimentRecord, submission.record_id)
    if not record:
        raise api_error(404, "RECORD_NOT_FOUND", "实验记录不存在")

    # 权限：教师→自己课程的课时实验或自己创建的实验模块，管理员→全部
    if current_user.role == "teacher":
        if not _teacher_owns_record(db, record, current_user):
            raise api_error(403, "FORBIDDEN", "无权评分该提交")
    elif current_user.role not in ("admin",):
        raise api_error(403, "FORBIDDEN", "无权评分")

    offering_scope.record_teaching_access(db, record, current_user, write=True)
    submission = db.scalar(select(ExperimentSubmission).where(ExperimentSubmission.id == submission_id)
                           .with_for_update().execution_options(populate_existing=True))
    if not submission:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "提交记录不存在")

    if db.get(LabSubmission, submission.id):
        raise api_error(409, "LAB_REVIEW_REQUIRED", "请使用实验评测复核入口并填写改分原因")

    from datetime import datetime, timezone
    if payload.score is not None:
        submission.score = payload.score
    if payload.feedback is not None:
        submission.feedback = payload.feedback
    submission.reviewed_by_id = current_user.id
    submission.reviewed_at = datetime.now(timezone.utc)

    # 评分后更新 record 状态为 graded
    if payload.score is not None:
        record.status = "graded"

    db.commit()
    db.refresh(submission)
    return ExperimentSubmissionRead.model_validate(submission)
