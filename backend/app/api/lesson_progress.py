"""TASK-018（F-06）：服务端学习进度——打开记录 in_progress，完成显式操作。

- 唯一键 (lesson_id, student_id)；状态仅 in_progress/completed
- 幂等：start/complete/revert 重复调用结果一致；start 不会把 completed 降级
- 完成可撤回为 in_progress；课程进度聚合返回 total/completed/percent/next_lesson_id
- 不导入不可信 localStorage 历史，不记录视频播放位置/停留时长
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.courses import can_access_course_content, require_course
from app.dependencies import get_current_user, get_db
from app.errors import api_error
from app.models import Chapter, Lesson, LessonProgress, User
from app.schemas import CourseProgressRead, LessonProgressRead

router = APIRouter(tags=["lesson-progress"])

VALID_PROGRESS_STATUSES = {"in_progress", "completed"}


def _require_lesson(lesson_id: int, db: Session, current_user: User) -> Lesson:
    lesson = db.get(Lesson, lesson_id)
    if not lesson:
        raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
    if not can_access_course_content(lesson.chapter.course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限访问该课时")
    if current_user.role == "student" and lesson.status != "published":
        raise api_error(403, "LESSON_UNPUBLISHED", "课时尚未发布")
    return lesson


def _student_only(current_user: User) -> None:
    if current_user.role != "student":
        raise api_error(403, "STUDENT_ONLY", "仅学生可记录学习进度")


def _get_progress(db: Session, lesson_id: int, student_id: int, offering_id: int) -> LessonProgress | None:
    return db.scalar(
        select(LessonProgress).where(
            LessonProgress.lesson_id == lesson_id,
            LessonProgress.student_id == student_id,
            LessonProgress.offering_id == offering_id,
        )
    )


def _upsert_progress(
    db: Session, lesson_id: int, student_id: int, target_status: str, *, force: bool = False
) -> LessonProgress:
    """幂等写入。

    - completed：显式升级（idempotent）
    - in_progress（start，force=False）：不降级已完成的课时，只更新访问时间
    - in_progress（revert，force=True）：completed → in_progress 撤回
    """
    now = datetime.now(UTC)
    from app.services.offering_service import resolve_student_offering
    lesson = db.get(Lesson, lesson_id)
    offering = resolve_student_offering(db, lesson.chapter.course_id, db.get(User, student_id))
    row = _get_progress(db, lesson_id, student_id, offering.id)
    if row is None:
        row = LessonProgress(
            lesson_id=lesson_id, student_id=student_id, status=target_status,
            offering_id=offering.id, scope_key=f"offering:{offering.id}"
        )
        row.last_accessed_at = now
        db.add(row)
    else:
        if target_status == "completed" or force:
            row.status = target_status
        elif row.status != "completed":
            row.status = "in_progress"
        row.last_accessed_at = now
    db.commit()
    db.refresh(row)
    return row


@router.post("/lessons/{lesson_id}/progress/start", response_model=LessonProgressRead)
def start_lesson_progress(
    lesson_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """打开课时：记录 in_progress 与最后访问时间；已完成的课时保持 completed。"""
    _student_only(current_user)
    _require_lesson(lesson_id, db, current_user)
    return _upsert_progress(db, lesson_id, current_user.id, "in_progress")


@router.post("/lessons/{lesson_id}/progress/complete", response_model=LessonProgressRead)
def complete_lesson_progress(
    lesson_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """显式完成课时（幂等）。"""
    _student_only(current_user)
    _require_lesson(lesson_id, db, current_user)
    return _upsert_progress(db, lesson_id, current_user.id, "completed")


@router.post("/lessons/{lesson_id}/progress/revert", response_model=LessonProgressRead)
def revert_lesson_progress(
    lesson_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """撤回完成：completed → in_progress（幂等）。"""
    _student_only(current_user)
    _require_lesson(lesson_id, db, current_user)
    return _upsert_progress(db, lesson_id, current_user.id, "in_progress", force=True)


@router.get("/courses/{course_id}/progress", response_model=CourseProgressRead)
def get_course_progress(
    course_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """课程进度聚合：跨设备一致的服务端事实。"""
    _student_only(current_user)
    course = require_course(course_id, db)
    if not can_access_course_content(course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该课程进度")

    from app.services.offering_service import resolve_student_offering
    offering = resolve_student_offering(db, course_id, current_user)
    lessons = db.scalars(
        select(Lesson)
        .join(Chapter, Chapter.id == Lesson.chapter_id)
        .where(Chapter.course_id == course_id, Lesson.status == "published")
        .order_by(Chapter.order_index, Chapter.id, Lesson.order_index, Lesson.id)
    ).all()
    progress_rows = {
        row.lesson_id: row
        for row in db.scalars(
            select(LessonProgress).where(LessonProgress.student_id == current_user.id, LessonProgress.offering_id == offering.id)
        ).all()
    }
    total = len(lessons)
    completed = sum(
        1 for lesson in lessons
        if progress_rows.get(lesson.id) is not None
        and progress_rows[lesson.id].status == "completed"
    )
    next_lesson_id = next(
        (
            lesson.id
            for lesson in lessons
            if progress_rows.get(lesson.id) is None
            or progress_rows[lesson.id].status != "completed"
        ),
        None,
    )
    items = [
        LessonProgressRead(
            lesson_id=lesson.id,
            status=(
                progress_rows[lesson.id].status
                if lesson.id in progress_rows
                else "in_progress"
            ),
            last_accessed_at=(
                progress_rows[lesson.id].last_accessed_at if lesson.id in progress_rows else None
            ),
        )
        for lesson in lessons
    ]
    percent = round(completed * 100 / total) if total else 0
    return CourseProgressRead(
        course_id=course_id,
        total=total,
        completed=completed,
        percent=percent,
        next_lesson_id=next_lesson_id,
        items=items,
    )
