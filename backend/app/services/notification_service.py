"""站内通知服务——从教师工作台待办、公告、成绩异议与 AI 评分结果派生并持久化已读状态。

通知按角色派生：教师看到工作台待办/公告/待处理异议，学生看到自己的成绩异议进度、
AI 评分完成与教师改分结果。所有条目用 ``dedupe_key`` 幂等物化，重复派生不会重复发。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CodeGrade,
    Exam,
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    GradeAppeal,
    GradeOverride,
    Notification,
    NotificationRead,
    Submission,
    User,
)
from app.schemas.notifications import NotificationListRead, NotificationRead as NotificationReadSchema

NOTIFICATION_LIMIT = 50
STUDENT_GRADE_LIMIT = 20


def _urgency_priority(urgency: str) -> str:
    return {"urgent": "urgent", "soon": "important"}.get(urgency, "normal")


def sync_user_notifications(db: Session, user: User, now: datetime | None = None) -> None:
    """按角色派生物化通知：学生只看自己的成绩与异议，教师只看所教范围。"""
    if user.role == "student":
        sync_student_notifications(db, user, now=now)
    else:
        sync_teacher_notifications(db, user, now=now)


def _materialize(db: Session, user: User, current: dict[str, dict]) -> None:
    """把当前可见通知物化到库：新增/更新/隐藏，已读回执保留。"""
    existing = db.scalars(
        select(Notification).where(Notification.recipient_id == user.id)
    ).all()
    by_key = {row.dedupe_key: row for row in existing}

    for key, payload in current.items():
        row = by_key.get(key)
        if row is None:
            db.add(Notification(
                recipient_id=user.id,
                dedupe_key=key,
                **payload,
            ))
            continue
        changed = False
        for field, value in payload.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
        if not row.visible or changed:
            row.visible = True
    for key, row in by_key.items():
        if key not in current:
            row.visible = False

    try:
        db.commit()
    except IntegrityError:
        # 多实例同时派生时唯一键冲突视为已同步
        db.rollback()


def sync_teacher_notifications(db: Session, user: User, now: datetime | None = None) -> None:
    """物化当前可见通知；已消失的待办自动隐藏，已读回执保留。"""
    from app.services.dashboard_service import build_teacher_dashboard

    dashboard = build_teacher_dashboard(db, user, now=now)
    current: dict[str, dict] = {}

    for item in dashboard.work_items:
        key = f"work:{item.kind}:{item.id}"
        content = " · ".join(
            part for part in (item.course_title or "", item.detail or "") if part
        )
        current[key] = {
            "type": "work",
            "title": item.title[:200],
            "content": content[:500],
            "entity_kind": item.kind[:30],
            "entity_id": item.id,
            "route": item.route or "",
            "priority": _urgency_priority(item.urgency),
        }

    for notice in dashboard.announcements:
        key = f"announcement:{notice.id}"
        content = notice.content if notice.course_title is None else f"{notice.course_title} · {notice.content}"
        current[key] = {
            "type": "announcement",
            "title": notice.title[:200],
            "content": content[:500],
            "entity_kind": "announcement",
            "entity_id": notice.id,
            "route": "",
            "priority": notice.priority,
        }

    # 待处理异议：教师按在授范围派生，处理完成自动隐藏
    from app.services.appeal_service import teacher_pending_appeal_items

    current.update(teacher_pending_appeal_items(db, user))

    _materialize(db, user, current)


def sync_student_notifications(db: Session, user: User, now: datetime | None = None) -> None:
    """学生通知：成绩异议进度/结果、AI 评分完成、教师改分。"""
    current: dict[str, dict] = {}

    appeals = db.scalars(
        select(GradeAppeal).where(GradeAppeal.student_id == user.id)
        .order_by(GradeAppeal.created_at.desc(), GradeAppeal.id.desc()).limit(STUDENT_GRADE_LIMIT)
    ).all()
    for appeal in appeals:
        if appeal.status in ("pending", "regrading"):
            status_text = "已受理，等待教师处理" if appeal.status == "pending" else "重评进行中"
            current[f"appeal:{appeal.id}:received"] = {
                "type": "appeal",
                "title": "成绩异议已提交",
                "content": f"{status_text}（{appeal.target_kind} 原分 "
                           f"{_score_text(appeal.score_before)}）",
                "entity_kind": "grade_appeal",
                "entity_id": appeal.id,
                "route": appeal.route or "",
                "priority": "normal",
            }
        elif appeal.status in ("upheld", "adjusted", "rejected"):
            title = {"adjusted": "成绩异议已改分", "upheld": "成绩异议维持原分",
                     "rejected": "成绩异议未受理"}[appeal.status]
            current[f"appeal:{appeal.id}:resolved"] = {
                "type": "appeal",
                "title": title,
                "content": f"原分 {_score_text(appeal.score_before)} → "
                           f"{_score_text(appeal.score_after)}；"
                           f"{appeal.handler_comment or '教师已完成处理'}",
                "entity_kind": "grade_appeal",
                "entity_id": appeal.id,
                "route": appeal.route or "",
                "priority": "important" if appeal.status == "adjusted" else "normal",
            }

    # AI 评分完成（作业提交）
    for grade, submission in db.execute(
        select(CodeGrade, Submission)
        .join(Submission, Submission.id == CodeGrade.submission_id)
        .where(Submission.student_id == user.id, CodeGrade.status == "completed")
        .order_by(CodeGrade.id.desc()).limit(STUDENT_GRADE_LIMIT)
    ).all():
        current[f"grade:{grade.id}:completed"] = {
            "type": "grading",
            "title": "AI 评分已完成",
            "content": f"你的作业提交已评分：{_score_text(grade.final_score_100)} 分",
            "entity_kind": "code_grade",
            "entity_id": grade.id,
            "route": f"/student/submissions/{submission.id}",
            "priority": "normal",
        }

    # AI 评分完成（考试编程题）
    for grade, answer, exam in db.execute(
        select(CodeGrade, ExamAnswer, Exam)
        .join(ExamAnswer, ExamAnswer.id == CodeGrade.exam_answer_id)
        .join(ExamSubmission, ExamSubmission.id == ExamAnswer.submission_id)
        .join(ExamQuestion, ExamQuestion.id == ExamAnswer.question_id)
        .join(Exam, Exam.id == ExamQuestion.exam_id)
        .where(ExamSubmission.student_id == user.id, CodeGrade.status == "completed")
        .order_by(CodeGrade.id.desc()).limit(STUDENT_GRADE_LIMIT)
    ).all():
        current[f"grade:{grade.id}:completed"] = {
            "type": "grading",
            "title": "AI 评分已完成",
            "content": f"你的考试编程题已评分：{_score_text(grade.final_score_100)} 分",
            "entity_kind": "code_grade",
            "entity_id": grade.id,
            "route": f"/student/exams/{exam.id}",
            "priority": "normal",
        }

    # 教师改分（含异议改分）：按 GradeOverride 审计派生
    for override, grade in db.execute(
        select(GradeOverride, CodeGrade)
        .join(CodeGrade, CodeGrade.id == GradeOverride.code_grade_id)
        .outerjoin(Submission, Submission.id == CodeGrade.submission_id)
        .outerjoin(ExamAnswer, ExamAnswer.id == CodeGrade.exam_answer_id)
        .outerjoin(ExamSubmission, ExamSubmission.id == ExamAnswer.submission_id)
        .where((Submission.student_id == user.id) | (ExamSubmission.student_id == user.id))
        .order_by(GradeOverride.id.desc()).limit(STUDENT_GRADE_LIMIT)
    ).all():
        before = (override.original_snapshot or {}).get("final_score_100")
        after = (override.replacement_snapshot or {}).get("final_score_100")
        current[f"grade:{grade.id}:override:{override.id}"] = {
            "type": "grading",
            "title": "教师已复核你的成绩",
            "content": f"复核后成绩：{_score_text(before)} → {_score_text(after)}",
            "entity_kind": "code_grade",
            "entity_id": grade.id,
            "route": "",
            "priority": "important",
        }

    _materialize(db, user, current)


def _score_text(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def list_notifications(db: Session, user: User, unread_only: bool = False) -> NotificationListRead:
    sync_user_notifications(db, user)

    query = select(Notification).where(
        Notification.recipient_id == user.id,
        Notification.visible.is_(True),
    )
    if unread_only:
        read_ids = select(NotificationRead.notification_id).where(
            NotificationRead.user_id == user.id
        )
        query = query.where(~Notification.id.in_(read_ids))

    rows = db.scalars(
        query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(NOTIFICATION_LIMIT)
    ).all()
    read_ids = set(
        db.scalars(
            select(NotificationRead.notification_id).where(
                NotificationRead.user_id == user.id,
                NotificationRead.notification_id.in_([row.id for row in rows]),
            )
        ).all()
    )
    unread_count = (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.recipient_id == user.id,
                Notification.visible.is_(True),
                ~Notification.id.in_(
                    select(NotificationRead.notification_id).where(
                        NotificationRead.user_id == user.id
                    )
                ),
            )
        )
        or 0
    )
    items = [
        NotificationReadSchema(
            id=row.id,
            type=row.type,
            title=row.title,
            content=row.content,
            entity_kind=row.entity_kind,
            entity_id=row.entity_id,
            route=row.route or None,
            priority=row.priority,
            created_at=row.created_at,
            is_read=row.id in read_ids,
        )
        for row in rows
    ]
    return NotificationListRead(items=items, unread_count=unread_count, total=len(items))


def mark_notification_read(db: Session, user: User, notification_id: int) -> bool:
    row = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_id == user.id,
            Notification.visible.is_(True),
        )
    )
    if row is None:
        return False
    existing = db.scalar(
        select(NotificationRead).where(
            NotificationRead.notification_id == notification_id,
            NotificationRead.user_id == user.id,
        )
    )
    if existing is None:
        db.add(NotificationRead(notification_id=notification_id, user_id=user.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return True


def mark_all_notifications_read(db: Session, user: User) -> int:
    rows = db.scalars(
        select(Notification).where(
            Notification.recipient_id == user.id,
            Notification.visible.is_(True),
        )
    ).all()
    existing = set(
        db.scalars(
            select(NotificationRead.notification_id).where(
                NotificationRead.user_id == user.id,
            )
        ).all()
    )
    created = 0
    for row in rows:
        if row.id not in existing:
            db.add(NotificationRead(notification_id=row.id, user_id=user.id))
            created += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return created
