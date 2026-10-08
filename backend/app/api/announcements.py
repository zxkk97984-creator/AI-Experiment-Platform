"""公告 API——发布与已读回执；可见性查询在 services/announcement_service.py 供仪表盘复用

权限矩阵：
- admin：仅可发布 scope=global
- teacher：仅可发布 scope=course 且 course.teacher_id == 当前用户
- student：不可发布
可见性：全局公告所有人可见；课程公告仅任课教师、管理员、已选课学生可见。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_roles
from app.errors import api_error
from app.models import Announcement, AnnouncementRead, Course, User
from app.schemas.announcements import AnnouncementCreate, AnnouncementRead as AnnouncementReadSchema
from app.services.announcement_service import visible_conditions

router = APIRouter(prefix="/announcements", tags=["公告"])


@router.post("", response_model=AnnouncementReadSchema, status_code=201)
def publish_announcement(
    payload: AnnouncementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "teacher")),
):
    """发布公告——严格执行角色/scope 矩阵"""
    if current_user.role == "admin" and payload.scope != "global":
        raise api_error(403, "FORBIDDEN", "管理员仅可发布全局公告")
    if current_user.role == "teacher":
        if payload.scope not in ("course", "offering"):
            raise api_error(403, "FORBIDDEN", "教师仅可向自己的课程发布公告")
        if payload.scope == "offering":
            from app.services.offering_service import require_offering
            offering = require_offering(db, payload.offering_id, current_user, write=True)
            payload.course_id = offering.course_id
        course = db.get(Course, payload.course_id)
        if not course:
            raise api_error(404, "NOT_FOUND", "课程不存在")
        if payload.scope == "course" and course.teacher_id != current_user.id:
            raise api_error(403, "FORBIDDEN", "仅课程教师可发布公告")

    now = datetime.now(timezone.utc)
    if payload.expires_at is not None and payload.expires_at <= now:
        raise api_error(422, "INVALID_EXPIRES_AT", "过期时间必须晚于当前时间")

    title = payload.title.strip()
    content = payload.content.strip()
    if not title or not content:
        raise api_error(422, "INVALID_CONTENT", "标题与内容不能为空白")

    notice = Announcement(
        title=title,
        content=content,
        priority=payload.priority,
        scope=payload.scope,
        course_id=payload.course_id if payload.scope in ("course", "offering") else None,
        offering_id=payload.offering_id,
        author_id=current_user.id,
        expires_at=payload.expires_at,
    )
    db.add(notice)
    db.commit()
    db.refresh(notice)

    course_title = None
    if notice.course_id is not None:
        course = db.get(Course, notice.course_id)
        course_title = course.title if course else None
    return AnnouncementReadSchema(
        id=notice.id,
        title=notice.title,
        content=notice.content,
        priority=notice.priority,
        scope=notice.scope,
        course_id=notice.course_id,
        course_title=course_title,
        author_name=current_user.real_name,
        published_at=notice.published_at,
        expires_at=notice.expires_at,
        is_read=False,
    )


@router.post("/{announcement_id}/read", status_code=204)
def mark_announcement_read(
    announcement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记已读——幂等；不可见的公告返回 404 避免泄露其存在"""
    now = datetime.now(timezone.utc)
    visible = db.scalars(
        select(Announcement)
        .where(
            Announcement.id == announcement_id,
            Announcement.archived_at.is_(None),
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
            visible_conditions(current_user),
        )
    ).first()
    if visible is None:
        raise api_error(404, "NOT_FOUND", "公告不存在或不可见")

    existing = db.scalars(
        select(AnnouncementRead).where(
            AnnouncementRead.announcement_id == announcement_id,
            AnnouncementRead.user_id == current_user.id,
        )
    ).first()
    if existing is None:
        db.add(AnnouncementRead(announcement_id=announcement_id, user_id=current_user.id))
        try:
            db.commit()
        except IntegrityError:
            # 并发重复已读：唯一约束冲突视为已标记，仍幂等返回 204
            db.rollback()
    return Response(status_code=204)
