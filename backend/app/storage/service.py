"""Application-facing storage facade.

Business services select a logical storage area and key.  They never receive
the LocalFilesystemStorage root or an absolute path.
"""
from __future__ import annotations

from enum import StrEnum
from typing import BinaryIO, Mapping

from app.config import Settings

from .backend import (
    StorageBackend,
    StorageMetadata,
    StorageObjectLister,
    StorageReadStream,
)
from .local import LocalFilesystemStorage
from .s3 import S3CompatibleStorage


class StorageArea(StrEnum):
    COVERS = "covers"
    VIDEOS = "videos"
    STUDIO = "studio"
    DATASETS = "datasets"
    ARTIFACTS = "artifacts"
    MATERIALS = "materials"


class StorageService:
    def __init__(
        self,
        backends: Mapping[str | StorageArea, StorageBackend],
        *,
        backend_name: str = "local",
    ) -> None:
        if not isinstance(backend_name, str) or not backend_name.strip():
            raise ValueError("storage backend name must be a non-empty string")
        self.backend_name = backend_name.strip()
        self._backends = {
            area.value if isinstance(area, StorageArea) else area: backend
            for area, backend in backends.items()
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> "StorageService":
        if settings.storage_backend == "s3":
            backend = S3CompatibleStorage.from_settings(settings)
            return cls(
                {
                    StorageArea.COVERS: backend,
                    StorageArea.VIDEOS: backend,
                    StorageArea.STUDIO: backend,
                    StorageArea.DATASETS: backend,
                    StorageArea.ARTIFACTS: backend,
                    StorageArea.MATERIALS: backend,
                },
                backend_name="s3",
            )
        if settings.storage_backend != "local":
            raise ValueError("DAI_STORAGE_BACKEND must be 'local' or 's3'")
        return cls(
            {
                StorageArea.COVERS: LocalFilesystemStorage(
                    settings.cover_storage_path,
                    key_prefix="covers",
                ),
                StorageArea.VIDEOS: LocalFilesystemStorage(settings.video_storage_path),
                StorageArea.STUDIO: LocalFilesystemStorage(settings.studio_storage_path),
                StorageArea.DATASETS: LocalFilesystemStorage(
                    settings.lab_storage_path / "datasets", key_prefix="datasets",
                ),
                StorageArea.ARTIFACTS: LocalFilesystemStorage(
                    settings.lab_storage_path / "artifacts", key_prefix="artifacts",
                ),
                StorageArea.MATERIALS: LocalFilesystemStorage(settings.material_storage_path),
            },
            backend_name="local",
        )

    @property
    def configured_areas(self) -> frozenset[str]:
        """Only inspect areas backed by this explicitly configured adapter."""
        return frozenset(self._backends)

    def _backend(self, area: str | StorageArea) -> StorageBackend:
        name = area.value if isinstance(area, StorageArea) else area
        try:
            return self._backends[name]
        except KeyError as exc:
            raise ValueError(f"unknown storage area: {name}") from exc

    def put(
        self,
        area: str | StorageArea,
        key: str,
        source: BinaryIO,
        *,
        max_bytes: int | None = None,
    ) -> StorageMetadata:
        return self._backend(area).put(key, source, max_bytes=max_bytes)

    def read(self, area: str | StorageArea, key: str) -> bytes:
        return self._backend(area).read(key)

    def open_read(
        self,
        area: str | StorageArea,
        key: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> StorageReadStream:
        return self._backend(area).open_read(key, offset=offset, length=length)

    def exists(self, area: str | StorageArea, key: str) -> bool:
        return self._backend(area).exists(key)

    def delete(
        self,
        area: str | StorageArea,
        key: str,
    ) -> None:
        self._backend(area).delete(key)

    def copy(
        self,
        area: str | StorageArea,
        source_key: str,
        destination_key: str,
    ) -> StorageMetadata:
        return self._backend(area).copy(source_key, destination_key)

    def head(self, area: str | StorageArea, key: str) -> StorageMetadata:
        return self._backend(area).head(key)

    def list_objects(
        self,
        area: str | StorageArea,
        prefix: str,
    ) -> tuple[StorageMetadata, ...]:
        """Enumerate object keys for Bundle/Asset orchestration only."""
        backend = self._backend(area)
        if not isinstance(backend, StorageObjectLister):
            raise TypeError("storage backend does not support object listing")
        return backend.list_objects(prefix)
