"""S3-compatible implementation of the single-object storage contract.

The backend stores logical keys exactly as the application gives them.  An
optional private ``key_prefix`` is only a physical namespace inside the
configured bucket; it is never exposed to business services or persisted as a
different object key.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from typing import Any, BinaryIO

from .backend import (
    InvalidStorageKey,
    StorageBackend,
    StorageConflict,
    StorageError,
    StorageLimitExceeded,
    StorageMetadata,
    StorageNotFound,
    StorageRangeError,
    StorageReadStream,
)


_CHUNK_SIZE = 1024 * 1024
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchObject", "NotFound"}
_CONFLICT_CODES = {"409", "412", "PreconditionFailed"}
_RANGE_CODES = {"416", "InvalidRange"}


class _S3ReadStream:
    """Hide provider-specific stream methods behind the forward-only contract."""

    def __init__(self, body: Any) -> None:
        self._body = body
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        return self._body.read(size)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._body.close()


class _EmptyReadStream:
    """A closeable empty stream used for a valid read at EOF."""

    def read(self, size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        return None


class S3CompatibleStorage(StorageBackend):
    """Store logical objects in any S3-compatible object store.

    A boto3-compatible client is injected so the backend is straightforward
    to test with moto and can be configured for AWS S3, MinIO, OSS, COS, or
    another compatible provider without putting provider names in the
    database.  The client owns endpoint, credentials, and addressing details.
    """

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        key_prefix: str = "",
    ) -> None:
        if not bucket or not bucket.strip():
            raise ValueError("S3 bucket must be configured")
        self._client = client
        self._bucket = bucket
        self._key_prefix = self._validate_prefix(key_prefix)

    @classmethod
    def from_settings(cls, settings: Any) -> "S3CompatibleStorage":
        """Build a client from generic S3 settings without naming a provider."""
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise StorageError("S3 backend requires the boto3 dependency") from exc

        client_kwargs: dict[str, Any] = {
            "region_name": settings.storage_s3_region,
        }
        if settings.storage_s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.storage_s3_endpoint_url

        access_key = _secret_value(settings.storage_s3_access_key_id)
        secret_key = _secret_value(settings.storage_s3_secret_access_key)
        session_token = _secret_value(settings.storage_s3_session_token)
        if access_key:
            client_kwargs["aws_access_key_id"] = access_key
        if secret_key:
            client_kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            client_kwargs["aws_session_token"] = session_token

        if settings.storage_s3_addressing_style != "auto":
            try:
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - boto3 imports it normally
                raise StorageError("S3 backend requires botocore") from exc
            client_kwargs["config"] = Config(
                s3={"addressing_style": settings.storage_s3_addressing_style}
            )

        client = boto3.client("s3", **client_kwargs)
        return cls(
            client,
            bucket=settings.storage_s3_bucket,
            key_prefix=settings.storage_s3_key_prefix,
        )

    @staticmethod
    def _validate_prefix(prefix: str) -> str:
        if not prefix:
            return ""
        if not isinstance(prefix, str) or "\x00" in prefix:
            raise InvalidStorageKey("S3 key prefix must be a safe POSIX path")
        if "\\" in prefix or prefix.startswith("/") or "//" in prefix:
            raise InvalidStorageKey("S3 key prefix must use safe POSIX segments")
        parts = tuple(prefix.strip("/").split("/"))
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise InvalidStorageKey("S3 key prefix contains an invalid path segment")
        return "/".join(parts)

    @staticmethod
    def _validate_key(key: str) -> str:
        if not isinstance(key, str) or not key or "\x00" in key:
            raise InvalidStorageKey("storage key must be a non-empty string")
        if "\\" in key or key.startswith("/") or "//" in key:
            raise InvalidStorageKey("storage key must use safe POSIX segments")

        parts = tuple(key.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise InvalidStorageKey("storage key contains an invalid path segment")
        if ":" in parts[0]:
            raise InvalidStorageKey("storage key cannot contain a drive prefix")
        return key

    def _physical_key(self, key: str) -> str:
        logical = self._validate_key(key)
        if self._key_prefix:
            return f"{self._key_prefix}/{logical}"
        return logical

    def _logical_key(self, physical_key: str) -> str | None:
        if not self._key_prefix:
            return physical_key
        prefix = f"{self._key_prefix}/"
        if not physical_key.startswith(prefix):
            return None
        return physical_key[len(prefix) :]

    @staticmethod
    def _error_code(exc: Exception) -> str | None:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return None
        error = response.get("Error")
        if not isinstance(error, dict):
            return None
        code = error.get("Code")
        return str(code) if code is not None else None

    @classmethod
    def _storage_exception(cls, exc: Exception, key: str) -> StorageError:
        code = cls._error_code(exc)
        if code in _NOT_FOUND_CODES:
            return StorageNotFound(key)
        if code in _CONFLICT_CODES:
            return StorageConflict(key)
        if code in _RANGE_CODES:
            return StorageRangeError("requested byte range is not satisfiable")
        return StorageError("S3 storage operation failed")

    def _metadata_from_response(
        self,
        key: str,
        response: dict[str, Any],
    ) -> StorageMetadata:
        return StorageMetadata(
            key=key,
            size=int(response.get("ContentLength", response.get("Size", 0))),
            etag=response.get("ETag"),
            last_modified=_as_datetime(response.get("LastModified")),
        )

    def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        max_bytes: int | None = None,
    ) -> StorageMetadata:
        physical_key = self._physical_key(key)
        total = 0
        # The staging stream makes max-size validation independent of caller
        # seekability and prevents an over-limit input from being uploaded.
        with tempfile.SpooledTemporaryFile(max_size=8 * _CHUNK_SIZE, mode="w+b") as staged:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise StorageLimitExceeded(max_bytes, total)
                staged.write(chunk)
            staged.seek(0)
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=physical_key,
                    Body=staged,
                    ContentLength=total,
                )
            except Exception as exc:
                raise self._storage_exception(exc, key) from exc
        return self.head(key)

    def read(self, key: str) -> bytes:
        physical_key = self._physical_key(key)
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=physical_key,
            )
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()
        except Exception as exc:
            raise self._storage_exception(exc, key) from exc

    def open_read(
        self,
        key: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> StorageReadStream:
        physical_key = self._physical_key(key)
        if offset < 0 or (length is not None and length < 0):
            raise StorageRangeError("offset and length must be non-negative")

        size = self.head(key).size
        if offset > size:
            raise StorageRangeError("offset is beyond the object size")
        if offset == size or length == 0:
            return _EmptyReadStream()

        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": physical_key,
        }
        if offset or length is not None:
            end = size - 1 if length is None else min(size - 1, offset + length - 1)
            request["Range"] = f"bytes={offset}-{end}"

        try:
            response = self._client.get_object(**request)
            return _S3ReadStream(response["Body"])
        except Exception as exc:
            raise self._storage_exception(exc, key) from exc

    def exists(self, key: str) -> bool:
        try:
            self.head(key)
        except StorageNotFound:
            return False
        return True

    def delete(self, key: str) -> None:
        physical_key = self._physical_key(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=physical_key)
        except Exception as exc:
            error = self._storage_exception(exc, key)
            if isinstance(error, StorageNotFound):
                return
            raise error from exc

    def copy(self, source_key: str, destination_key: str) -> StorageMetadata:
        source_physical_key = self._physical_key(source_key)
        destination_physical_key = self._physical_key(destination_key)

        # S3 has no directory and no portable atomic "copy if destination is
        # absent" primitive.  The explicit precondition preserves the same
        # sequential contract as LocalFilesystemStorage; a future distributed
        # coordination layer can strengthen the race behavior if required.
        self.head(source_key)
        if self.exists(destination_key):
            raise StorageConflict(destination_key)

        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=destination_physical_key,
                CopySource={"Bucket": self._bucket, "Key": source_physical_key},
            )
        except Exception as exc:
            raise self._storage_exception(exc, source_key) from exc
        return self.head(destination_key)

    def head(self, key: str) -> StorageMetadata:
        physical_key = self._physical_key(key)
        try:
            response = self._client.head_object(
                Bucket=self._bucket,
                Key=physical_key,
            )
        except Exception as exc:
            raise self._storage_exception(exc, key) from exc
        return self._metadata_from_response(key, response)

    def list_objects(self, prefix: str) -> tuple[StorageMetadata, ...]:
        """List concrete objects below a logical prefix, never directories."""
        physical_prefix = self._physical_key(prefix)
        objects: list[StorageMetadata] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._bucket, Prefix=physical_prefix)
            for page in pages:
                for item in page.get("Contents", ()):  # S3 omits Contents when empty
                    physical_key = item.get("Key")
                    if not isinstance(physical_key, str):
                        continue
                    logical_key = self._logical_key(physical_key)
                    if logical_key is None:
                        continue
                    if logical_key != prefix and not logical_key.startswith(f"{prefix}/"):
                        continue
                    objects.append(self._metadata_from_response(logical_key, item))
        except Exception as exc:
            raise self._storage_exception(exc, prefix) from exc
        return tuple(sorted(objects, key=lambda item: item.key))


def _secret_value(value: Any) -> str:
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return str(value or "").strip()


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    # botocore returns dateutil tzutc(), while RFC 7231's usegmt formatter
    # requires datetime.timezone.utc specifically. Normalize at the boundary.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
