"""站内通知 API——教师端当前可见通知与已读回执。"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.dependencies import get_db, require_roles
from app.errors import api_error
from app.models import User
from app.schemas.notifications import NotificationListRead
from app.services.notification_service import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get("", response_model=NotificationListRead)
def get_notifications(
    unread_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "student")),
):
    return list_notifications(db, current_user, unread_only=unread_only)


@router.post("/{notification_id}/read", status_code=204)
def read_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "student")),
):
    if not mark_notification_read(db, current_user, notification_id):
        raise api_error(404, "NOTIFICATION_NOT_FOUND", "通知不存在或不可见")
    return Response(status_code=204)


@router.post("/read-all", status_code=204)
def read_all_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "student")),
):
    mark_all_notifications_read(db, current_user)
    return Response(status_code=204)
