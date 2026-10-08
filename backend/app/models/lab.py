"""Versioned lab configuration, durable jobs and append-only grade history."""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from app.database import Base
from app.models import TimestampMixin


class LabAssessment(TimestampMixin, Base):
    __tablename__ = "lab_assessments"
    __table_args__ = (UniqueConstraint("entry_key", "version", name="uq_lab_assessment_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_key: Mapped[str] = mapped_column(String(80), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    template_version_id: Mapped[int | None] = mapped_column(ForeignKey("notebook_template_versions.id"))
    template_draft_revision: Mapped[int | None] = mapped_column(Integer)
    template_draft_hash: Mapped[str | None] = mapped_column(String(64))
    config_hash: Mapped[str | None] = mapped_column(String(64))
    validated_hash: Mapped[str | None] = mapped_column(String(64))
    validation_run_id: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LabRecord(TimestampMixin, Base):
    __tablename__ = "lab_records"
    record_id: Mapped[int] = mapped_column(ForeignKey("experiment_records.id"), primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("lab_assessments.id"))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    selected_submission_id: Mapped[int | None] = mapped_column(ForeignKey("experiment_submissions.id"))


class LabRun(TimestampMixin, Base):
    __tablename__ = "lab_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_lab_run_request"),
        UniqueConstraint("active_student_id", name="uq_lab_active_student"),
        Index("ix_lab_run_queue", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    record_id: Mapped[int | None] = mapped_column(ForeignKey("experiment_records.id"))
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("lab_assessments.id"))
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("experiment_submissions.id"))
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("lab_dataset_versions.id"))
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("lab_artifacts.id"))
    active_student_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    request_key: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    device: Mapped[str] = mapped_column(String(16), default="cpu")
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    logs: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"), default="")
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LabSubmission(TimestampMixin, Base):
    __tablename__ = "lab_submissions"
    submission_id: Mapped[int] = mapped_column(ForeignKey("experiment_submissions.id"), primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("lab_assessments.id"))
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("lab_artifacts.id"))
    current_run_id: Mapped[int | None] = mapped_column(Integer)
    published_grade_id: Mapped[int | None] = mapped_column(Integer)
    submission_hash: Mapped[str] = mapped_column(String(64))
    quota_charged: Mapped[bool] = mapped_column(Boolean, default=True)
    review_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[str | None] = mapped_column(String(2000))


class LabGradeRevision(TimestampMixin, Base):
    __tablename__ = "lab_grade_revisions"
    __table_args__ = (CheckConstraint("total >= 0 AND total <= 100", name="ck_lab_grade_range"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("experiment_submissions.id"), index=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("lab_assessments.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("lab_runs.id"))
    source: Mapped[str] = mapped_column(String(20))
    total: Mapped[float] = mapped_column(Float)
    items: Mapped[list] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    feedback: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str | None] = mapped_column(String(2000))
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LabRegradeBatch(TimestampMixin, Base):
    __tablename__ = "lab_regrade_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("course_offerings.id"))
    assessment_id: Mapped[int] = mapped_column(ForeignKey("lab_assessments.id"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(24), default="running")
    reason: Mapped[str] = mapped_column(String(2000))
    submission_ids: Mapped[list] = mapped_column(JSON, default=list)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LabResourceLease(Base):
    """Single-host resource reservations shared by kernels and lab workers."""
    __tablename__ = "lab_resource_leases"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    token: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20))
    cpus: Mapped[float] = mapped_column(Float)
    memory_mb: Mapped[int] = mapped_column(Integer)
    gpu: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
