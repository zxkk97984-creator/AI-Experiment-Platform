"""Administrator-owned teaching authority; course ownership is content only."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin


class CourseOffering(TimestampMixin, Base):
    __tablename__ = 'course_offerings'
    __table_args__ = (UniqueConstraint('course_id', 'academic_term_id', 'teaching_class_id', name='uq_offering_course_term_class'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey('courses.id'), index=True)
    academic_term_id: Mapped[int] = mapped_column(ForeignKey('academic_terms.id'), index=True)
    teaching_class_id: Mapped[int] = mapped_column(ForeignKey('teaching_classes.id'), index=True)
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), index=True)
    status: Mapped[str] = mapped_column(String(32), default='pending_assignment')
    leaderboard_named: Mapped[bool] = mapped_column(Boolean, default=False)


class OfferingEnrollment(TimestampMixin, Base):
    __tablename__ = 'offering_enrollments'
    __table_args__ = (UniqueConstraint('offering_id', 'student_id', name='uq_offering_student'),
                      UniqueConstraint('active_scope_key', name='uq_student_active_course_term'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offering_id: Mapped[int] = mapped_column(ForeignKey('course_offerings.id'), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), default='active')
    origin: Mapped[str] = mapped_column(String(16), default='class', server_default='class')
    active_scope_key: Mapped[str | None] = mapped_column(String(120))


class OfferingExperiment(TimestampMixin, Base):
    __tablename__ = 'offering_experiments'
    __table_args__ = (UniqueConstraint('offering_id', 'entry_kind', 'entry_id', name='uq_offering_experiment'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offering_id: Mapped[int] = mapped_column(ForeignKey('course_offerings.id'), index=True)
    entry_kind: Mapped[str] = mapped_column(String(16))
    entry_id: Mapped[int] = mapped_column(Integer)
    template_version_id: Mapped[int] = mapped_column(ForeignKey('notebook_template_versions.id'))
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey('lab_assessments.id'))
    pending_assessment_id: Mapped[int | None] = mapped_column(ForeignKey('lab_assessments.id'))
    status: Mapped[str] = mapped_column(String(24), default='draft')
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
