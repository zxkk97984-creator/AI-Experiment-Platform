"""角色首页聚合服务——学生/教师真实数据聚合，JOIN/子查询避免 N+1 循环"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, distinct, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Assignment, Chapter, CodeGrade, Course, CourseEnrollment, CourseTeachingClass, Exam, ExamAnswer,
    ExamQuestion, ExamSubmission, ExperimentModule, ExperimentRecord,
    ExperimentSubmission, JudgeQuestion, Lesson, Submission, TeachingClass,
    TeachingClassStudent, User, LabSubmission, LabGradeRevision, LabRun,
)
from app.services.announcement_service import (
    list_visible_announcements, unread_announcement_count,
)
from app.services.course_access_service import student_visible_course_predicate
from app.schemas.dashboard import (
    ContinueLearning, CourseHealth, CourseSnapshot, ManagedCourse,
    PriorityItem, RecentFeedback, RecentSubmission, StudentDashboardRead,
    StudentSummary, TeacherActivity, TeacherDashboardRead, TeacherSummary,
    WorkItem,
)
from app.services.submission_status import (
    assignment_display, exam_display, experiment_display,
)
from app.services.offering_service import teacher_course_condition
from app.services.audience_service import (
    assignment_visible_condition, effective_student_ids, exam_visible_condition,
)

PRIORITY_CAP = 8
FEEDBACK_CAP = 5
ACTIVITY_CAP = 8
RECENT_SUBMISSION_CAP = 8
URGENT_HOURS = 24
SOON_HOURS = 72
# 截止风险窗口：计划规定"未来 7 天内的截止"；urgency 分级仍用 24/72 小时
DEADLINE_WINDOW_HOURS = 24 * 7
_URGENCY_RANK = {"urgent": 0, "soon": 1, "normal": 2}


def _urgency(hours: float) -> str:
    if hours <= URGENT_HOURS:
        return "urgent"
    if hours <= SOON_HOURS:
        return "soon"
    return "normal"


def _hours_until(now: datetime, time_at: datetime | None) -> float:
    if time_at is None:
        return SOON_HOURS * 365
    if time_at.tzinfo is None:
        # SQLite/MySQL 的 DateTime(timezone=True) 返回 naive UTC，按 UTC 处理
        time_at = time_at.replace(tzinfo=timezone.utc)
    return (time_at - now).total_seconds() / 3600


def _asc_sort_key(value: datetime | None):
    """升序排序键：naive 视为 UTC；None 恒排最后"""
    if value is None:
        return (1, 0.0)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (0, value.timestamp())


def _desc_sort_key(value: datetime | None):
    """降序排序键：naive 视为 UTC；None 恒排最后"""
    if value is None:
        return (2, 0.0)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (1, -value.timestamp())


# ── 学生首页 ───────────────────────────────────────────────────


def build_student_dashboard(db: Session, user: User, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    summary = StudentSummary()
    priority_items: list[PriorityItem] = []
    courses: list[CourseSnapshot] = []
    feedback: list[RecentFeedback] = []
    continue_learning: ContinueLearning | None = None

    enrolled_ids = list(
        db.scalars(
            select(Course.id)
            .join(CourseEnrollment, CourseEnrollment.course_id == Course.id)
            .where(
                CourseEnrollment.student_id == user.id,
                CourseEnrollment.status == "enrolled",
                student_visible_course_predicate(user.id),
            )
        ).all()
    )
    summary.course_count = len(enrolled_ids)

    # ── 待交作业：任务发布范围优先于选课关系（白名单学生无需选课）──
    pending_rows = db.execute(
        select(Assignment, Course.title)
        .join(Course, Course.id == Assignment.course_id)
        .where(
            Course.status == "published",
            assignment_visible_condition(user.id),
            exists().where(
                and_(
                    JudgeQuestion.assignment_id == Assignment.id,
                    ~exists().where(
                        Submission.question_id == JudgeQuestion.id,
                        Submission.student_id == user.id,
                    ),
                )
            ),
        )
        .order_by(Assignment.due_at)
    ).all()
    summary.pending_assignment_count = len(pending_rows)
    for assignment, course_title in pending_rows:
        priority_items.append(
            PriorityItem(
                kind="assignment", id=assignment.id, title=assignment.title,
                course_title=course_title, time_at=assignment.due_at,
                urgency=_urgency(_hours_until(now, assignment.due_at)),
                route=f"/student/assignments/{assignment.id}",
            )
        )

    # ── 即将考试：任务发布范围优先于选课关系 ──
    exam_rows = db.execute(
        select(Exam, Course.title)
        .join(Course, Course.id == Exam.course_id)
        .where(
            Course.status == "published",
            exam_visible_condition(user.id),
            or_(Exam.start_at > now, Exam.end_at > now),
            ~exists().where(
                ExamSubmission.exam_id == Exam.id,
                ExamSubmission.student_id == user.id,
                ExamSubmission.status.in_(("submitted", "grading", "graded")),
            ),
        )
        .order_by(Exam.start_at)
    ).all()
    summary.upcoming_exam_count = len(exam_rows)
    for exam, course_title in exam_rows:
        time_at = exam.start_at or exam.end_at
        priority_items.append(
            PriorityItem(
                kind="exam", id=exam.id, title=exam.title,
                course_title=course_title, time_at=time_at,
                urgency=_urgency(_hours_until(now, time_at)),
                route=f"/student/exams/{exam.id}",
            )
        )

    if enrolled_ids:
        # ── 进行中实验：课程课时记录（需课程可达）与模块记录 ──
        lesson_rows = db.execute(
            select(ExperimentRecord, Lesson.title, Course.id, Course.title)
            .join(Lesson, Lesson.id == ExperimentRecord.lesson_id)
            .join(Chapter, Chapter.id == Lesson.chapter_id)
            .join(Course, Course.id == Chapter.course_id)
            .where(
                ExperimentRecord.student_id == user.id,
                ExperimentRecord.status == "started",
                ExperimentRecord.lesson_id.is_not(None),
                Course.id.in_(enrolled_ids),
            )
            .order_by(ExperimentRecord.updated_at.desc())
        ).all()
        module_rows = db.execute(
            select(ExperimentRecord, ExperimentModule.name)
            .join(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
            .where(
                ExperimentRecord.student_id == user.id,
                ExperimentRecord.status == "started",
                ExperimentRecord.module_id.is_not(None),
            )
            .order_by(ExperimentRecord.updated_at.desc())
        ).all()
        for record, lesson_title, course_id, course_title in lesson_rows:
            priority_items.append(
                PriorityItem(
                    kind="experiment", id=record.id, title=lesson_title,
                    course_title=course_title, time_at=record.updated_at,
                    urgency=_urgency(_hours_until(now, record.updated_at)),
                    route=f"/student/courses/{course_id}/notebook/{record.lesson_id}",
                )
            )
        for record, module_name in module_rows:
            priority_items.append(
                PriorityItem(
                    kind="experiment", id=record.id, title=module_name,
                    course_title=None, time_at=record.updated_at,
                    urgency=_urgency(_hours_until(now, record.updated_at)),
                    route=f"/student/experiments/{record.module_id}",
                )
            )

        # ── 续学：最近更新的可达实验记录，回退到第一门课 ──
        cont_rows = db.execute(
            select(ExperimentRecord, Lesson.title, Course.id, Course.title)
            .join(Lesson, Lesson.id == ExperimentRecord.lesson_id)
            .join(Chapter, Chapter.id == Lesson.chapter_id)
            .join(Course, Course.id == Chapter.course_id)
            .where(ExperimentRecord.student_id == user.id, Course.id.in_(enrolled_ids))
            .order_by(ExperimentRecord.updated_at.desc())
            .limit(1)
        ).all()
        cont_module = db.execute(
            select(ExperimentRecord, ExperimentModule.name)
            .join(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
            .where(ExperimentRecord.student_id == user.id)
            .order_by(ExperimentRecord.updated_at.desc())
            .limit(1)
        ).all()
        if cont_rows:
            record, lesson_title, course_id, course_title = cont_rows[0]
            continue_learning = ContinueLearning(
                kind="lesson_experiment", title=lesson_title, subtitle=course_title,
                updated_at=record.updated_at,
                route=f"/student/courses/{course_id}/notebook/{record.lesson_id}",
            )
        elif cont_module:
            record, module_name = cont_module[0]
            continue_learning = ContinueLearning(
                kind="module_experiment", title=module_name, subtitle=None,
                updated_at=record.updated_at,
                route=f"/student/experiments/{record.module_id}",
            )

        # ── 课程快照：按课程聚合真实计数 ──
        pending_by_course = dict(
            db.execute(
                select(Assignment.course_id, func.count(Assignment.id))
                .where(
                    Assignment.course_id.in_(enrolled_ids),
                    assignment_visible_condition(user.id),
                    exists().where(
                        and_(
                            JudgeQuestion.assignment_id == Assignment.id,
                            ~exists().where(
                                Submission.question_id == JudgeQuestion.id,
                                Submission.student_id == user.id,
                            ),
                        )
                    ),
                )
                .group_by(Assignment.course_id)
            ).all()
        )
        exams_by_course = dict(
            db.execute(
                select(Exam.course_id, func.count(Exam.id))
                .where(
                    Exam.course_id.in_(enrolled_ids),
                    exam_visible_condition(user.id),
                    or_(Exam.start_at > now, Exam.end_at > now),
                    ~exists().where(
                        ExamSubmission.exam_id == Exam.id,
                        ExamSubmission.student_id == user.id,
                        ExamSubmission.status.in_(("submitted", "grading", "graded")),
                    ),
                )
                .group_by(Exam.course_id)
            ).all()
        )
        activity_by_course = dict(
            db.execute(
                select(Course.id, func.max(ExperimentRecord.updated_at))
                .select_from(ExperimentRecord)
                .join(Lesson, Lesson.id == ExperimentRecord.lesson_id)
                .join(Chapter, Chapter.id == Lesson.chapter_id)
                .join(Course, Course.id == Chapter.course_id)
                .where(ExperimentRecord.student_id == user.id, Course.id.in_(enrolled_ids))
                .group_by(Course.id)
            ).all()
        )
        course_rows = db.execute(
            select(Course).options(
                selectinload(Course.academic_term),
                selectinload(Course.teaching_class_links).selectinload(CourseTeachingClass.teaching_class),
            ).where(Course.id.in_(enrolled_ids)).order_by(Course.title)
        ).scalars().all()
        for course in course_rows:
            courses.append(
                CourseSnapshot(
                    id=course.id, title=course.title,
                    academic_term=course.academic_term.name if course.academic_term else None,
                    teaching_classes=[link.teaching_class.name for link in course.teaching_class_links],
                    pending_assignment_count=pending_by_course.get(course.id, 0),
                    upcoming_exam_count=exams_by_course.get(course.id, 0),
                    last_activity_at=activity_by_course.get(course.id),
                    route=f"/student/courses/{course.id}",
                )
            )
        courses.sort(key=lambda c: c.last_activity_at is None)

        # ── 最新反馈：实验复核 / 作业判题 / 考试评分 ──
        exp_fb = db.execute(
            select(
                ExperimentSubmission, ExperimentRecord, Lesson.title,
                ExperimentModule.name, Course.id, Course.title,
                func.coalesce(LabGradeRevision.published_at, ExperimentSubmission.reviewed_at).label("published_at"),
            )
            .outerjoin(LabSubmission, LabSubmission.submission_id == ExperimentSubmission.id)
            .outerjoin(LabGradeRevision, LabGradeRevision.id == LabSubmission.published_grade_id)
            .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
            .outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id)
            .outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
            .outerjoin(Chapter, Chapter.id == Lesson.chapter_id)
            .outerjoin(Course, Course.id == Chapter.course_id)
            .where(
                ExperimentRecord.student_id == user.id,
                or_(LabGradeRevision.published_at.is_not(None), and_(LabSubmission.submission_id.is_(None), ExperimentSubmission.reviewed_at.is_not(None))),
                # 课时实验反馈仅限当前已选 published 课程；独立模块实验（无课程）保留
                or_(Course.id.is_(None), Course.id.in_(enrolled_ids)),
            )
            .order_by(func.coalesce(LabGradeRevision.published_at, ExperimentSubmission.reviewed_at).desc())
        ).all()
        for sub, record, lesson_title, module_name, course_id, course_title, published_at in exp_fb:
            title = lesson_title or module_name
            route = (
                f"/student/courses/{course_id}/notebook/{record.lesson_id}"
                if record.lesson_id
                else f"/student/experiments/{record.module_id}"
            )
            feedback.append(
                RecentFeedback(
                    kind="experiment", id=sub.id, title=f"{title} 反馈",
                    course_title=course_title, score=sub.score, feedback=sub.feedback,
                    graded_at=published_at, route=route,
                )
            )
        assign_fb = db.execute(
            select(Submission, JudgeQuestion.title, Assignment.title, Course.title)
            .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
            .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
            .join(Course, Course.id == Assignment.course_id)
            .where(
                Submission.student_id == user.id,
                Submission.grading_status == "completed",
                Submission.score.is_not(None),
                Course.id.in_(enrolled_ids),
                Course.status == "published",
            )
            .order_by(Submission.finished_at.desc())
        ).all()
        for sub, _q_title, assignment_title, course_title in assign_fb:
            feedback.append(
                RecentFeedback(
                    kind="assignment", id=sub.id, title=assignment_title,
                    course_title=course_title, score=sub.score, feedback=None,
                    graded_at=sub.finished_at, route=f"/student/submissions/{sub.id}",
                )
            )
        exam_fb = db.execute(
            select(ExamSubmission, Exam.title, Course.title)
            .join(Exam, Exam.id == ExamSubmission.exam_id)
            .join(Course, Course.id == Exam.course_id)
            .where(
                ExamSubmission.student_id == user.id,
                ExamSubmission.status == "graded",
                ExamSubmission.score.is_not(None),
                Course.id.in_(enrolled_ids),
                Course.status == "published",
            )
            .order_by(ExamSubmission.graded_at.desc())
        ).all()
        for sub, exam_title, course_title in exam_fb:
            feedback.append(
                RecentFeedback(
                    kind="exam", id=sub.id, title=f"{exam_title} 成绩",
                    course_title=course_title, score=sub.score, feedback=None,
                    graded_at=sub.graded_at, route=f"/student/exams/{sub.exam_id}",
                )
            )
        feedback.sort(key=lambda f: _desc_sort_key(f.graded_at))
        feedback = feedback[:FEEDBACK_CAP]

    if continue_learning is None and courses:
        first = courses[0]
        continue_learning = ContinueLearning(
            kind="course", title=first.title, subtitle=None,
            updated_at=first.last_activity_at, route=first.route,
        )

    announcements = list_visible_announcements(db, user, now, limit=PRIORITY_CAP)
    summary.unread_announcement_count = unread_announcement_count(db, user, now)
    priority_items = priority_items[:PRIORITY_CAP]

    student_class_names = list(db.scalars(
        select(TeachingClass.name).join(TeachingClassStudent, TeachingClassStudent.teaching_class_id == TeachingClass.id)
        .where(TeachingClassStudent.student_id == user.id, TeachingClassStudent.status == "active", TeachingClass.status == "active")
        .order_by(TeachingClass.name)
    ).all())
    return StudentDashboardRead(
        student_no=user.student_no,
        teaching_classes=student_class_names,
        summary=summary, priority_items=priority_items,
        continue_learning=continue_learning, courses=courses,
        recent_feedback=feedback, announcements=announcements,
    )


# ── 教师首页 ───────────────────────────────────────────────────


def _teacher_pending_review_rows(db: Session, owned_ids: list[int]):
    """教师课程内的待复核数据：实验提交（未复核）与 AI 评分（需复核）"""
    exp_rows = db.execute(
        select(
            ExperimentSubmission, ExperimentRecord, User.real_name, User.student_no,
            Lesson.id, Lesson.title, Lesson.due_at,
            ExperimentModule.id, ExperimentModule.name, ExperimentModule.due_at,
            Course.id, Course.title,
        )
        .outerjoin(LabSubmission, LabSubmission.submission_id == ExperimentSubmission.id)
        .outerjoin(LabRun, LabRun.id == LabSubmission.current_run_id)
        .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
        .outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id)
        .outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
        .outerjoin(Chapter, Chapter.id == Lesson.chapter_id)
        .outerjoin(Course, Course.id == Chapter.course_id)
        .join(User, User.id == ExperimentRecord.student_id)
        .where(or_(LabSubmission.review_requested.is_(True), LabRun.status == "needs_review",
                   and_(LabSubmission.submission_id.is_(None), ExperimentSubmission.reviewed_at.is_(None))), Course.id.in_(owned_ids))
        .order_by(ExperimentSubmission.submitted_at)
    ).all()
    ai_rows = db.execute(
        select(
            CodeGrade, Submission, Assignment, Course.id, Course.title, User.real_name,
        )
        .join(Submission, Submission.id == CodeGrade.submission_id)
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .join(Course, Course.id == Assignment.course_id)
        .join(User, User.id == Submission.student_id)
        .where(CodeGrade.needs_teacher_review.is_(True), Course.id.in_(owned_ids))
    ).all()
    exam_ai_rows = db.execute(
        select(
            CodeGrade, ExamAnswer, Exam, Course.id, Course.title, User.real_name,
        )
        .join(ExamAnswer, ExamAnswer.id == CodeGrade.exam_answer_id)
        .join(ExamSubmission, ExamSubmission.id == ExamAnswer.submission_id)
        .join(Exam, Exam.id == ExamSubmission.exam_id)
        .join(Course, Course.id == Exam.course_id)
        .join(User, User.id == ExamSubmission.student_id)
        .where(CodeGrade.needs_teacher_review.is_(True), Course.id.in_(owned_ids))
    ).all()
    return exp_rows, ai_rows, exam_ai_rows


def _deadline_stats_batch(db: Session, assignments: list[Assignment]) -> dict[int, tuple[int, int]]:
    """批量统计一组作业的 (完成全部题目的当前选课学生数, 选课学生数)，避免逐作业 N+1

    已提交定义：学生对作业的全部题目都有提交记录（只答一题不算已提交）；
    且该学生必须仍处于 enrolled 状态——退课/异常提交者不进入分子。
    """
    if not assignments:
        return {}
    assignment_ids = [a.id for a in assignments]
    total_questions = dict(
        db.execute(
            select(JudgeQuestion.assignment_id, func.count(JudgeQuestion.id))
            .where(JudgeQuestion.assignment_id.in_(assignment_ids))
            .group_by(JudgeQuestion.assignment_id)
        ).all()
    )
    pairs = db.execute(
        select(Submission.student_id, Submission.question_id, JudgeQuestion.assignment_id)
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .where(JudgeQuestion.assignment_id.in_(assignment_ids))
        .distinct()
    ).all()
    seen: dict[tuple[int, int], set[int]] = {}
    for student_id, question_id, assignment_id in pairs:
        seen.setdefault((assignment_id, student_id), set()).add(question_id)
    completed: dict[int, set[int]] = {}
    for (assignment_id, student_id), questions in seen.items():
        if total_questions.get(assignment_id, 0) > 0 and len(questions) >= total_questions[assignment_id]:
            completed.setdefault(assignment_id, set()).add(student_id)
    # 有效发布范围：作业级 audience（课程在册 / 指定班级 / 白名单 ± 排除）
    from app.models import Course as _Course

    result = {}
    for a in assignments:
        course = db.get(_Course, a.course_id)
        audience = effective_student_ids(
            db, task_type="assignment", task_id=a.id, course=course,
        ) if course else set()
        result[a.id] = (
            len(completed.get(a.id, set()) & audience),
            len(audience),
        )
    return result


def _task_urgency(now: datetime, due_at: datetime | None, oldest_at: datetime | None) -> str:
    """工作队列 urgency：优先使用截止时间；无截止时间时按最旧待办滞留时长分级。"""
    if due_at is not None:
        return _urgency(_hours_until(now, due_at))
    if oldest_at is None:
        return "normal"
    waiting_hours = -_hours_until(now, oldest_at)
    if waiting_hours >= 48:
        return "urgent"
    if waiting_hours >= 24:
        return "soon"
    return "normal"


def _exp_entry_route(lesson_id, module_id):
    if lesson_id is not None:
        return f"/teacher/submissions/unified?kind=experiment&entry_id={lesson_id}&status=pending_grading"
    if module_id is not None:
        return f"/teacher/submissions/unified?kind=experiment&entry_id={module_id}&status=pending_grading"
    return "/teacher/submissions/unified?kind=experiment&status=pending_grading"


def _teacher_pending_assignment_rows(db: Session, owned_ids: list[int]):
    """教师课程内仍在判题队列中的作业提交，按提交行返回供工作队列聚合。"""
    return db.execute(
        select(Submission, Assignment, JudgeQuestion.title, Course.id, Course.title)
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .join(Course, Course.id == Assignment.course_id)
        .where(
            Course.id.in_(owned_ids),
            Assignment.status == "published",
            Submission.grading_status.in_(("pending", "queued", "running")),
        )
        .order_by(Submission.created_at)
    ).all()


def _teacher_pending_exam_release_rows(db: Session, owned_ids: list[int], now: datetime):
    """已结束、有交卷数据且未发布讲评/成绩的考试。"""
    return db.execute(
        select(Exam, Course.id, Course.title)
        .join(Course, Course.id == Exam.course_id)
        .where(
            Exam.course_id.in_(owned_ids),
            Exam.status == "published",
            Exam.review_released_at.is_(None),
            Exam.end_at.is_not(None),
            Exam.end_at <= now,
            ~exists().where(
                ExamSubmission.exam_id == Exam.id,
                ExamSubmission.status == "in_progress",
            ),
            exists().where(
                ExamSubmission.exam_id == Exam.id,
                ExamSubmission.status.in_(("submitted", "grading", "graded", "review_required")),
            ),
        )
        .order_by(Exam.end_at)
    ).all()


def _teacher_recent_submissions(db: Session, owned_ids: list[int]):
    """最近实验/作业/考试提交摘要；各源取最近 8 条后在 Python 中合并排序。"""
    rows: list[tuple] = []

    exp_rows = db.execute(
        select(
            ExperimentSubmission, User.real_name, User.student_no,
            Lesson.title, ExperimentModule.name, Course.id, Course.title,
        )
        .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
        .outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id)
        .outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
        .outerjoin(Chapter, Chapter.id == Lesson.chapter_id)
        .outerjoin(Course, Course.id == Chapter.course_id)
        .join(User, User.id == ExperimentRecord.student_id)
        .where(Course.id.in_(owned_ids))
        .order_by(ExperimentSubmission.submitted_at.desc(), ExperimentSubmission.id.desc())
        .limit(RECENT_SUBMISSION_CAP)
    ).all()
    for sub, name, student_no, lesson_title, module_name, course_id, course_title in exp_rows:
        status, tone = experiment_display(sub)
        rows.append((
            sub.submitted_at,
            "experiment", sub.id, name, student_no, lesson_title or module_name,
            course_id, course_title, status, tone, None, None, None, sub.score,
            sub.submitted_at, f"/teacher/submissions/{sub.id}",
        ))

    assign_rows = db.execute(
        select(
            Submission, User.real_name, User.student_no, JudgeQuestion.title,
            Assignment.title, Course.id, Course.title, CodeGrade,
        )
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .join(Course, Course.id == Assignment.course_id)
        .join(User, User.id == Submission.student_id)
        .outerjoin(CodeGrade, CodeGrade.submission_id == Submission.id)
        .where(Course.id.in_(owned_ids))
        .order_by(Submission.created_at.desc(), Submission.id.desc())
        .limit(RECENT_SUBMISSION_CAP)
    ).all()
    for sub, name, student_no, question_title, assignment_title, course_id, course_title, cg in assign_rows:
        status, tone = assignment_display(sub, cg)
        submitted_at = sub.created_at
        rows.append((
            submitted_at,
            "assignment", sub.id, name, student_no,
            f"{assignment_title} · {question_title}",
            course_id, course_title, status, tone,
            sub.tests_passed, sub.tests_total,
            cg.final_score_100 if cg is not None else None,
            sub.score, submitted_at, f"/teacher/judge-submissions/{sub.id}",
        ))

    exam_rows = db.execute(
        select(
            ExamSubmission, User.real_name, User.student_no,
            Exam.title, Course.id, Course.title,
        )
        .join(Exam, Exam.id == ExamSubmission.exam_id)
        .join(Course, Course.id == Exam.course_id)
        .join(User, User.id == ExamSubmission.student_id)
        .where(Course.id.in_(owned_ids))
        .order_by(ExamSubmission.submitted_at.desc(), ExamSubmission.id.desc())
        .limit(RECENT_SUBMISSION_CAP)
    ).all()
    for sub, name, student_no, exam_title, course_id, course_title in exam_rows:
        status, tone = exam_display(sub)
        submitted_at = sub.submitted_at or sub.last_saved_at
        rows.append((
            submitted_at,
            "exam", sub.id, name, student_no, exam_title,
            course_id, course_title, status, tone, None, None, None, sub.score,
            submitted_at, f"/teacher/exams/{sub.exam_id}/grades/{sub.id}",
        ))

    rows.sort(key=lambda r: _desc_sort_key(r[0]))
    return rows[:RECENT_SUBMISSION_CAP]


def build_teacher_dashboard_counts(db: Session, user: User, now: datetime | None = None):
    """侧栏徽标轻量计数；只统计数量，不组装工作队列与最近提交明细。"""
    now = now or datetime.now(timezone.utc)
    owned_ids = list(db.scalars(select(Course.id).where(teacher_course_condition(user.id))).all())
    if not owned_ids:
        unread = unread_announcement_count(db, user, now)
        from app.schemas.dashboard import TeacherDashboardCounts

        return TeacherDashboardCounts(unread_announcement_count=unread)

    from app.schemas.dashboard import TeacherDashboardCounts

    exp_rows, ai_rows, exam_ai_rows = _teacher_pending_review_rows(db, owned_ids)
    pending_review = len(exp_rows) + len(ai_rows) + len(exam_ai_rows)
    assignment_pending = db.scalar(
        select(func.count(Submission.id))
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .join(Course, Course.id == Assignment.course_id)
        .where(
            Course.id.in_(owned_ids),
            Assignment.status == "published",
            Submission.grading_status.in_(("pending", "queued", "running")),
        )
    ) or 0
    pending_grading = len(exp_rows) + assignment_pending
    upcoming_deadlines = db.scalar(
        select(func.count(Assignment.id))
        .where(
            Assignment.course_id.in_(owned_ids),
            Assignment.status == "published",
            Assignment.due_at > now,
            Assignment.due_at <= now + timedelta(hours=DEADLINE_WINDOW_HOURS),
        )
    ) or 0
    pending_release = len(_teacher_pending_exam_release_rows(db, owned_ids, now))
    return TeacherDashboardCounts(
        pending_grading_count=pending_grading,
        pending_review_count=pending_review,
        upcoming_deadline_count=upcoming_deadlines,
        pending_release_count=pending_release,
        unread_announcement_count=unread_announcement_count(db, user, now),
    )


def build_teacher_dashboard(db: Session, user: User, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    summary = TeacherSummary()
    work_items: list[WorkItem] = []
    course_health: list[CourseHealth] = []
    activity: list[TeacherActivity] = []
    recent_submissions: list[RecentSubmission] = []
    managed: list[ManagedCourse] = []

    courses = (
        db.scalars(
            select(Course).where(teacher_course_condition(user.id)).order_by(Course.title)
        ).all()
    )
    owned_ids = [course.id for course in courses]
    summary.course_count = len(courses)
    summary.active_course_count = sum(
        1 for course in courses if course.status == "published"
    )
    managed = [ManagedCourse(id=course.id, title=course.title) for course in courses]

    if owned_ids:
        summary.student_count = (
            db.scalar(
                select(func.count(distinct(CourseEnrollment.student_id))).where(
                    CourseEnrollment.course_id.in_(owned_ids),
                    CourseEnrollment.status == "enrolled",
                )
            )
            or 0
        )

        exp_rows, ai_rows, exam_ai_rows = _teacher_pending_review_rows(db, owned_ids)
        assignment_pending_rows = _teacher_pending_assignment_rows(db, owned_ids)
        exam_release_rows = _teacher_pending_exam_release_rows(db, owned_ids, now)
        summary.pending_review_count = len(exp_rows) + len(ai_rows) + len(exam_ai_rows)
        summary.pending_grading_count = len(exp_rows) + len(assignment_pending_rows)
        summary.pending_release_count = len(exam_release_rows)

        # ── 7 天内截止的作业（urgency 分级仍按 24/72 小时）──
        deadline_rows = db.execute(
            select(Assignment, Course.id, Course.title)
            .join(Course, Course.id == Assignment.course_id)
            .where(
                Assignment.course_id.in_(owned_ids),
                Assignment.status == "published",
                Assignment.due_at > now,
                Assignment.due_at <= now + timedelta(hours=DEADLINE_WINDOW_HOURS),
            )
            .order_by(Assignment.due_at)
        ).all()
        summary.upcoming_deadline_count = len(deadline_rows)

        # ── 工作队列：实验待评分按课程+入口聚合 ──
        exp_groups: dict[tuple, dict] = {}
        for sub, record, student_name, student_no, lesson_id, lesson_title, lesson_due, module_id, module_name, module_due, course_id, course_title in exp_rows:
            key = (course_id or 0, lesson_id, module_id)
            bucket = exp_groups.setdefault(key, {
                "count": 0, "oldest": sub.submitted_at, "last": sub.submitted_at,
                "title": lesson_title or module_name,
                "due_at": lesson_due or module_due,
                "course_title": course_title,
            })
            bucket["count"] += 1
            if sub.submitted_at is not None and (
                bucket["oldest"] is None or sub.submitted_at < bucket["oldest"]
            ):
                bucket["oldest"] = sub.submitted_at
            if sub.submitted_at is not None and (
                bucket["last"] is None or sub.submitted_at > bucket["last"]
            ):
                bucket["last"] = sub.submitted_at
        for (course_id, lesson_id, module_id), bucket in exp_groups.items():
            work_items.append(
                WorkItem(
                    kind="experiment_review",
                    id=lesson_id or module_id or course_id,
                    title=f"{bucket['title']} · {bucket['count']} 份提交待评分",
                    course_id=course_id or None,
                    course_title=bucket["course_title"],
                    detail=f"共 {bucket['count']} 份等待教师反馈",
                    count=bucket["count"], status="pending_grading",
                    time_at=bucket["due_at"] or bucket["oldest"],
                    urgency=_task_urgency(now, bucket["due_at"], bucket["oldest"]),
                    route=_exp_entry_route(lesson_id, module_id),
                )
            )

        # ── 作业判题队列待评分，按作业聚合 ──
        assign_groups: dict[int, dict] = {}
        for sub, assignment, question_title, course_id, course_title in assignment_pending_rows:
            bucket = assign_groups.setdefault(assignment.id, {
                "count": 0, "oldest": sub.created_at, "title": assignment.title,
                "due_at": assignment.due_at, "course_id": course_id,
                "course_title": course_title,
            })
            bucket["count"] += 1
            if sub.created_at is not None and (
                bucket["oldest"] is None or sub.created_at < bucket["oldest"]
            ):
                bucket["oldest"] = sub.created_at
        for assignment_id, bucket in assign_groups.items():
            work_items.append(
                WorkItem(
                    kind="assignment_grading",
                    id=assignment_id,
                    title=f"{bucket['title']} · {bucket['count']} 份提交待评分",
                    course_id=bucket["course_id"],
                    course_title=bucket["course_title"],
                    detail=f"{bucket['count']} 份提交仍在判题队列",
                    count=bucket["count"], status="pending_grading",
                    time_at=bucket["due_at"] or bucket["oldest"],
                    urgency=_task_urgency(now, bucket["due_at"], bucket["oldest"]),
                    route=f"/teacher/submissions/unified?kind=assignment&entry_id={assignment_id}&status=pending_grading",
                )
            )

        # ── AI 评分待复核，按作业/考试聚合 ──
        ai_groups: dict[tuple, dict] = {}
        for cg, sub, assignment, course_id, course_title, student_name in ai_rows:
            key = ("assignment", assignment.id)
            bucket = ai_groups.setdefault(key, {
                "count": 0, "id": cg.id, "oldest": cg.finished_at,
                "title": assignment.title, "course_id": course_id,
                "course_title": course_title,
            })
            bucket["count"] += 1
            if cg.finished_at is not None and (
                bucket["oldest"] is None or cg.finished_at < bucket["oldest"]
            ):
                bucket["oldest"] = cg.finished_at
        for cg, answer, exam, course_id, course_title, student_name in exam_ai_rows:
            key = ("exam", exam.id)
            bucket = ai_groups.setdefault(key, {
                "count": 0, "id": cg.id, "oldest": cg.finished_at,
                "title": exam.title, "course_id": course_id,
                "course_title": course_title,
            })
            bucket["count"] += 1
            if cg.finished_at is not None and (
                bucket["oldest"] is None or cg.finished_at < bucket["oldest"]
            ):
                bucket["oldest"] = cg.finished_at
        for (ai_kind, _target_id), bucket in ai_groups.items():
            work_items.append(
                WorkItem(
                    kind="ai_review",
                    id=bucket["id"],
                    title=f"AI 评分待复核 · {bucket['count']} 份",
                    course_id=bucket["course_id"],
                    course_title=bucket["course_title"],
                    detail=f"{bucket['title']} · 模型已给出建议分",
                    count=bucket["count"], status="review_required",
                    time_at=bucket["oldest"],
                    urgency=_task_urgency(now, None, bucket["oldest"]),
                    route=f"/teacher/ai-grading?kind={ai_kind}",
                )
            )

        # ── 考试待发布成绩 ──
        for exam, course_id, course_title in exam_release_rows:
            work_items.append(
                WorkItem(
                    kind="exam_release",
                    id=exam.id,
                    title=f"{exam.title} · 待发布成绩",
                    course_id=course_id, course_title=course_title,
                    detail="已全部判题，等待发布讲评",
                    count=None, status="pending_release",
                    time_at=exam.end_at,
                    urgency=_task_urgency(now, exam.end_at, exam.end_at),
                    route=f"/teacher/exams/{exam.id}/grades",
                )
            )

        deadline_stats = _deadline_stats_batch(db, [a for a, _cid, _ct in deadline_rows])
        for assignment, course_id, course_title in deadline_rows:
            submitted, expected = deadline_stats[assignment.id]
            # 工作队列只提示参与不完全的截止：全员已提交（且有人选课）时省略
            if expected > 0 and submitted >= expected:
                continue
            work_items.append(
                WorkItem(
                    kind="deadline", id=assignment.id, title=assignment.title,
                    course_id=course_id, course_title=course_title,
                    detail=f"{submitted}/{expected} 已提交", time_at=assignment.due_at,
                    urgency=_urgency(_hours_until(now, assignment.due_at)),
                    route=f"/teacher/assignments/{assignment.id}/edit",
                )
            )
        work_items.sort(
            key=lambda w: (_URGENCY_RANK[w.urgency], _asc_sort_key(w.time_at))
        )
        work_items = work_items[:PRIORITY_CAP]

        # ── 课程概览：按课程聚合待复核与截止数 ──
        exp_by_course: dict[int, int] = {}
        ai_by_course: dict[int, int] = {}
        for sub, record, _n, _no, _lid, _lt, _ld, _mid, _mn, _md, course_id, _ct in exp_rows:
            exp_by_course[course_id] = exp_by_course.get(course_id, 0) + 1
        for cg, _s, _a, course_id, _ct, _n in ai_rows:
            ai_by_course[course_id] = ai_by_course.get(course_id, 0) + 1
        for cg, _ans, _e, course_id, _ct, _n in exam_ai_rows:
            ai_by_course[course_id] = ai_by_course.get(course_id, 0) + 1
        deadline_by_course: dict[int, list] = {}
        for assignment, course_id, _ct in deadline_rows:
            deadline_by_course.setdefault(course_id, []).append(assignment)

        # 批量统计所有课程的选课学生数（一次查询）
        enrolled_counts = dict(
            db.execute(
                select(CourseEnrollment.course_id, func.count(distinct(CourseEnrollment.student_id)))
                .where(
                    CourseEnrollment.course_id.in_(owned_ids),
                    CourseEnrollment.status == "enrolled",
                )
                .group_by(CourseEnrollment.course_id)
            ).all()
        )

        for course in courses:
            cid = course.id
            deadline_list = deadline_by_course.get(cid, [])
            at_risk_submitted = sum(deadline_stats[a.id][0] for a in deadline_list)
            at_risk_expected = sum(deadline_stats[a.id][1] for a in deadline_list)
            course_health.append(
                CourseHealth(
                    course_id=cid, title=course.title,
                    student_count=enrolled_counts.get(cid, 0),
                    pending_review_count=exp_by_course.get(cid, 0)
                    + ai_by_course.get(cid, 0),
                    upcoming_deadline_count=len(deadline_list),
                    at_risk_submitted_count=at_risk_submitted,
                    at_risk_expected_count=at_risk_expected,
                    route=f"/teacher/courses/{cid}/manage",
                )
            )
        course_health.sort(
            key=lambda h: (
                h.upcoming_deadline_count == 0,
                -h.upcoming_deadline_count,
                -h.pending_review_count,
                h.title,
            )
        )

        # ── 最近动态：真实实验提交（兼容旧客户端） ──
        activity_rows = db.execute(
            select(
                ExperimentSubmission, User.real_name,
                Lesson.title, ExperimentModule.name, Course.id, Course.title,
            )
            .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
            .outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id)
            .outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
            .outerjoin(Chapter, Chapter.id == Lesson.chapter_id)
            .outerjoin(Course, Course.id == Chapter.course_id)
            .join(User, User.id == ExperimentRecord.student_id)
            .where(Course.id.in_(owned_ids))
            .order_by(ExperimentSubmission.submitted_at.desc())
            .limit(ACTIVITY_CAP)
        ).all()
        for sub, student_name, lesson_title, module_name, course_id, course_title in activity_rows:
            item_title = lesson_title or module_name
            activity.append(
                TeacherActivity(
                    kind="experiment_submission", id=sub.id,
                    title=f"{student_name} 提交了{item_title}",
                    course_title=course_title, actor_name=student_name,
                    happened_at=sub.submitted_at, route=f"/teacher/submissions/{sub.id}",
                )
            )

        # ── 最近提交表格（混合实验/作业/考试） ──
        for row in _teacher_recent_submissions(db, owned_ids):
            (
                _submitted_at, kind, item_id, student_name, student_no, entry_title,
                course_id, course_title, status, status_tone,
                tests_passed, tests_total, ai_score, score, _time_at, route,
            ) = row
            recent_submissions.append(
                RecentSubmission(
                    kind=kind, id=item_id, student_name=student_name,
                    student_no=student_no, entry_title=entry_title,
                    course_id=course_id, course_title=course_title,
                    status=status, status_tone=status_tone,
                    tests_passed=tests_passed, tests_total=tests_total,
                    ai_score=ai_score, score=score, submitted_at=_time_at,
                    route=route,
                )
            )

    announcements = list_visible_announcements(db, user, now, limit=PRIORITY_CAP)
    summary.unread_announcement_count = unread_announcement_count(db, user, now)
    return TeacherDashboardRead(
        summary=summary, work_items=work_items, course_health=course_health,
        recent_activity=activity, recent_submissions=recent_submissions,
        managed_courses=managed, announcements=announcements,
    )
