"""教师上传视频：上传、移除、签名播放地址与媒体读取端点。

权限模型：
- 上传/移除：管理员或课程 owner 教师（ensure_course_manager）。
- 播放签名 URL：登录用户；学生必须满足 can_access_course_content（可见 + 有效选课）。
- 媒体端点：无 Authorization Header，但必须校验 HMAC 签名、用户 active 状态、
  重新执行课程访问权限检查，并确认课时仍指向签名对应的当前文件。
"""
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import get_current_user, get_db
from app.errors import api_error
from app.models import Lesson, User
from app.schemas import (
    LessonRead,
    LessonVideoPlaybackRead,
    LessonVideoUploadRead,
)
from app.storage import StorageArea, StorageError, StorageService
from app.services.lesson_video_service import (
    create_playback_signature,
    remove_storage_key,
    store_upload,
    verify_playback_signature,
)
from app.services.storage_object_binding_service import (
    register_active_object,
    retire_bound_object,
)
from .storage_media import storage_response
from .courses import can_access_course_content, ensure_course_manager, require_course

router = APIRouter(tags=["lesson-videos"])

logger = logging.getLogger("lesson_video")


def _require_video_lesson(lesson_id: int, db: Session) -> Lesson:
    lesson = db.get(Lesson, lesson_id)
    if not lesson:
        raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
    if lesson.content_type != "video":
        raise api_error(409, "LESSON_NOT_VIDEO", "该课时不是视频类型")
    return lesson


def _playback_url(request: Request, lesson_id: int, uid: int, expires: int, sig: str) -> str:
    """返回同源相对签名 URL，避免反向代理的内部 scheme/host 泄漏到浏览器。"""
    return (
        f"/api/v1/media/lesson-videos/{lesson_id}"
        f"?uid={uid}&expires={expires}&sig={sig}"
    )


