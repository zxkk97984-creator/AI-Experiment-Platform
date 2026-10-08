"""统一提交中心服务——实验 / 作业 / 考试提交 UNION ALL 合并查询。"""

from __future__ import annotations


from sqlalchemy import and_, case, func, literal, literal_column, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    Assignment, Chapter, CodeGrade, Course, Exam, ExamSubmission,
    ExperimentModule, ExperimentRecord, ExperimentSubmission, JudgeQuestion,
    Lesson, Submission, User, LabSubmission, LabRun,
)
from app.schemas.unified_submissions import (
    UnifiedSubmissionFilterOption, UnifiedSubmissionFilterOptions,
    UnifiedSubmissionRead, UnifiedSubmissionSummary,
)

PAGE_SIZE_MAX = 50


def _teacher_course_ids(db: Session, user: User) -> list[int]:
    if user.role == "admin":
        return []
    return list(db.scalars(select(Course.id).where(teacher_course_condition(user.id))).all())


def _course_scope(db: Session, user: User) -> list[int] | None:
    ids = _teacher_course_ids(db, user)
    if user.role == "admin":
        return None
    return ids


def _teacher_scope(db: Session, user: User) -> list:
    """教师课程范围过滤条件：无课程时 in_([]) 恒假 → 零结果（fail-closed）；管理员不过滤。"""
    course_ids = _course_scope(db, user)
    return [Course.id.in_(course_ids)] if course_ids is not None else []


def _experiment_stmt(db: Session, user: User):
    # 教师可见：自己课程的课时实验（lesson→chapter→course 链路）+
    # 自己创建的实验模块（module 链路，无课程归属，course 链全 NULL）。
    # 曾仅按 Course 过滤，模块实验提交被整体排除；管理员不过滤。
    scope = None
    if user.role == "teacher":
        scope = or_(
            Course.id.in_(_teacher_course_ids(db, user)),
            ExperimentModule.owner_id == user.id,
        )
    stmt = (
        select(
            literal("experiment").label("kind"),
            ExperimentSubmission.id.label("id"),
            User.real_name.label("student_name"),
            User.student_no.label("student_no"),
            func.coalesce(Lesson.title, ExperimentModule.name).label("entry_title"),
            Course.id.label("course_id"),
            Course.title.label("course_title"),
            case(
                (LabSubmission.review_requested.is_(True), "review_required"),
                (LabRun.status == "needs_review", "review_required"),
                (LabRun.status == "failed", "failed"),
                (LabRun.status.in_(("queued", "running", "recovering")), "pending_grading"),
                (ExperimentSubmission.score.is_(None), "pending_grading"),
                else_="graded",
            ).label("status"),
            case(
                (LabSubmission.review_requested.is_(True), "warning"),
                (LabRun.status.in_(("needs_review", "failed", "queued", "running", "recovering")), "warning"),
                (ExperimentSubmission.score.is_(None), "warning"),
                else_="success",
            ).label("status_tone"),
            literal_column("NULL").label("tests_passed"),
            literal_column("NULL").label("tests_total"),
            case((LabSubmission.published_grade_id.is_not(None), ExperimentSubmission.score), else_=None).label("ai_score"),
            ExperimentSubmission.score.label("score"),
            ExperimentSubmission.submitted_at.label("submitted_at"),
            func.coalesce(Lesson.id, ExperimentModule.id).label("entry_id"),
            literal_column("NULL").label("context_id"),
        )
        .select_from(ExperimentSubmission)
        .outerjoin(LabSubmission, LabSubmission.submission_id == ExperimentSubmission.id)
        .outerjoin(LabRun, LabRun.id == LabSubmission.current_run_id)
        .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
        .outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id)
        .outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
        .outerjoin(Chapter, Chapter.id == Lesson.chapter_id)
        .outerjoin(Course, Course.id == func.coalesce(Chapter.course_id, select(CourseOffering.course_id).where(CourseOffering.id == ExperimentRecord.offering_id).correlate(ExperimentRecord).scalar_subquery()))
        .join(User, User.id == ExperimentRecord.student_id)
    )
    if scope is not None:
        stmt = stmt.where(scope)
    return stmt


def _assignment_status_expr():
    return case(
        (
            or_(
                Submission.status == "system_error",
                Submission.grading_status == "system_error",
            ),
            "failed",
        ),
        (
            Submission.grading_status.in_(("pending", "queued", "running")),
            "pending_grading",
        ),
        (
            and_(CodeGrade.needs_teacher_review.is_(True)),
            "review_required",
        ),
        (Submission.score.is_not(None), "graded"),
        else_="pending_grading",
    )


