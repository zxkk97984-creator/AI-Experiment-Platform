"""Shared course content and offering-scoped chapter activity references."""
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin


class CourseMaterial(TimestampMixin, Base):
    __tablename__ = "course_materials"
    __table_args__ = (
        UniqueConstraint("storage_object_id", name="uq_material_object_exclusive"),
        CheckConstraint("source IN ('file', 'external')", name="ck_material_source"),
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_material_status"),
        CheckConstraint("version >= 1", name="ck_material_version"),
        Index("ix_material_course_status_order", "course_id", "status", "order_index"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(20), default="lecture")
    source: Mapped[str] = mapped_column(String(20), default="file")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    external_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    storage_object_id: Mapped[int | None] = mapped_column(ForeignKey("storage_objects.id", ondelete="RESTRICT"), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Previous versions preserve descriptive metadata, never a live file binding.
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class ChapterActivityLink(TimestampMixin, Base):
    __tablename__ = "chapter_activity_links"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN assignment_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN exam_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN lesson_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN module_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_chapter_activity_one_target",
        ),
        Index("ix_chapter_activity_offering_chapter", "offering_id", "chapter_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offering_id: Mapped[int] = mapped_column(ForeignKey("course_offerings.id", ondelete="CASCADE"))
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"))
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), nullable=True)
    exam_id: Mapped[int | None] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), nullable=True)
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), nullable=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("experiment_modules.id", ondelete="CASCADE"), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    @property
    def target_type(self):
        return next(kind for kind in ("assignment", "exam", "lesson", "module") if getattr(self, f"{kind}_id") is not None)

    @property
    def target_id(self):
        return getattr(self, f"{self.target_type}_id")
