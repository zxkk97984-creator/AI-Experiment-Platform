"""Course-owner materials and class-specific activity links."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import func, select

from app.config import get_settings
from app.dependencies import get_current_user, get_db
from app.errors import api_error
from app.models import StorageObject, User
from app.models.course_material import ChapterActivityLink, CourseMaterial
from app.schemas.course_material import (
    ChapterActivityCreate, ChapterActivityRead, ChapterActivityTargetRead, MaterialAccessRead, MaterialCreate,
    MaterialListRead, MaterialRead, MaterialUpdate,
)
from app.services import course_material_service as service, offering_service
from app.services.account_service import audit, ensure_full_account
from app.storage import StorageArea, StorageError, StorageService
from .storage_media import storage_response

router = APIRouter(tags=["course-materials"])


@router.get("/courses/{course_id}/materials", response_model=MaterialListRead)
def list_materials(course_id: int, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                   chapter_id: int | None = None, q: str | None = Query(None, max_length=200),
                   offering_id: int | None = None, db=Depends(get_db), user=Depends(get_current_user), settings=Depends(get_settings)):
    course = service.require_course(db, course_id, user)
    if offering_id is not None:
        offering = offering_service.require_offering(db, offering_id, user)
        if offering.course_id != course_id:
            raise api_error(403, "FORBIDDEN", "授课范围不属于当前课程")
    filters = [CourseMaterial.course_id == course_id, CourseMaterial.status != "archived"]
    if user.role == "student":
        filters.append(CourseMaterial.status == "published")
    if chapter_id is not None:
        service.validate_chapter(db, course_id, chapter_id)
        filters.append(CourseMaterial.chapter_id == chapter_id)
    if q:
        filters.append(CourseMaterial.title.contains(q.strip(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(CourseMaterial).where(*filters)) or 0
    rows = db.scalars(select(CourseMaterial).where(*filters).order_by(CourseMaterial.order_index, CourseMaterial.id)
        .offset((page - 1) * page_size).limit(page_size)).all()
    object_ids = [row.storage_object_id for row in rows if row.storage_object_id is not None]
    objects = {obj.id: obj for obj in db.scalars(select(StorageObject).where(StorageObject.id.in_(object_ids)))} if object_ids else {}
    return MaterialListRead(items=[service.serialize(db, row, user, objects) for row in rows], page=page, page_size=page_size,
        total=total, can_edit=service.can_edit(db, course, user),
        ordinary_max_bytes=settings.material_max_upload_bytes, large_max_bytes=settings.material_large_max_upload_bytes)


@router.post("/courses/{course_id}/materials", response_model=MaterialRead, status_code=201)
def create_material(course_id: int, payload: MaterialCreate, db=Depends(get_db), user=Depends(get_current_user)):
    service.require_course(db, course_id, user, write=True)
    service.validate_chapter(db, course_id, payload.chapter_id)
    row = CourseMaterial(course_id=course_id, created_by_id=user.id, **payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.id, "material.create", "material", row.id)
    db.commit()
    return service.serialize(db, row, user)


@router.patch("/materials/{material_id}", response_model=MaterialRead)
def update_material(material_id: int, payload: MaterialUpdate, db=Depends(get_db), user=Depends(get_current_user)):
    row = service.require_material(db, material_id, user, write=True, lock=True)
    service.check_version(row, payload.version)
    values = payload.model_dump(exclude_unset=True, exclude={"version"})
    if any(values.get(key) is None for key in ("title", "category", "order_index", "status") if key in values):
        raise api_error(422, "MATERIAL_INVALID", "标题、分类、排序和发布状态不能为空")
    if "chapter_id" in values:
        service.validate_chapter(db, row.course_id, values["chapter_id"])
    if "external_url" in values and row.source != "external":
        raise api_error(422, "MATERIAL_SOURCE_IMMUTABLE", "请新建官网链接资料，不能把文件资料改为外链")
    service.remember(db, row)
    for key, value in values.items():
        setattr(row, key, value)
    if row.status == "published":
        service.check_ready(db, row)
    audit(db, user.id, "material.update", "material", row.id, changes={"version": row.version, "status": row.status})
    db.commit()
    return service.serialize(db, row, user)


@router.delete("/materials/{material_id}", status_code=204)
def archive_material(material_id: int, version: int = Query(..., ge=1), db=Depends(get_db), user=Depends(get_current_user), settings=Depends(get_settings)):
    row = service.require_material(db, material_id, user, write=True, lock=True)
    service.check_version(row, version)
    old_id = row.storage_object_id
    service.remember(db, row)
    row.status, row.storage_object_id = "archived", None
    audit(db, user.id, "material.archive", "material", row.id, changes={"version": row.version})
    db.commit()
    service.retire(db, old_id, settings)


@router.put("/materials/{material_id}/file", response_model=MaterialRead)
def upload_material(material_id: int, file: Annotated[UploadFile, File()], version: Annotated[int, Form(ge=1)],
                          db=Depends(get_db), user=Depends(get_current_user), settings=Depends(get_settings)):
    row = service.require_material(db, material_id, user, write=True)
    if row.status == "archived" or row.source != "file":
        raise api_error(409, "MATERIAL_UPLOAD_NOT_ALLOWED", "请先创建可用的文件资料")
    row = service.replace_file(db, row, user, file, version, settings)
    return service.serialize(db, row, user)


@router.get("/materials/{material_id}/access-url", response_model=MaterialAccessRead)
def material_access_url(material_id: int, offering_id: int | None = None, preview: bool = False,
                        db=Depends(get_db), user=Depends(get_current_user), settings=Depends(get_settings)):
    row = service.require_material(db, material_id, user, offering_id=offering_id)
    scope = service.access_scope(db, row, user, offering_id)
    if row.source == "external":
        service.check_ready(db, row)
        return MaterialAccessRead(url=row.external_url, external=True)
    obj = service.material_object(db, row)
    previewable = obj.content_type == "application/pdf" or (obj.content_type or "").startswith("video/")
    disposition = "inline" if preview and previewable else "attachment"
    url, expires_at = service.sign_access(row, user, scope, disposition, settings)
    return MaterialAccessRead(url=url, expires_at=expires_at, previewable=previewable)


@router.api_route("/media/course-materials/{material_id}", methods=["GET", "HEAD"])
def material_media(material_id: int, request: Request, uid: int, offering_id: int, expires: int,
                   disposition: Literal["attachment", "inline"], sig: str = Query(..., max_length=128),
                   db=Depends(get_db), settings=Depends(get_settings)):
    user = db.get(User, uid)
    if not user or user.status != "active":
        raise api_error(401, "UNAUTHORIZED", "账号不可用")
    ensure_full_account(user)
    row = service.require_material(db, material_id, user, offering_id=offering_id or None)
    service.verify_access(row, user, offering_id, expires, disposition, sig, settings)
    scope = service.access_scope(db, row, user, offering_id or None)
    if scope != offering_id:
        raise api_error(403, "FORBIDDEN", "授课范围已失效")
    obj = service.material_object(db, row)
    previewable = obj.content_type == "application/pdf" or (obj.content_type or "").startswith("video/")
    if disposition == "inline" and not previewable:
        raise api_error(422, "MATERIAL_PREVIEW_UNSUPPORTED", "此资料仅支持下载")
    try:
        return storage_response(request, StorageService.from_settings(settings), StorageArea.MATERIALS, obj.object_key,
            media_type=obj.content_type or "application/octet-stream", filename=obj.original_filename,
            content_disposition_type=disposition,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
    except StorageError as exc:
        raise api_error(404, "MATERIAL_FILE_NOT_FOUND", "资料文件不存在，请联系教师") from exc


@router.get("/offerings/{offering_id}/chapter-activities", response_model=list[ChapterActivityRead])
def list_activities(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    offering = offering_service.require_offering(db, offering_id, user)
    rows = db.scalars(select(ChapterActivityLink).where(ChapterActivityLink.offering_id == offering_id)
        .order_by(ChapterActivityLink.order_index, ChapterActivityLink.id)).all()
    result = []
    for link in rows:
        target = service.target_for_link(db, link, offering)
        if not target or (user.role == "student" and target.status != "published"):
            continue
        if user.role == "student" and link.target_type in ("assignment", "exam"):
            from app.services import audience_service
            checker = audience_service.student_in_assignment_audience if link.target_type == "assignment" else audience_service.student_in_exam_audience
            if not checker(db, target, user.id):
                continue
        if user.role == "student" and link.target_type in ("lesson", "module"):
            from app.models.teaching import OfferingExperiment
            delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id,
                OfferingExperiment.entry_kind == link.target_type, OfferingExperiment.entry_id == target.id))
            if not delivery or delivery.status != "published":
                continue
        result.append(service.activity_read(link, target, user, offering.course_id))
    return result


@router.get("/offerings/{offering_id}/chapter-activity-options", response_model=list[ChapterActivityTargetRead])
def activity_options(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "只有授课教师可以关联章节活动")
    offering = offering_service.require_offering(db, offering_id, user)
    result = []
    for kind in ("assignment", "exam"):
        model = service.TARGETS[kind]
        for target in db.scalars(select(model).where(model.offering_id == offering_id).order_by(model.id).limit(1000)):
            result.append(ChapterActivityTargetRead(target_type=kind, target_id=target.id, title=target.title, status=target.status))
    from app.models.teaching import OfferingExperiment
    for delivery in db.scalars(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id).limit(1000)):
        if delivery.entry_kind not in ("lesson", "module"):
            continue
        link = ChapterActivityLink(offering_id=offering_id, **{f"{delivery.entry_kind}_id": delivery.entry_id})
        target = service.target_for_link(db, link, offering)
        if target:
            result.append(ChapterActivityTargetRead(target_type=delivery.entry_kind, target_id=target.id,
                title=getattr(target, "title", None) or target.name, status=delivery.status))
    return result


@router.post("/offerings/{offering_id}/chapter-activities", response_model=ChapterActivityRead, status_code=201)
def create_activity(offering_id: int, payload: ChapterActivityCreate, db=Depends(get_db), user=Depends(get_current_user)):
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "只有授课教师可以关联章节活动")
    offering = offering_service.require_offering(db, offering_id, user, write=True)
    service.validate_chapter(db, offering.course_id, payload.chapter_id)
    row = ChapterActivityLink(offering_id=offering_id, chapter_id=payload.chapter_id, order_index=payload.order_index,
        created_by_id=user.id, **{f"{payload.target_type}_id": payload.target_id})
    target = service.target_for_link(db, row, offering)
    if not target:
        raise api_error(422, "ACTIVITY_SCOPE_INVALID", "活动不属于当前课程或授课班级，请先向本班发布实验")
    existing = db.scalar(select(ChapterActivityLink).where(ChapterActivityLink.offering_id == offering_id,
        ChapterActivityLink.chapter_id == payload.chapter_id,
        getattr(ChapterActivityLink, f"{payload.target_type}_id") == payload.target_id))
    if existing:
        return service.activity_read(existing, target, user, offering.course_id)
    db.add(row)
    db.flush()
    audit(db, user.id, "chapter.activity_link", "offering", offering_id, changes={"link_id": row.id, "kind": payload.target_type})
    db.commit()
    return service.activity_read(row, target, user, offering.course_id)


@router.delete("/chapter-activities/{link_id}", status_code=204)
def delete_activity(link_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    row = db.get(ChapterActivityLink, link_id)
    if not row:
        raise api_error(404, "ACTIVITY_LINK_NOT_FOUND", "活动关联不存在")
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "只有授课教师可以管理章节活动")
    offering_service.require_offering(db, row.offering_id, user, write=True)
    audit(db, user.id, "chapter.activity_unlink", "offering", row.offering_id, changes={"link_id": row.id})
    db.delete(row)
    db.commit()
