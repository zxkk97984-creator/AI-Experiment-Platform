"""单次成绩异议 API（批次 2）。

学生：申请 / 撤回 / 补充说明 / 查看自己的进度与改分结果。
教师与管理员：按作用域列出待处理异议并处理（维持 / 改分 / 重评 / 拒绝）。
课程总评成绩册不提供异议入口——本路由只覆盖实际参与计分的评分结果。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_db, get_redis_client, pagination, require_roles
from app.errors import api_error
from app.models import GradeAppeal, User
from app.schemas.appeals import AppealCreate, AppealHandle, AppealReply
from app.services import appeal_service

router = APIRouter(prefix="/appeals", tags=["成绩异议"])


def _appeal_read(db: Session, appeal: GradeAppeal, *, window_days: int, teacher_view: bool) -> dict:
    title = None
    route = appeal.route
    try:
        target = appeal_service.resolve_appeal_target(
            db, target_kind=appeal.target_kind, target_id=appeal.target_id,
            student_id=appeal.student_id,
        )
        title, route = target.title, target.route or route
    except HTTPException:
        # 目标已删除（例如提交被清理）：异议记录仍可读，只是没有链接
        pass
    visible_at = appeal.visible_at
    payload = {
        "id": appeal.id,
        "target_kind": appeal.target_kind,
        "target_id": appeal.target_id,
        "status": appeal.status,
        "reason": appeal.reason,
        "student_reply": appeal.student_reply,
        "handler_comment": appeal.handler_comment,
        "score_before": appeal.score_before,
        "score_after": appeal.score_after,
        "result_revision_snapshot": appeal.result_revision_snapshot,
        "title": title,
        "route": route,
        "created_at": appeal.created_at.isoformat() if appeal.created_at else None,
        "visible_at": visible_at.isoformat() if visible_at else None,
        "window_expires_at": (visible_at + timedelta(days=window_days)).isoformat()
        if visible_at else None,
        "resolved_at": appeal.resolved_at.isoformat() if appeal.resolved_at else None,
        "withdrawn_at": appeal.withdrawn_at.isoformat() if appeal.withdrawn_at else None,
        "can_withdraw": appeal.status in appeal_service.APPEAL_OPEN_STATUSES,
    }
    if teacher_view:
        student = db.get(User, appeal.student_id)
        payload.update({
            "student_id": appeal.student_id,
            "student_name": student.real_name if student else None,
            "student_no": student.student_no if student else None,
            "code_grade_id": appeal.code_grade_id,
            "resolved_by_id": appeal.resolved_by_id,
        })
    return payload


@router.post("", status_code=201)
def create_appeal(
    data: AppealCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    settings: Settings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
):
    appeal = appeal_service.create_appeal(
        db, redis_client, student=current_user, target_kind=data.target_kind,
        target_id=data.target_id, reason=data.reason,
        window_days=settings.appeal_window_days,
    )
    return _appeal_read(db, appeal, window_days=settings.appeal_window_days, teacher_view=False)


@router.get("/mine")
def list_my_appeals(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    settings: Settings = Depends(get_settings),
):
    rows = db.scalars(
        select(GradeAppeal).where(GradeAppeal.student_id == current_user.id)
        .order_by(GradeAppeal.created_at.desc(), GradeAppeal.id.desc()).limit(50)
    ).all()
    return {"items": [_appeal_read(db, row, window_days=settings.appeal_window_days,
                                   teacher_view=False) for row in rows]}


@router.post("/{appeal_id}/withdraw")
def withdraw_appeal(
    appeal_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    settings: Settings = Depends(get_settings),
):
    appeal = db.get(GradeAppeal, appeal_id)
    if appeal is None or appeal.student_id != current_user.id:
        raise api_error(404, "NOT_FOUND", "异议记录不存在")
    updated = appeal_service.withdraw_appeal(db, appeal=appeal, student=current_user)
    return {"ok": True, "status": updated.status,
            "appeal": _appeal_read(db, updated, window_days=settings.appeal_window_days,
                                   teacher_view=False)}


@router.post("/{appeal_id}/reply")
def reply_appeal(
    appeal_id: int,
    data: AppealReply,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    settings: Settings = Depends(get_settings),
):
    appeal = db.get(GradeAppeal, appeal_id)
    if appeal is None or appeal.student_id != current_user.id:
        raise api_error(404, "NOT_FOUND", "异议记录不存在")
    updated = appeal_service.reply_appeal(db, appeal=appeal, student=current_user,
                                          message=data.message)
    return {"ok": True, "status": updated.status,
            "appeal": _appeal_read(db, updated, window_days=settings.appeal_window_days,
                                   teacher_view=False)}


@router.get("")
def list_appeals(
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
    page: PaginationParams = Depends(pagination),
    settings: Settings = Depends(get_settings),
):
    condition = appeal_service.appeal_scope_condition(db, current_user)
    query = select(GradeAppeal)
    count_q = select(func.count()).select_from(GradeAppeal)
    if condition is not None:
        query = query.where(condition)
        count_q = count_q.where(condition)
    if status:
        query = query.where(GradeAppeal.status == status)
        count_q = count_q.where(GradeAppeal.status == status)
    if kind:
        query = query.where(GradeAppeal.target_kind == kind)
        count_q = count_q.where(GradeAppeal.target_kind == kind)
    total = db.scalar(count_q) or 0
    rows = db.scalars(
        query.order_by(GradeAppeal.created_at.desc(), GradeAppeal.id.desc())
        .offset((page.page - 1) * page.page_size).limit(page.page_size)
    ).all()
    return {
        "items": [_appeal_read(db, row, window_days=settings.appeal_window_days,
                               teacher_view=True) for row in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
    }


@router.post("/{appeal_id}/handle")
def handle_appeal(
    appeal_id: int,
    data: AppealHandle,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
    redis_client=Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
):
    appeal = db.get(GradeAppeal, appeal_id)
    if appeal is None:
        raise api_error(404, "NOT_FOUND", "异议记录不存在")
    result = appeal_service.handle_appeal(
        db, redis_client, appeal=appeal, actor=current_user, action=data.action,
        comment=data.comment, score=data.score, functional_score=data.functional_score,
        robustness_score=data.robustness_score, algorithm_score=data.algorithm_score,
        quality_score=data.quality_score, final_score_100=data.final_score_100,
    )
    result["appeal"] = _appeal_read(db, db.get(GradeAppeal, appeal_id),
                                    window_days=settings.appeal_window_days, teacher_view=True)
    return result
