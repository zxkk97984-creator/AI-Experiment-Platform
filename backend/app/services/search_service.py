"""全局搜索服务——按角色收敛可见范围，统一返回课程/作业/考试/学生/提交摘要。"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Assignment, Chapter, Course, CourseEnrollment, Exam, ExperimentModule,
    ExperimentRecord, ExperimentSubmission, JudgeQuestion, Lesson, Submission, User,
)
from app.schemas.search import SearchResultItem, SearchResponse
from app.services.course_access_service import student_visible_course_predicate
from app.services.offering_service import teacher_course_condition
from app.services.audience_service import assignment_visible_condition, exam_visible_condition

SEARCH_LIMIT = 6


def _like(q: str) -> str:
    return f"%{q}%"


def _teacher_prefix(user: User) -> str:
    return "/admin" if user.role == "admin" else "/teacher"


def search_all(db: Session, user: User, q: str) -> SearchResponse:
    q = (q or "").strip()
    if not q:
        return SearchResponse()
    like = _like(q)
    response = SearchResponse()

    # ── 课程 ──
    course_stmt = select(Course).order_by(Course.title).limit(SEARCH_LIMIT)
    if user.role == "teacher":
        course_stmt = select(Course).where(teacher_course_condition(user.id)).order_by(Course.title).limit(SEARCH_LIMIT)
    elif user.role == "student":
        course_stmt = (
            select(Course)
            .where(student_visible_course_predicate(user.id))
            .order_by(Course.title)
            .limit(SEARCH_LIMIT)
        )
    elif user.role != "admin":
        course_stmt = select(Course).where(Course.id == -1).limit(1)
    course_stmt = course_stmt.where(or_(Course.title.ilike(like), Course.code.ilike(like)))
    for course in db.scalars(course_stmt).all():
        route = (
            f"{_teacher_prefix(user)}/courses/{course.id}/manage"
            if user.role in ("teacher", "admin")
            else f"/student/courses/{course.id}"
        )
        response.courses.append(SearchResultItem(
            id=course.id, title=course.title,
            subtitle=course.code or "课程", route=route,
        ))

    # 教师/管理员可查自己课程范围内的作业
    if user.role in ("teacher", "admin"):
        assignment_stmt = (
            select(Assignment, Course.title)
            .join(Course, Course.id == Assignment.course_id)
            .where(Assignment.title.ilike(like))
            .order_by(Assignment.updated_at.desc())
            .limit(SEARCH_LIMIT)
        )
        if user.role == "teacher":
            assignment_stmt = assignment_stmt.where(teacher_course_condition(user.id))
        for assignment, course_title in db.execute(assignment_stmt).all():
            response.assignments.append(SearchResultItem(
                id=assignment.id, title=assignment.title, subtitle=course_title,
                route=f"{_teacher_prefix(user)}/assignments/{assignment.id}/edit",
            ))
        exam_stmt = (
            select(Exam, Course.title)
            .join(Course, Course.id == Exam.course_id)
            .where(Exam.title.ilike(like))
            .order_by(Exam.updated_at.desc())
            .limit(SEARCH_LIMIT)
        )
        if user.role == "teacher":
            exam_stmt = exam_stmt.where(teacher_course_condition(user.id))
        for exam, course_title in db.execute(exam_stmt).all():
            response.exams.append(SearchResultItem(
                id=exam.id, title=exam.title, subtitle=course_title,
                route=f"{_teacher_prefix(user)}/exams/{exam.id}/grades",
            ))

    if user.role == "student":
        enrolled = select(CourseEnrollment.course_id).where(
            CourseEnrollment.student_id == user.id,
            CourseEnrollment.status == "enrolled",
        )
        assignment_stmt = (
            select(Assignment, Course.title)
            .join(Course, Course.id == Assignment.course_id)
            .where(
                Course.status == "published",
                assignment_visible_condition(user.id),
                Assignment.title.ilike(like),
            )
            .order_by(Assignment.published_at.desc())
            .limit(SEARCH_LIMIT)
        )
        for assignment, course_title in db.execute(assignment_stmt).all():
            response.assignments.append(SearchResultItem(
                id=assignment.id, title=assignment.title, subtitle=course_title,
                route=f"/student/assignments/{assignment.id}",
            ))
        exam_stmt = (
            select(Exam, Course.title)
            .join(Course, Course.id == Exam.course_id)
            .where(
                Course.status == "published",
                exam_visible_condition(user.id),
                Exam.title.ilike(like),
            )
            .order_by(Exam.start_at.desc())
            .limit(SEARCH_LIMIT)
        )
        for exam, course_title in db.execute(exam_stmt).all():
            response.exams.append(SearchResultItem(
                id=exam.id, title=exam.title, subtitle=course_title,
                route=f"/student/exams/{exam.id}",
            ))

    # ── 学生：教师只查自己课程在册学生；学生不查其他学生 ──
    if user.role in ("teacher", "admin"):
        student_stmt = (
            select(User, Course.title)
            .join(CourseEnrollment, CourseEnrollment.student_id == User.id)
            .join(Course, Course.id == CourseEnrollment.course_id)
            .where(
                CourseEnrollment.status == "enrolled",
                or_(User.real_name.ilike(like), User.username.ilike(like), User.student_no.ilike(like)),
            )
            .distinct()
            .order_by(User.real_name)
            .limit(SEARCH_LIMIT)
        )
        if user.role == "teacher":
            student_stmt = student_stmt.where(teacher_course_condition(user.id))
        for student, course_title in db.execute(student_stmt).all():
            response.students.append(SearchResultItem(
                id=student.id, title=student.real_name or student.username,
                subtitle=student.student_no or student.username,
                route=f"{_teacher_prefix(user)}/classes?q={student.real_name or student.username}",
                meta=course_title,
            ))

    # ── 提交：按角色范围查实验与作业提交 ──
    if user.role in ("teacher", "admin"):
        course_ids = []
        if user.role == "teacher":
            course_ids = list(db.scalars(select(Course.id).where(teacher_course_condition(user.id))).all())
        exp_stmt = (
            select(ExperimentSubmission, User.real_name, User.student_no, Course.id, Course.title)
            .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
            .outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id)
            .outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id)
            .outerjoin(Chapter, Chapter.id == Lesson.chapter_id)
            .outerjoin(Course, Course.id == func.coalesce(Chapter.course_id, select(CourseOffering.course_id).where(CourseOffering.id == ExperimentRecord.offering_id).correlate(ExperimentRecord).scalar_subquery()))
            .join(User, User.id == ExperimentRecord.student_id)
            .where(
                or_(User.real_name.ilike(like), User.student_no.ilike(like))
            )
            .order_by(ExperimentSubmission.submitted_at.desc())
            .limit(SEARCH_LIMIT)
        )
        assign_stmt = (
            select(Submission, User.real_name, User.student_no, Course.id, Course.title)
            .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
            .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
            .join(Course, Course.id == Assignment.course_id)
            .join(User, User.id == Submission.student_id)
            .where(or_(User.real_name.ilike(like), User.student_no.ilike(like)))
            .order_by(Submission.created_at.desc())
            .limit(SEARCH_LIMIT)
        )
        if user.role == "teacher":
            exp_stmt = exp_stmt.where(Course.id.in_(course_ids))
            assign_stmt = assign_stmt.where(Course.id.in_(course_ids))
        for sub, name, student_no, course_id, course_title in db.execute(exp_stmt).all():
            response.submissions.append(SearchResultItem(
                id=sub.id, title=f"{name or '学生'} 的实验提交",
                subtitle=f"{student_no or ''} · {course_title or '独立实验'}",
                route=f"{_teacher_prefix(user)}/submissions/{sub.id}",
            ))
        for sub, name, student_no, course_id, course_title in db.execute(assign_stmt).all():
            response.submissions.append(SearchResultItem(
                id=sub.id, title=f"{name or '学生'} 的作业提交",
                subtitle=f"{student_no or ''} · {course_title or ''}",
                route=f"{_teacher_prefix(user)}/judge-submissions/{sub.id}",
            ))

    if user.role == "student":
        exp_stmt = (
            select(ExperimentSubmission, ExperimentRecord.student_id)
            .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
            .where(ExperimentRecord.student_id == user.id)
            .order_by(ExperimentSubmission.submitted_at.desc())
            .limit(SEARCH_LIMIT)
        )
        # 学生搜索主要查自己的作业提交
        assign_stmt = (
            select(Submission)
            .where(Submission.student_id == user.id)
            .order_by(Submission.created_at.desc())
            .limit(SEARCH_LIMIT)
        )
        for sub, _sid in db.execute(exp_stmt).all():
            response.submissions.append(SearchResultItem(
                id=sub.id, title="我的实验提交", subtitle=None,
                route=f"/student/courses",
            ))
        for sub in db.scalars(assign_stmt).all():
            response.submissions.append(SearchResultItem(
                id=sub.id, title="我的作业提交", subtitle=None,
                route=f"/student/submissions/{sub.id}",
            ))

    return response

from app.models.teaching import CourseOffering
