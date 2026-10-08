from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Course,
    CourseEnrollment,
    CourseTeachingClass,
    TeachingClassStudent,
)


def sync_course_class_enrollments(db: Session, course: Course) -> None:
    """Materialize active class memberships into the existing enrollment table.

    Manual enrolled/dropped decisions are authoritative and are never overwritten.
    Class-origin rows are kept in sync with all currently bound classes.
    """
    class_ids = list(
        db.scalars(
            select(CourseTeachingClass.teaching_class_id).where(
                CourseTeachingClass.course_id == course.id
            )
        ).all()
    )
    student_ids: set[int] = set()
    if class_ids:
        student_ids = set(
            db.scalars(
                select(TeachingClassStudent.student_id).where(
                    TeachingClassStudent.teaching_class_id.in_(class_ids),
                    TeachingClassStudent.status == "active",
                )
            ).all()
        )

    enrollments = {
        row.student_id: row
        for row in db.scalars(
            select(CourseEnrollment).where(CourseEnrollment.course_id == course.id)
        ).all()
    }
    for student_id in student_ids:
        enrollment = enrollments.get(student_id)
        if enrollment is None:
            db.add(CourseEnrollment(
                course_id=course.id, student_id=student_id,
                status="enrolled", origin="class",
            ))
        elif enrollment.origin == "class" or (enrollment.origin == "self" and enrollment.status == "dropped"):
            enrollment.origin = "class"
            enrollment.status = "enrolled"

    for student_id, enrollment in enrollments.items():
        if enrollment.origin == "class" and student_id not in student_ids:
            enrollment.status = "dropped"

    from app.models.teaching import CourseOffering, OfferingEnrollment
    from app.services.offering_service import enroll_student
    offerings = list(db.scalars(select(CourseOffering).where(CourseOffering.course_id == course.id, CourseOffering.status == 'active')))
    # Remove first, so a genuine class transfer can acquire the unique grade scope.
    for offering in offerings:
        current = set(db.scalars(select(TeachingClassStudent.student_id).where(TeachingClassStudent.teaching_class_id == offering.teaching_class_id, TeachingClassStudent.status == 'active')))
        members = list(db.scalars(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == offering.id)))
        for member in members:
            if member.status == 'active' and member.origin == 'class' and member.student_id not in current:
                member.status, member.active_scope_key = 'removed', None
    db.flush()
    for offering in offerings:
        current = db.scalars(select(TeachingClassStudent.student_id).where(TeachingClassStudent.teaching_class_id == offering.teaching_class_id, TeachingClassStudent.status == 'active'))
        for student_id in current:
            member = db.scalar(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == offering.id, OfferingEnrollment.student_id == student_id))
            if member and member.status == 'transferred':
                continue
            enroll_student(db, offering, student_id)


def sync_courses_for_class(db: Session, teaching_class_id: int) -> None:
    course_ids = db.scalars(
        select(CourseTeachingClass.course_id).where(
            CourseTeachingClass.teaching_class_id == teaching_class_id
        )
    ).all()
    for course_id in course_ids:
        course = db.get(Course, course_id)
        if course:
            sync_course_class_enrollments(db, course)


def resolve_student_identity(db, student_no, username=None, real_name=None):
    """Never choose arbitrarily when the supplied identifiers disagree."""
    from app.models import User
    by_no = db.scalar(select(User).where(User.student_no == student_no)) if student_no else None
    by_name = db.scalar(select(User).where(User.username == username)) if username else None
    if student_no and username and (by_no is None or by_name is None or by_no.id != by_name.id):
        return None
    user = by_no if student_no else by_name
    if not user or user.role != 'student' or user.status != 'active':
        return None
    if real_name and real_name.strip() != user.real_name:
        return None
    return user
