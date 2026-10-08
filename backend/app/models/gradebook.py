"""Offering-scoped gradebook configuration and immutable publication snapshots."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class Gradebook(Base):
    __tablename__ = 'gradebooks'
    __table_args__ = (UniqueConstraint('offering_id', name='uq_gradebook_offering'),
                      CheckConstraint('revision >= 1', name='ck_gradebook_revision'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offering_id: Mapped[int] = mapped_column(ForeignKey('course_offerings.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    published_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GradebookCategory(Base):
    __tablename__ = 'gradebook_categories'
    __table_args__ = (UniqueConstraint('gradebook_id', 'key', name='uq_gradebook_category_key'),
                      CheckConstraint('weight >= 0 AND weight <= 100', name='ck_gradebook_category_weight'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gradebook_id: Mapped[int] = mapped_column(ForeignKey('gradebooks.id', ondelete='CASCADE'), index=True)
    key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(120))
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    position: Mapped[int] = mapped_column(Integer, default=0)


class GradebookColumn(Base):
    __tablename__ = 'gradebook_columns'
    __table_args__ = (UniqueConstraint('gradebook_id', 'key', name='uq_gradebook_column_key'),
                      CheckConstraint('weight >= 0 AND weight <= 100', name='ck_gradebook_column_weight'),
                      CheckConstraint("source_kind IN ('manual', 'assignment', 'exam', 'experiment')", name='ck_gradebook_column_source'),
                      CheckConstraint("selection IN ('highest', 'latest')", name='ck_gradebook_column_selection'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gradebook_id: Mapped[int] = mapped_column(ForeignKey('gradebooks.id', ondelete='CASCADE'), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey('gradebook_categories.id', ondelete='CASCADE'), index=True)
    key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(120))
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    source_kind: Mapped[str] = mapped_column(String(24), default='manual')
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selection: Mapped[str] = mapped_column(String(20), default='highest')
    position: Mapped[int] = mapped_column(Integer, default=0)


class GradebookEntry(Base):
    __tablename__ = 'gradebook_entries'
    __table_args__ = (UniqueConstraint('column_id', 'student_id', name='uq_gradebook_entry'),
                      CheckConstraint("state IN ('scored', 'pending', 'missing', 'exempt', 'auto')", name='ck_gradebook_entry_state'),
                      CheckConstraint('score IS NULL OR (score >= 0 AND score <= 100)', name='ck_gradebook_entry_score'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    column_id: Mapped[int] = mapped_column(ForeignKey('gradebook_columns.id', ondelete='CASCADE'), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    score: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GradebookAdjustment(Base):
    __tablename__ = 'gradebook_adjustments'
    __table_args__ = (UniqueConstraint('gradebook_id', 'student_id', name='uq_gradebook_adjustment'),
                      CheckConstraint('score >= 0 AND score <= 100', name='ck_gradebook_adjustment_score'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gradebook_id: Mapped[int] = mapped_column(ForeignKey('gradebooks.id'), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    score: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    reason: Mapped[str] = mapped_column(Text)
    updated_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GradebookSnapshot(Base):
    __tablename__ = 'gradebook_snapshots'
    __table_args__ = (UniqueConstraint('gradebook_id', 'revision', name='uq_gradebook_snapshot_revision'),
                      CheckConstraint("kind IN ('published', 'archive')", name='ck_gradebook_snapshot_kind'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gradebook_id: Mapped[int] = mapped_column(ForeignKey('gradebooks.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    source_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