def _assignment_tone_expr():
    return case(
        (
            or_(
                Submission.status == "system_error",
                Submission.grading_status == "system_error",
            ),
            "danger",
        ),
        (
            Submission.grading_status.in_(("pending", "queued", "running")),
            "warning",
        ),
        (CodeGrade.needs_teacher_review.is_(True), "info"),
        (Submission.score.is_not(None), "success"),
        else_="warning",
    )


def _assignment_stmt(db: Session, user: User):
    scope = _teacher_scope(db, user)
    return (
        select(
            literal("assignment").label("kind"),
            Submission.id.label("id"),
            User.real_name.label("student_name"),
            User.student_no.label("student_no"),
            func.concat(Assignment.title, " · ", JudgeQuestion.title).label("entry_title"),
            Course.id.label("course_id"),
            Course.title.label("course_title"),
            _assignment_status_expr().label("status"),
            _assignment_tone_expr().label("status_tone"),
            Submission.tests_passed.label("tests_passed"),
            Submission.tests_total.label("tests_total"),
            CodeGrade.final_score_100.label("ai_score"),
            Submission.score.label("score"),
            Submission.created_at.label("submitted_at"),
            Assignment.id.label("entry_id"),
            literal_column("NULL").label("context_id"),
        )
        .select_from(Submission)
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .join(Course, Course.id == Assignment.course_id)
        .join(User, User.id == Submission.student_id)
        .outerjoin(CodeGrade, CodeGrade.submission_id == Submission.id)
        .where(*scope)
    )


def _exam_status_expr():
    return case(
        (ExamSubmission.status == "review_required", "review_required"),
        (ExamSubmission.status == "graded", "graded"),
        else_="pending_grading",
    )


def _exam_tone_expr():
    return case(
        (ExamSubmission.status == "review_required", "info"),
        (ExamSubmission.status == "graded", "success"),
        else_="warning",
    )


def _exam_stmt(db: Session, user: User):
    scope = _teacher_scope(db, user)
    return (
        select(
            literal("exam").label("kind"),
            ExamSubmission.id.label("id"),
            User.real_name.label("student_name"),
            User.student_no.label("student_no"),
            Exam.title.label("entry_title"),
            Course.id.label("course_id"),
            Course.title.label("course_title"),
            _exam_status_expr().label("status"),
            _exam_tone_expr().label("status_tone"),
            literal_column("NULL").label("tests_passed"),
            literal_column("NULL").label("tests_total"),
            literal_column("NULL").label("ai_score"),
            ExamSubmission.score.label("score"),
            func.coalesce(ExamSubmission.submitted_at, ExamSubmission.last_saved_at).label("submitted_at"),
            Exam.id.label("entry_id"),
            Exam.id.label("context_id"),
        )
        .select_from(ExamSubmission)
        .join(Exam, Exam.id == ExamSubmission.exam_id)
        .join(Course, Course.id == Exam.course_id)
        .join(User, User.id == ExamSubmission.student_id)
        .where(*scope)
    )


def _build_union(db: Session, user: User):
    return union_all(_experiment_stmt(db, user), _assignment_stmt(db, user), _exam_stmt(db, user))


def _summary_from_union(db: Session, sub) -> UnifiedSubmissionSummary:
    status_rows = dict(
        db.execute(
            select(sub.c.status, func.count()).group_by(sub.c.status)
        ).all()
    )
    return UnifiedSubmissionSummary(
        total=sum(status_rows.values()),
        pending=status_rows.get("pending_grading", 0),
        graded=status_rows.get("graded", 0),
        review=status_rows.get("review_required", 0),
        failed=status_rows.get("failed", 0),
    )


def _course_options(db: Session, user: User) -> list[UnifiedSubmissionFilterOption]:
    course_ids = _course_scope(db, user)
    stmt = select(Course.id, Course.title).order_by(Course.title)
    if course_ids is not None:
        stmt = stmt.where(Course.id.in_(course_ids))
    return [
        UnifiedSubmissionFilterOption(id=course_id, name=title, kind="course")
        for course_id, title in db.execute(stmt).all()
    ]


