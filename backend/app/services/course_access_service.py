"""Published content discoverable only through administrator-granted offerings."""
from sqlalchemy import and_, select
from app.models import Course
from app.models.teaching import CourseOffering, OfferingEnrollment

def student_visible_course_predicate(student_id: int):
    return and_(Course.status == 'published', Course.id.in_(select(CourseOffering.course_id).join(OfferingEnrollment).where(OfferingEnrollment.student_id == student_id, OfferingEnrollment.status == 'active', CourseOffering.status == 'active')))
