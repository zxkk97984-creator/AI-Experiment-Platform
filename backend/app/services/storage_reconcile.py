"""Read-only storage reconciliation and persistent quarantine recording.

The database and a physical storage backend cannot share one ACID
transaction.  This service therefore observes both sides, records findings
in a retention-backed quarantine ledger, and leaves physical deletion to
``StorageGarbageCollector`` after a later safety check.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Course,
    Lesson,
    NotebookTemplate,
    NotebookTemplateVersion,
    StorageObject,
    StorageQuarantine,
)
from app.database import Base
from app.storage import (
    StorageArea,
    StorageError,
    StorageNotFound,
    StorageQuarantineStatus,
    StorageService,
)


class StorageFindingKind(StrEnum):
    """Stable finding codes used by reports and the quarantine ledger."""

    MISSING_PHYSICAL = "missing_physical"
    UNTRACKED_PHYSICAL = "untracked_physical"
    UNREFERENCED_ACTIVE = "unreferenced_active"
    STALE_STAGING = "stale_staging"
    STALE_FAILED = "stale_failed"
    STALE_DELETING = "stale_deleting"
    DELETED_PHYSICAL_PRESENT = "deleted_physical_present"
    SIZE_MISMATCH = "size_mismatch"
    SHA256_MISMATCH = "sha256_mismatch"
    UNKNOWN_BACKEND = "unknown_backend"
    UNKNOWN_AREA = "unknown_area"
    INVALID_OBJECT_KEY = "invalid_object_key"
    STORAGE_ERROR = "storage_error"


@dataclass(frozen=True)
class StorageFinding:
    kind: str
    backend: str
    area: str
    object_key: str
    object_id: int | None = None
    quarantined: bool = False
    quarantine_until: datetime | None = None
    references: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StorageReconcileReport:
    dry_run: bool
    scanned_metadata_count: int
    scanned_physical_count: int
    findings: tuple[StorageFinding, ...]
    quarantined_count: int


_AREA_PREFIXES: tuple[tuple[StorageArea, str], ...] = (
    (StorageArea.COVERS, "covers"),
    (StorageArea.VIDEOS, "lessons"),
    (StorageArea.STUDIO, "templates"),
    (StorageArea.DATASETS, "datasets"),
    (StorageArea.ARTIFACTS, "artifacts"),
    (StorageArea.MATERIALS, "materials"),
)
_NAMESPACE_AREAS = {
    "course-covers": StorageArea.COVERS,
    "lesson-videos": StorageArea.VIDEOS,
    "studio-assets": StorageArea.STUDIO,
    "lab-datasets": StorageArea.DATASETS,
    "lab-artifacts": StorageArea.ARTIFACTS,
    "course-materials": StorageArea.MATERIALS,
}
_QUARANTINE_TABLE = "storage_quarantines"


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return _aware(value, datetime.now(timezone.utc))


def _is_stale(
    value: datetime | None,
    *,
    now: datetime,
    threshold: timedelta,
) -> bool:
    return now - _aware(value, now) >= threshold


def _safe_key_for_area(area: StorageArea, key: str) -> bool:
    prefix = dict(_AREA_PREFIXES)[area]
    return key.startswith(f"{prefix}/") and len(key) > len(prefix) + 1


def _area_for_object(obj: StorageObject) -> StorageArea | None:
    metadata = obj.metadata_json if isinstance(obj.metadata_json, dict) else {}
    configured = metadata.get("storage_area")
    if isinstance(configured, str):
        try:
            return StorageArea(configured)
        except ValueError:
            pass
    return _NAMESPACE_AREAS.get(obj.namespace)


def _record_reference(result: dict[int, list[str]], object_id: int, label: str) -> None:
    result.setdefault(object_id, []).append(label)


def business_references(db: Session) -> dict[int, tuple[str, ...]]:
    """Find every ORM foreign key that points to ``storage_objects.id``.

    The scan is metadata-driven so new manifest/business bindings are covered
    without adding another hard-coded branch to a business Service.  The
    quarantine ledger itself is intentionally excluded: a finding is not a
    business reference that can protect an object from collection.
    """

    references: dict[int, list[str]] = {}
    for table in Base.metadata.tables.values():
        if table.name == _QUARANTINE_TABLE:
            continue
        for column in table.columns:
            targets = {
                (foreign_key.column.table.name, foreign_key.column.name)
                for foreign_key in column.foreign_keys
            }
            if ("storage_objects", "id") not in targets:
                continue
            rows = db.execute(select(column)).scalars().all()
            for object_id in rows:
                if object_id is not None:
                    _record_reference(
                        references,
                        int(object_id),
                        f"{table.name}.{column.name}",
                    )
    return {object_id: tuple(sorted(labels)) for object_id, labels in references.items()}


def legacy_references(db: Session) -> dict[tuple[str, str], tuple[str, ...]]:
    """Protect pre-Phase-2B keys and Studio prefixes from orphan GC."""

    references: dict[tuple[str, str], list[str]] = {}

    for course_id, key in db.execute(select(Course.id, Course.cover)).all():
        if isinstance(key, str) and key.startswith("covers/"):
            references.setdefault((StorageArea.COVERS.value, key), []).append(
                f"courses.cover[{course_id}]"
            )

    for lesson_id, key in db.execute(select(Lesson.id, Lesson.video_storage_key)).all():
        if isinstance(key, str) and key.startswith("lessons/"):
            references.setdefault((StorageArea.VIDEOS.value, key), []).append(
                f"lessons.video_storage_key[{lesson_id}]"
            )

    studio_prefixes: list[tuple[str, str]] = []
    for template_id, prefix in db.execute(
        select(NotebookTemplate.id, NotebookTemplate.draft_assets_dir)
    ).all():
        if isinstance(prefix, str) and prefix:
            studio_prefixes.append((prefix.rstrip("/"), f"notebook_templates.draft_assets_dir[{template_id}]"))
    for version_id, prefix in db.execute(
        select(NotebookTemplateVersion.id, NotebookTemplateVersion.assets_dir)
    ).all():
        if isinstance(prefix, str) and prefix:
            studio_prefixes.append((prefix.rstrip("/"), f"notebook_template_versions.assets_dir[{version_id}]"))

    for prefix, label in studio_prefixes:
        for (area, key), labels in list(references.items()):
            if area == StorageArea.STUDIO.value and (
                key == prefix or key.startswith(f"{prefix}/")
            ):
                labels.append(label)
        # Prefixes are only known after the exact-key maps above.  Keep them
        # in a private marker entry so physical orphan scanning can match a
        # whole legacy bundle without inventing directory objects.
        references.setdefault((StorageArea.STUDIO.value, f"{prefix}/"), []).append(label)

    return {key: tuple(sorted(labels)) for key, labels in references.items()}


def _legacy_labels(
    references: Mapping[tuple[str, str], tuple[str, ...]],
    area: StorageArea,
    key: str,
) -> tuple[str, ...]:
    labels = list(references.get((area.value, key), ()))
    for (candidate_area, prefix), prefix_labels in references.items():
        if candidate_area == area.value and prefix.endswith("/") and key.startswith(prefix):
            labels.extend(prefix_labels)
    return tuple(sorted(set(labels)))


class StorageReconcileService:
    """Observe metadata and physical objects and persist safe findings."""

    def __init__(
        self,
        db: Session,
        storage: StorageService,
        *,
        quarantine_retention: timedelta = timedelta(days=1),
        stale_staging_after: timedelta = timedelta(hours=1),
        stale_failed_after: timedelta = timedelta(days=1),
        stale_deleting_after: timedelta = timedelta(hours=1),
    ) -> None:
        self.db = db
        self.storage = storage
        self.quarantine_retention = quarantine_retention
        self.stale_staging_after = stale_staging_after
        self.stale_failed_after = stale_failed_after
        self.stale_deleting_after = stale_deleting_after
        for name, value in (
            ("quarantine_retention", quarantine_retention),
            ("stale_staging_after", stale_staging_after),
            ("stale_failed_after", stale_failed_after),
            ("stale_deleting_after", stale_deleting_after),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")

    def scan(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = True,
        verify_sha256: bool = True,
    ) -> StorageReconcileReport:
        """Scan both sides; only non-dry runs write quarantine rows."""

        current_time = _utc_now(now)
        refs = business_references(self.db)
        legacy = legacy_references(self.db)
        findings: list[StorageFinding] = []
        known_keys: set[tuple[str, str]] = set()
        metadata_count = 0
        physical_count = 0
        quarantined_count = 0

        objects = self.db.scalars(select(StorageObject).order_by(StorageObject.id)).all()
        for obj in objects:
            metadata_count += 1
            if obj.backend != self.storage.backend_name:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.UNKNOWN_BACKEND.value,
                        backend=obj.backend,
                        area="",
                        object_key=obj.object_key,
                        object_id=obj.id,
                        details={"configured_backend": self.storage.backend_name},
                    )
                )
                continue

            area = _area_for_object(obj)
            if area is None:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.UNKNOWN_AREA.value,
                        backend=obj.backend,
                        area="",
                        object_key=obj.object_key,
                        object_id=obj.id,
                        details={"namespace": obj.namespace},
                    )
                )
                continue
            known_keys.add((area.value, obj.object_key))
            if not _safe_key_for_area(area, obj.object_key):
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.INVALID_OBJECT_KEY.value,
                        backend=obj.backend,
                        area=area.value,
                        object_key=obj.object_key,
                        object_id=obj.id,
                    )
                )
                continue

            references = tuple(
                sorted(
                    set(refs.get(obj.id, ()))
                    | set(_legacy_labels(legacy, area, obj.object_key))
                )
            )
            self._append_stale_state_findings(
                findings,
                obj,
                area,
                references,
                current_time,
            )

            try:
                physical = self.storage.head(area, obj.object_key)
            except StorageNotFound:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.MISSING_PHYSICAL.value,
                        backend=obj.backend,
                        area=area.value,
                        object_key=obj.object_key,
                        object_id=obj.id,
                        references=references,
                        details={"expected_size": obj.size_bytes},
                    )
                )
                continue
            except StorageError as exc:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.STORAGE_ERROR.value,
                        backend=obj.backend,
                        area=area.value,
                        object_key=obj.object_key,
                        object_id=obj.id,
                        references=references,
                        details={"error": type(exc).__name__},
                    )
                )
                continue

            if obj.status == "deleted":
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.DELETED_PHYSICAL_PRESENT.value,
                        backend=obj.backend,
                        area=area.value,
                        object_key=obj.object_key,
                        object_id=obj.id,
                        references=references,
                        details={"actual_size": physical.size},
                    )
                )
                continue
            if obj.size_bytes is not None and physical.size != obj.size_bytes:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.SIZE_MISMATCH.value,
                        backend=obj.backend,
                        area=area.value,
                        object_key=obj.object_key,
                        object_id=obj.id,
                        references=references,
                        details={
                            "expected_size": obj.size_bytes,
                            "actual_size": physical.size,
                        },
                    )
                )
            if verify_sha256 and obj.sha256:
                try:
                    actual_sha256 = self._sha256(area, obj.object_key)
                except StorageError as exc:
                    findings.append(
                        StorageFinding(
                            kind=StorageFindingKind.STORAGE_ERROR.value,
                            backend=obj.backend,
                            area=area.value,
                            object_key=obj.object_key,
                            object_id=obj.id,
                            references=references,
                            details={"error": type(exc).__name__},
                        )
                    )
                    continue
                if actual_sha256 != obj.sha256:
                    findings.append(
                        StorageFinding(
                            kind=StorageFindingKind.SHA256_MISMATCH.value,
                            backend=obj.backend,
                            area=area.value,
                            object_key=obj.object_key,
                            object_id=obj.id,
                            references=references,
                            details={
                                "expected_sha256": obj.sha256,
                                "actual_sha256": actual_sha256,
                            },
                        )
                    )
            if obj.status == "active" and not references:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.UNREFERENCED_ACTIVE.value,
                        backend=obj.backend,
                        area=area.value,
                        object_key=obj.object_key,
                        object_id=obj.id,
                        details={"reason": "no ORM or legacy business reference"},
                    )
                )

        for area, prefix in _AREA_PREFIXES:
            if area.value not in self.storage.configured_areas:
                continue
            try:
                physical_objects = self.storage.list_objects(area, prefix)
            except StorageError as exc:
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.STORAGE_ERROR.value,
                        backend=self.storage.backend_name,
                        area=area.value,
                        object_key=prefix,
                        details={"error": type(exc).__name__},
                    )
                )
                continue
            physical_count += len(physical_objects)
            for physical in physical_objects:
                identity = (area.value, physical.key)
                if identity in known_keys:
                    continue
                references = _legacy_labels(legacy, area, physical.key)
                if references:
                    continue
                findings.append(
                    StorageFinding(
                        kind=StorageFindingKind.UNTRACKED_PHYSICAL.value,
                        backend=self.storage.backend_name,
                        area=area.value,
                        object_key=physical.key,
                        details={"actual_size": physical.size},
                    )
                )

        finalized: list[StorageFinding] = []
        for finding in findings:
            if finding.kind in {
                StorageFindingKind.UNKNOWN_BACKEND.value,
                StorageFindingKind.UNKNOWN_AREA.value,
                StorageFindingKind.INVALID_OBJECT_KEY.value,
                StorageFindingKind.STORAGE_ERROR.value,
            }:
                finalized.append(finding)
                continue
            due = current_time + self.quarantine_retention
            if not dry_run:
                due = self._upsert_quarantine(finding, current_time)
                quarantined_count += 1
            finalized.append(
                StorageFinding(
                    **{
                        **finding.__dict__,
                        "quarantined": not dry_run,
                        "quarantine_until": due,
                    }
                )
            )

        if not dry_run:
            self.db.commit()
        return StorageReconcileReport(
            dry_run=dry_run,
            scanned_metadata_count=metadata_count,
            scanned_physical_count=physical_count,
            findings=tuple(finalized),
            quarantined_count=quarantined_count,
        )

    def _append_stale_state_findings(
        self,
        findings: list[StorageFinding],
        obj: StorageObject,
        area: StorageArea,
        references: tuple[str, ...],
        now: datetime,
    ) -> None:
        common = {
            "backend": obj.backend,
            "area": area.value,
            "object_key": obj.object_key,
            "object_id": obj.id,
            "references": references,
        }
        if obj.status == "staging" and _is_stale(
            obj.updated_at,
            now=now,
            threshold=self.stale_staging_after,
        ):
            findings.append(StorageFinding(kind=StorageFindingKind.STALE_STAGING.value, **common))
        elif obj.status == "failed" and _is_stale(
            obj.updated_at,
            now=now,
            threshold=self.stale_failed_after,
        ):
            findings.append(StorageFinding(kind=StorageFindingKind.STALE_FAILED.value, **common))
        elif obj.status == "deleting" and _is_stale(
            obj.updated_at,
            now=now,
            threshold=self.stale_deleting_after,
        ):
            findings.append(StorageFinding(kind=StorageFindingKind.STALE_DELETING.value, **common))

    def _upsert_quarantine(self, finding: StorageFinding, now: datetime) -> datetime:
        existing = self.db.scalar(
            select(StorageQuarantine).where(
                StorageQuarantine.backend == finding.backend,
                StorageQuarantine.area == finding.area,
                StorageQuarantine.object_key == finding.object_key,
                StorageQuarantine.kind == finding.kind,
            )
        )
        due = now + self.quarantine_retention
        if existing is None:
            self.db.add(
                StorageQuarantine(
                    backend=finding.backend,
                    area=finding.area,
                    object_key=finding.object_key,
                    object_id=finding.object_id,
                    kind=finding.kind,
                    status=StorageQuarantineStatus.QUARANTINED.value,
                    first_seen_at=now,
                    quarantine_until=due,
                    attempts=0,
                    details_json=dict(finding.details),
                )
            )
            return due

        if existing.status == StorageQuarantineStatus.RESOLVED.value:
            existing.status = StorageQuarantineStatus.QUARANTINED.value
            existing.first_seen_at = now
            existing.quarantine_until = due
            existing.attempts = 0
            existing.last_error = None
            existing.resolved_at = None
        elif _aware(existing.quarantine_until, now) < now:
            # A new scan keeps an already-due finding eligible.  It does not
            # reset attempts or extend retention after a failed collection.
            due = existing.quarantine_until
        existing.object_id = finding.object_id or existing.object_id
        existing.details_json = dict(finding.details)
        return due

    def _sha256(self, area: StorageArea, key: str) -> str:
        stream = self.storage.open_read(area, key)
        digest = hashlib.sha256()
        try:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        finally:
            stream.close()
        return digest.hexdigest()
