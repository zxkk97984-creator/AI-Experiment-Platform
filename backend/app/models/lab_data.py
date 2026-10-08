"""Immutable experiment datasets and model artifact manifests.

Storage foreign keys deliberately use RESTRICT: a referenced model or dataset
must never be collected as an unreferenced temporary object.
"""
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import BIGINT_PK, TimestampMixin


class LabDatasetVersion(TimestampMixin, Base):
    __tablename__ = "lab_dataset_versions"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    task_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_object_id: Mapped[int] = mapped_column(ForeignKey("storage_objects.id", ondelete="RESTRICT"))
    sha256: Mapped[str] = mapped_column(String(64))


class LabArtifact(TimestampMixin, Base):
    __tablename__ = "lab_artifacts"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    record_id: Mapped[int | None] = mapped_column(ForeignKey("experiment_records.id"), nullable=True, index=True)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("lab_assessments.id"), nullable=True, index=True)
    # LabRun.artifact_id is the authoritative FK; this audit ID avoids a
    # circular migration dependency and is never used for authorization.
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(24), default="external")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    raw_object_id: Mapped[int | None] = mapped_column(ForeignKey("storage_objects.id", ondelete="RESTRICT"), nullable=True)
    code_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LabFile(Base):
    __tablename__ = "lab_files"
    __table_args__ = (
        CheckConstraint("(dataset_version_id IS NULL) != (artifact_id IS NULL)", name="ck_lab_files_parent"),
        UniqueConstraint("dataset_version_id", "path", name="uq_lab_files_dataset_path"),
        UniqueConstraint("artifact_id", "path", name="uq_lab_files_artifact_path"),
        Index("ix_lab_files_object", "object_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    dataset_version_id: Mapped[int | None] = mapped_column(ForeignKey("lab_dataset_versions.id", ondelete="CASCADE"), nullable=True)
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("lab_artifacts.id", ondelete="CASCADE"), nullable=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("storage_objects.id", ondelete="RESTRICT"))
    path: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(40))