@router.put(
    "/lessons/{lesson_id}/video-file",
    response_model=LessonVideoUploadRead,
)
async def put_lesson_video(
    lesson_id: int,
    file: Annotated[UploadFile, File()],
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """上传/替换本地视频文件。成功后课时切换为 upload 来源并清空外链。"""
    lesson = _require_video_lesson(lesson_id, db)
    ensure_course_manager(lesson.chapter.course, current_user)

    stored = await store_upload(file, lesson_id, settings)
    storage = StorageService.from_settings(settings)

    try:
        new_object = register_active_object(
            db,
            storage,
            area=StorageArea.VIDEOS,
            namespace="lesson-videos",
            object_key=stored.storage_key,
            original_filename=stored.filename,
            content_type=stored.content_type,
            size_bytes=stored.size,
            created_by_id=current_user.id,
        )
        # 文件写入和对象元数据准备好后再锁定最新课时，避免并发替换时误删。
        lesson = db.execute(
            select(Lesson).where(Lesson.id == lesson_id).with_for_update()
        ).scalar_one()
        old_key = lesson.video_storage_key
        old_object_id = lesson.video_object_id
        lesson.video_source = "upload"
        lesson.video_url = None
        lesson.video_storage_key = stored.storage_key
        lesson.video_filename = stored.filename
        lesson.video_content_type = stored.content_type
        lesson.video_size = stored.size
        lesson.video_object_id = new_object.id
        db.commit()
    except Exception:
        db.rollback()
        # 数据库提交失败：删除刚写入的新文件，保持旧状态不变
        remove_storage_key(settings, stored.storage_key)
        raise
    db.refresh(lesson)

    # 业务行提交成功后才进入旧对象 deleting，避免先删旧对象造成数据丢失。
    if old_object_id is not None or (old_key and old_key != stored.storage_key):
        retire_bound_object(
            db,
            storage,
            object_id=old_object_id,
            area=StorageArea.VIDEOS,
            legacy_key=old_key,
            logger_name="lesson_video",
        )

    sig, expires_at = create_playback_signature(
        lesson.id, current_user.id, lesson.video_storage_key, settings, session_version=current_user.session_version
    )
    return LessonVideoUploadRead(
        lesson=LessonRead.model_validate(lesson),
        playback_url=_playback_url(request, lesson.id, current_user.id, int(expires_at.timestamp()), sig),
        expires_at=expires_at,
    )


@router.delete("/lessons/{lesson_id}/video-file", status_code=204)
def delete_lesson_video(
    lesson_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """移除本地视频：恢复 external 来源，保持 video_url 为空；提交成功后删除文件。"""
    lesson = _require_video_lesson(lesson_id, db)
    ensure_course_manager(lesson.chapter.course, current_user)

    old_key = lesson.video_storage_key
    old_object_id = lesson.video_object_id
    lesson.video_source = "external"
    lesson.video_storage_key = None
    lesson.video_filename = None
    lesson.video_content_type = None
    lesson.video_size = None
    lesson.video_object_id = None
    db.commit()
    retire_bound_object(
        db,
        StorageService.from_settings(settings),
        object_id=old_object_id,
        area=StorageArea.VIDEOS,
        legacy_key=old_key,
        logger_name="lesson_video",
    )
    return None


@router.get("/lessons/{lesson_id}/video-playback-url", response_model=LessonVideoPlaybackRead)
def get_lesson_video_playback_url(
    lesson_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """为当前用户签发短期签名播放地址。学生必须仍处于有效选课状态。"""
    lesson = db.get(Lesson, lesson_id)
    if not lesson:
        raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
    if lesson.video_source != "upload" or not lesson.video_storage_key:
        raise api_error(404, "VIDEO_NOT_FOUND", "该课时没有本地视频")

    if not can_access_course_content(lesson.chapter.course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限访问该课程的视频")

    sig, expires_at = create_playback_signature(
        lesson.id, current_user.id, lesson.video_storage_key, settings, session_version=current_user.session_version
    )
    return LessonVideoPlaybackRead(
        url=_playback_url(request, lesson.id, current_user.id, int(expires_at.timestamp()), sig),
        expires_at=expires_at,
    )


@router.get("/media/lesson-videos/{lesson_id}")
def stream_lesson_video(
    lesson_id: int,
    request: Request,
    uid: Annotated[int, Query()],
    expires: Annotated[int, Query()],
    sig: Annotated[str, Query()],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """媒体端点：支持 HTTP Range。不依赖 Authorization Header，但执行完整鉴权链。"""
    lesson = db.get(Lesson, lesson_id)
    if not lesson or lesson.video_source != "upload" or not lesson.video_storage_key:
        raise api_error(404, "VIDEO_NOT_FOUND", "视频文件不存在")

    user = db.get(User, uid)
    if not user or user.status != "active":
        raise api_error(401, "UNAUTHORIZED", "账号已失效，无法播放视频")
    from app.services.account_service import ensure_full_account
    ensure_full_account(user)
    verify_playback_signature(lesson.id, uid, expires, lesson.video_storage_key, sig, settings,
                              session_version=user.session_version)

    # 重新执行课程访问权限检查（退课/移出白名单/课程不可见后立即失效）
    if not can_access_course_content(lesson.chapter.course, user, db):
        raise api_error(403, "FORBIDDEN", "没有权限访问该课程的视频")

    # 签名内容包含当前 storage key，verify 通过即代表课时仍指向签名所对应的当前文件
    # （替换/移除视频后旧签名必然校验失败）
    try:
        response = storage_response(
            request,
            StorageService.from_settings(settings),
            StorageArea.VIDEOS,
            lesson.video_storage_key,
            media_type=lesson.video_content_type or "application/octet-stream",
            filename=lesson.video_filename
            or lesson.video_storage_key.rsplit("/", 1)[-1],
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Accept-Ranges": "bytes",
            },
        )
    except StorageError:
        logger.error("lesson video file missing on disk: lesson=%s key=%s", lesson_id, lesson.video_storage_key)
        raise api_error(404, "VIDEO_NOT_FOUND", "视频文件不存在")
    return response
