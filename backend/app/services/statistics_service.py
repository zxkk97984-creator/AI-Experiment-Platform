"""教师成绩统计总览服务。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Course, CourseEnrollment, Exam, ExamSubmission, User
from app.services.offering_service import teacher_course_condition
from app.services.audience_service import effective_student_ids
from app.schemas.statistics import ExamStatisticsRead, TeacherGradeStatisticsRead


def build_teacher_grade_statistics(db: Session, user: User) -> TeacherGradeStatisticsRead:
    courses = db.scalars(
        select(Course).where(teacher_course_condition(user.id)).order_by(Course.title)
    ).all()
    course_ids = [course.id for course in courses]
    result = TeacherGradeStatisticsRead(
        course_count=len(courses),
        active_course_count=sum(1 for course in courses if course.status == "published"),
    )
    if not course_ids:
        return result

    result.student_count = (
        db.scalar(
            select(func.count(func.distinct(CourseEnrollment.student_id))).where(
                CourseEnrollment.course_id.in_(course_ids),
                CourseEnrollment.status == "enrolled",
            )
        )
        or 0
    )

    exam_rows = db.execute(
        select(Exam, Course.title)
        .join(Course, Course.id == Exam.course_id)
        .where(Exam.course_id.in_(course_ids))
        .order_by(Exam.start_at.desc(), Exam.id.desc())
    ).all()
    result.exam_count = len(exam_rows)

    all_scores: list[float] = []
    for exam, course_title in exam_rows:
        submissions = db.scalars(
            select(ExamSubmission).where(ExamSubmission.exam_id == exam.id)
        ).all()
        audience_ids = effective_student_ids(
            db, task_type="exam", task_id=exam.id, course=exam.course,
        )
        expected = len(audience_ids)
        expected = max(expected, len({submission.student_id for submission in submissions}))
        scores = [submission.score for submission in submissions if submission.score is not None]
        graded_count = len(scores)
        avg = round(sum(scores) / graded_count, 1) if scores else None
        pass_count = sum(1 for score in scores if score >= 60)
        pass_rate = round(pass_count * 100 / graded_count, 1) if graded_count else 0.0
        all_scores.extend(scores)
        result.exams.append(
            ExamStatisticsRead(
                id=exam.id,
                title=exam.title,
                course_id=exam.course_id,
                course_title=course_title,
                status=exam.status,
                review_released=exam.review_released_at is not None,
                expected_count=expected,
                graded_count=graded_count,
                average_score=avg,
                pass_rate=pass_rate,
                route=f"/teacher/exams/{exam.id}/grades",
            )
        )

    result.graded_count = len(all_scores)
    result.average_score = round(sum(all_scores) / len(all_scores), 1) if all_scores else None
    result.pass_rate = round(
        sum(1 for score in all_scores if score >= 60) * 100 / len(all_scores), 1
    ) if all_scores else 0.0
    return result