def _entry_options(db: Session, user: User) -> list[UnifiedSubmissionFilterOption]:
    course_ids = _course_scope(db, user)
    options: list[UnifiedSubmissionFilterOption] = []

    lesson_stmt = (
        select(Lesson.id, Lesson.title)
        .join(Chapter, Chapter.id == Lesson.chapter_id)
        .join(Course, Course.id == Chapter.course_id)
        .order_by(Lesson.title)
    )
    module_stmt = select(ExperimentModule.id, ExperimentModule.name).order_by(ExperimentModule.name)
    assignment_stmt = (
        select(Assignment.id, Assignment.title)
        .join(Course, Course.id == Assignment.course_id)
        .order_by(Assignment.title)
    )
    exam_stmt = (
        select(Exam.id, Exam.title)
        .join(Course, Course.id == Exam.course_id)
        .order_by(Exam.title)
    )
    if course_ids is not None:
        lesson_stmt = lesson_stmt.where(Course.id.in_(course_ids))
        assignment_stmt = assignment_stmt.where(Course.id.in_(course_ids))
        exam_stmt = exam_stmt.where(Course.id.in_(course_ids))
        module_stmt = module_stmt.where(
            ExperimentModule.owner_id == user.id
        ) if user.role == "teacher" else module_stmt
    options.extend(
        UnifiedSubmissionFilterOption(id=row_id, name=name, kind="experiment")
        for row_id, name in db.execute(lesson_stmt).all()
    )
    options.extend(
        UnifiedSubmissionFilterOption(id=row_id, name=name, kind="experiment")
        for row_id, name in db.execute(module_stmt).all()
    )
    options.extend(
        UnifiedSubmissionFilterOption(id=row_id, name=name, kind="assignment")
        for row_id, name in db.execute(assignment_stmt).all()
    )
    options.extend(
        UnifiedSubmissionFilterOption(id=row_id, name=name, kind="exam")
        for row_id, name in db.execute(exam_stmt).all()
    )
    return options


def list_unified_submissions(
    db: Session,
    user: User,
    *,
    q: str | None = None,
    course_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    entry_id: int | None = None,
    sort: str = "submitted_desc",
    page: int = 1,
    page_size: int = 20,
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), PAGE_SIZE_MAX)
    union = _build_union(db, user)
    sub = union.subquery()

    filters = []
    normalized_q = (q or "").strip()
    if normalized_q:
        like = f"%{normalized_q}%"
        filters.append(
            or_(
                sub.c.student_name.ilike(like),
                sub.c.student_no.ilike(like),
                sub.c.entry_title.ilike(like),
                sub.c.course_title.ilike(like),
            )
        )
    if course_id is not None:
        filters.append(sub.c.course_id == course_id)
    if kind and kind != "all":
        filters.append(sub.c.kind == kind)
    if status and status != "all":
        filters.append(sub.c.status == status)
    if entry_id is not None:
        filters.append(sub.c.entry_id == entry_id)

    total = db.scalar(select(func.count()).select_from(sub).where(*filters)) or 0
    order_col = sub.c.submitted_at.desc() if sort == "submitted_desc" else sub.c.submitted_at.asc()
    rows = db.execute(
        select(sub)
        .where(*filters)
        .order_by(order_col, sub.c.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    prefix = "/admin" if user.role == "admin" else "/teacher"
    items = []
    for row in rows:
        kind_value = row.kind
        if kind_value == "experiment":
            route = f"{prefix}/submissions/{row.id}"
        elif kind_value == "assignment":
            route = f"{prefix}/judge-submissions/{row.id}"
        else:
            route = f"{prefix}/exams/{row.context_id}/grades/{row.id}"
        items.append(
            UnifiedSubmissionRead(
                kind=kind_value,
                id=row.id,
                student_name=row.student_name,
                student_no=row.student_no,
                entry_title=row.entry_title,
                course_id=row.course_id,
                course_title=row.course_title,
                status=row.status,
                status_tone=row.status_tone,
                tests_passed=row.tests_passed,
                tests_total=row.tests_total,
                ai_score=row.ai_score,
                score=row.score,
                submitted_at=row.submitted_at,
                route=route,
            )
        )

    return items, total, page, page_size, _summary_from_union(db, sub), UnifiedSubmissionFilterOptions(
        courses=_course_options(db, user),
        entries=_entry_options(db, user),
    )

from app.services.offering_service import teacher_course_condition

from app.models.teaching import CourseOffering
