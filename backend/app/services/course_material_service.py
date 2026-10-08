"""Private shared materials; external links are never fetched by the server."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.errors import api_error
from app.models import Assignment, Chapter, Course, Exam, ExperimentModule, Lesson, StorageObject, User
from app.models.course_material import ChapterActivityLink, CourseMaterial
from app.schemas.course_material import ChapterActivityRead, MaterialRead
from app.services import offering_service
from app.services.material_upload_validation import file_size, validate_archive, validate_pdf, validate_video_box
from app.services.storage_object_binding_service import retire_bound_object
from app.services.storage_object_service import StorageObjectService
from app.storage import StorageArea, StorageError, StorageLimitExceeded, StorageService

TYPES = {
    ".pdf": "application/pdf", ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ipynb": "application/x-ipynb+json", ".zip": "application/zip",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
}
TARGETS = {"assignment": Assignment, "exam": Exam, "lesson": Lesson, "module": ExperimentModule}
logger = logging.getLogger("course_material")


def can_edit(db, course, user):
    return offering_service.can_edit_course_content(db, course, user)


def require_course(db, course_id, user, *, write=False, lock=None):
    if write and lock is not False:
        from app.services.teaching_query_policy import lock_current_actor
        user = lock_current_actor(db, user)
        course = db.scalar(select(Course).where(Course.id == course_id)
            .with_for_update().execution_options(populate_existing=True))
    else:
        course = db.get(Course, course_id)
    if not course:
        raise api_error(404, "COURSE_NOT_FOUND", "课程不存在")
    allowed = offering_service.can_edit_course_content(db, course, user) if write else offering_service.can_read_course_content(db, course, user)
    if not allowed:
        raise api_error(403, "FORBIDDEN", "没有权限管理课程资料" if write else "没有权限查看课程资料")
    return course


def validate_chapter(db, course_id, chapter_id):
    if chapter_id is not None:
        chapter = db.get(Chapter, chapter_id)
        if not chapter or chapter.course_id != course_id:
            raise api_error(422, "INVALID_CHAPTER", "章节不属于当前课程")


def require_material(db, material_id, user, *, write=False, offering_id=None, lock=False):
    query = select(CourseMaterial).where(CourseMaterial.id == material_id)
    material = db.scalar(query)
    if not material:
        raise api_error(404, "MATERIAL_NOT_FOUND", "资料不存在")
    course = require_course(db, material.course_id, user, write=write, lock=lock)
    if lock:
        material = db.scalar(query.with_for_update().execution_options(populate_existing=True))
        if not material:
            raise api_error(404, "MATERIAL_NOT_FOUND", "资料不存在")
        if material.course_id != course.id:
            raise api_error(409, "MATERIAL_CHANGED", "资料所属课程已变更，请刷新后重试")
    if material.status == "archived":
        raise api_error(404, "MATERIAL_NOT_FOUND", "资料已移除")
    if offering_id is not None:
        offering = offering_service.require_offering(db, offering_id, user)
        if offering.course_id != material.course_id:
            raise api_error(403, "FORBIDDEN", "授课范围不属于当前课程")
    if user.role == "student" and (material.status != "published" or course.status != "published"):
        raise api_error(404, "MATERIAL_NOT_FOUND", "资料尚未发布")
    return material


def access_scope(db, material, user, offering_id):
    if offering_id is not None:
        offering = offering_service.require_offering(db, offering_id, user)
        if offering.course_id != material.course_id:
            raise api_error(403, "FORBIDDEN", "授课范围不属于当前课程")
        return offering.id
    if user.role == "student":
        ids = list(offering_service.student_offering_ids(db, user.id, material.course_id))
        if len(ids) != 1:
            raise api_error(409, "OFFERING_REQUIRED", "请先选择授课班级")
        offering_service.require_offering(db, ids[0], user)
        return ids[0]
    # Content-only access deliberately conveys no offering/student-data authority.
    return 0


def material_object(db, material):
    obj = db.get(StorageObject, material.storage_object_id) if material.storage_object_id else None
    if not obj or obj.status != "active" or obj.namespace != "course-materials":
        raise api_error(404, "MATERIAL_FILE_NOT_FOUND", "资料文件不存在，请联系教师")
    return obj


def serialize(db, material, user, objects=None):
    obj = (objects.get(material.storage_object_id) if objects is not None else db.get(StorageObject, material.storage_object_id)) if material.storage_object_id else None
    editable = can_edit(db, db.get(Course, material.course_id), user)
    return MaterialRead(
        id=material.id, course_id=material.course_id, chapter_id=material.chapter_id,
        title=material.title, description=material.description, category=material.category,
        source=material.source, status=material.status, external_url=material.external_url,
        order_index=material.order_index, version=material.version,
        filename=obj.original_filename if obj else None, content_type=obj.content_type if obj else None,
        size_bytes=obj.size_bytes if obj else None, sha256=obj.sha256 if obj else None,
        previewable=bool(obj and (obj.content_type == "application/pdf" or (obj.content_type or "").startswith("video/"))),
        can_edit=editable, created_at=material.created_at, updated_at=material.updated_at,
        history=material.history if editable else [],
    )


def remember(db, material):
    obj = db.get(StorageObject, material.storage_object_id) if material.storage_object_id else None
    previous = {
        "version": material.version, "title": material.title, "source": material.source,
        "status": material.status, "changed_at": datetime.now(timezone.utc).isoformat(),
        "filename": obj.original_filename if obj else None,
        "content_type": obj.content_type if obj else None, "size_bytes": obj.size_bytes if obj else None,
        "sha256": obj.sha256 if obj else None,
    }
    # Metadata history is bounded; retired file bytes and URLs are not retained.
    material.history = [*(material.history or [])[-99:], previous]
    material.version += 1


def check_version(material, expected):
    if material.version != expected:
        raise api_error(409, "MATERIAL_CHANGED", "资料已被更新，请刷新后重试")


def check_ready(db, material):
    if material.source == "external":
        if not material.external_url:
            raise api_error(422, "MATERIAL_INCOMPLETE", "请先填写官网链接")
    else:
        material_object(db, material)


def validate_upload(upload, settings):
    filename = upload.filename or ""
    if (not filename or len(filename) > 255 or "/" in filename or "\\" in filename
            or ".." in filename or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
        raise api_error(422, "MATERIAL_FILENAME_INVALID", "资料文件名无效")
    ext = Path(filename).suffix.lower()
    if ext not in TYPES:
        raise api_error(415, "MATERIAL_TYPE_UNSUPPORTED", "支持 PDF、PPT、PPTX、IPYNB、ZIP、MP4、WebM、MOV")
    limit = settings.material_large_max_upload_bytes if ext in (".zip", ".mp4", ".webm", ".mov") else settings.material_max_upload_bytes
    length = file_size(upload.file)
    if length > limit:
        raise api_error(413, "MATERIAL_TOO_LARGE", "文件超过资料上传大小限制")
    head = upload.file.read(16)
    upload.file.seek(0)
    valid = bool(head)
    if ext == ".pdf":
        valid = head.startswith(b"%PDF-")
    elif ext in (".zip", ".pptx"):
        valid = head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    elif ext == ".ppt":
        valid = head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    elif ext in (".mp4", ".mov"):
        valid = head[4:8] == b"ftyp"
    elif ext == ".webm":
        valid = head.startswith(b"\x1a\x45\xdf\xa3")
    if not valid:
        raise api_error(415, "MATERIAL_CONTENT_INVALID", "文件内容与扩展名不匹配或文件为空")
    if ext == '.pdf':
        validate_pdf(upload.file, length)
    elif ext in ('.zip', '.pptx'):
        validate_archive(upload.file, length, presentation=ext == '.pptx')
    elif ext in ('.mp4', '.mov'):
        validate_video_box(upload.file, length)
    if ext == ".ipynb":
        raw = upload.file.read(limit + 1)
        upload.file.seek(0)
        if len(raw) > limit:
            raise api_error(413, "MATERIAL_TOO_LARGE", "Notebook 超过大小限制")
        try:
            notebook = json.loads(raw)
            if not isinstance(notebook, dict) or notebook.get("nbformat") != 4 or not isinstance(notebook.get("cells"), list):
                raise ValueError("invalid notebook")
        except (ValueError, UnicodeDecodeError) as exc:
            raise api_error(415, "MATERIAL_CONTENT_INVALID", "Notebook 文件格式无效") from exc
    return filename, ext, TYPES[ext], limit


def register_active_object(db, storage, *, key, filename, content_type, physical, digest, actor_id):
    """Bind previously copied and hashed bytes without storage I/O under locks."""
    objects = StorageObjectService(db)
    obj = objects.create_staging(namespace="course-materials", object_key=key,
        backend=storage.backend_name, original_filename=filename, content_type=content_type,
        size_bytes=physical.size, sha256=digest, etag=physical.etag,
        metadata_json={"storage_area": StorageArea.MATERIALS.value}, created_by_id=actor_id)
    objects.activate(obj.id)
    return obj


def _cleanup_upload(storage, key):
    try:
        storage.delete(StorageArea.MATERIALS, key)
    except StorageError:
        logger.exception("failed to clean material upload")


def replace_file(db, material, user, upload, expected_version, settings):
    from app.services.account_service import audit
    from app.services.teaching_query_policy import install_request_scope, lock_current_actor

    check_version(material, expected_version)
    material_id, course_id = material.id, material.course_id
    initial_owner_id = require_course(db, course_id, user, write=True, lock=False).teacher_id
    if not db.info.get("teaching_actor"):
        install_request_scope(db, user)
    # Authentication opened a read transaction. Release it before the potentially
    # large upload/copy so administrator changes can commit during storage I/O.
    db.rollback()
    filename, ext, content_type, limit = validate_upload(upload, settings)
    storage = StorageService.from_settings(settings)
    nonce = uuid4().hex
    staged_key = f"materials/staging/{nonce}{ext}"
    key = f"materials/{course_id}/{material_id}/{nonce}{ext}"
    digest = hashlib.sha256()
    for chunk in iter(lambda: upload.file.read(1024 * 1024), b""):
        digest.update(chunk)
    upload.file.seek(0)
    old_id = None
    committed = False
    try:
        staged = storage.put(StorageArea.MATERIALS, staged_key, upload.file, max_bytes=limit)
        physical = storage.copy(StorageArea.MATERIALS, staged_key, key)
        if physical.size != staged.size:
            raise StorageError("copied material size mismatch")
        # Shared order: policy -> current actor -> current course -> material.
        # All checks are current locking reads, never the original ORM snapshot.
        current = lock_current_actor(db, user)
        course = db.scalar(select(Course).where(Course.id == course_id)
            .with_for_update().execution_options(populate_existing=True))
        if not course or course.teacher_id != initial_owner_id or not can_edit(db, course, current):
            raise api_error(403, "FORBIDDEN", "课程内容负责人已变更，请刷新后重试")
        locked = db.scalar(select(CourseMaterial).where(CourseMaterial.id == material_id)
            .with_for_update().execution_options(populate_existing=True))
        if not locked or locked.course_id != course_id or locked.status == "archived" or locked.source != "file":
            raise api_error(409, "MATERIAL_UPLOAD_NOT_ALLOWED", "资料状态已变更，请刷新后重试")
        check_version(locked, expected_version)
        obj = register_active_object(db, storage, key=key, filename=filename,
            content_type=content_type, physical=physical, digest=digest.hexdigest(), actor_id=current.id)
        old_id = locked.storage_object_id
        remember(db, locked)
        locked.storage_object_id = obj.id
        locked.external_url = None
        audit(db, current.id, "material.upload", "material", locked.id, changes={"version": locked.version})
        db.commit()
        committed = True
    except StorageLimitExceeded as exc:
        db.rollback()
        raise api_error(413, "MATERIAL_TOO_LARGE", f"文件超过 {limit // (1024 * 1024)} MiB 限制") from exc
    except StorageError as exc:
        db.rollback()
        raise api_error(503, "MATERIAL_STORAGE_FAILED", "资料存储暂时不可用") from exc
    except Exception:
        db.rollback()
        raise
    finally:
        _cleanup_upload(storage, staged_key)
        if not committed:
            _cleanup_upload(storage, key)
    retire(db, old_id, settings)
    return locked


def retire(db, object_id, settings):
    if object_id is not None:
        retire_bound_object(db, StorageService.from_settings(settings), object_id=object_id,
            area=StorageArea.MATERIALS, legacy_key=None, logger_name="course_material")


def signature(material, user, offering_id, expires, disposition, settings):
    message = f"material:{material.id}:{material.version}:{material.storage_object_id}:{user.id}:{user.session_version}:{offering_id}:{expires}:{disposition}"
    return hmac.new(settings.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()


def sign_access(material, user, offering_id, disposition, settings):
    expiry = datetime.now(timezone.utc) + timedelta(seconds=settings.material_url_ttl_seconds)
    expires = int(expiry.timestamp())
    sig = signature(material, user, offering_id, expires, disposition, settings)
    url = f"/api/v1/media/course-materials/{material.id}?uid={user.id}&offering_id={offering_id}&expires={expires}&disposition={disposition}&sig={sig}"
    return url, expiry


def verify_access(material, user, offering_id, expires, disposition, sig, settings):
    now = int(datetime.now(timezone.utc).timestamp())
    if expires <= now or expires > now + settings.material_url_ttl_seconds + 60:
        raise api_error(401, "MATERIAL_URL_EXPIRED", "资料访问地址已过期，请重新打开")
    if not hmac.compare_digest(signature(material, user, offering_id, expires, disposition, settings), sig):
        raise api_error(401, "MATERIAL_SIGNATURE_INVALID", "资料访问地址无效，请重新打开")


def target_for_link(db, link, offering):
    target = db.get(TARGETS[link.target_type], link.target_id)
    if not target:
        return None
    if link.target_type == "lesson":
        if not target.chapter or target.chapter.course_id != offering.course_id or target.content_type not in ("notebook", "experiment"):
            return None
    elif link.target_type == "module":
        from app.models.teaching import OfferingExperiment
        if not db.scalar(select(OfferingExperiment.id).where(OfferingExperiment.offering_id == offering.id,
                OfferingExperiment.entry_kind == "module", OfferingExperiment.entry_id == target.id)):
            return None
    elif getattr(target, "offering_id", None) != offering.id:
        return None
    if link.target_type in ("assignment", "exam") and target.course_id != offering.course_id:
        return None
    if link.target_type == "lesson":
        from app.models.teaching import OfferingExperiment
        if not db.scalar(select(OfferingExperiment.id).where(OfferingExperiment.offering_id == offering.id,
                OfferingExperiment.entry_kind == "lesson", OfferingExperiment.entry_id == target.id)):
            return None
    return target


def activity_read(link, target, user, course_id):
    kind = link.target_type
    query = f"?offering_id={link.offering_id}"
    if user.role == "student":
        route = f"/student/courses/{course_id}/notebook/{target.id}" if kind == "lesson" and target.content_type == "notebook" else (
            f"/student/courses/{course_id}/lessons/{target.id}" if kind == "lesson" else f"/student/{'experiments' if kind == 'module' else kind + 's'}/{target.id}")
    else:
        root = "admin" if user.role == "admin" else "teacher"
        if user.role == "admin" and kind in ("assignment", "exam"):
            route = f"/admin/{kind}s/{target.id}/edit"
            query += f"&from=materials&course_id={course_id}"
        else:
            route = f"/{root}/courses/{course_id}/lessons/{target.id}/edit" if kind == "lesson" else f"/{root}/{'experiments' if kind == 'module' else kind + 's'}"
    return ChapterActivityRead(id=link.id, offering_id=link.offering_id, chapter_id=link.chapter_id,
        target_type=kind, target_id=target.id, title=getattr(target, "title", None) or target.name,
        status=target.status, route=route + query, order_index=link.order_index)
