from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version as package_version
import re
from types import SimpleNamespace
from typing import Any

from minio.datatypes import Part
from minio.error import S3Error

from app.schemas.creator_upload import ALLOWED_CREATOR_AUDIO_TYPES
from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    AudioChecksumMismatch,
    AudioStorageBoundsError,
    AudioStorageError,
    InvalidAudioObjectKey,
    MinioAudioStorage,
    StoredAudioObject,
)


PINNED_MINIO_SDK_VERSION = "7.2.20"
_MIN_PART_SIZE_BYTES = 5 * 1024 * 1024
_MAX_PARTS = 10_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_INTERNAL_METHODS = (
    "_create_multipart_upload",
    "_upload_part",
    "_list_parts",
    "_complete_multipart_upload",
    "_abort_multipart_upload",
)


class MultipartTransportCompatibilityError(AudioStorageError):
    """Raised when the pinned MinIO multipart adapter cannot fail closed."""


class MultipartUploadConflict(AudioStorageError):
    """Raised when a caller attempts a stale or non-sequential part write."""


@dataclass(frozen=True, slots=True)
class MultipartUploadSession:
    bucket: str
    key: str
    upload_id: str
    expected_size_bytes: int
    content_type: str
    expected_sha256: str | None
    part_size_bytes: int


@dataclass(frozen=True, slots=True)
class MultipartPartRecord:
    part_number: int
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class MultipartResumeState:
    parts: tuple[MultipartPartRecord, ...]
    received_size_bytes: int
    next_part_number: int
    complete: bool


class MinioMultipartAudioTransport:
    """
    Server-only resumable multipart transport for creator uploads.

    MinIO Python 7.2.20 does not expose the multipart lifecycle as public
    methods. This adapter isolates the five underscore-prefixed S3 methods,
    pins their exact SDK version, and refuses to initialize if the contract
    changes. No URL, credential, or object identity is intended for clients.
    """

    def __init__(
        self,
        client: Any,
        *,
        max_object_bytes: int,
        part_size_bytes: int,
        sdk_version: str | None = None,
    ) -> None:
        selected_version = sdk_version or package_version("minio")
        if selected_version != PINNED_MINIO_SDK_VERSION:
            raise MultipartTransportCompatibilityError(
                "private multipart adapter requires minio==7.2.20"
            )
        missing = [
            name
            for name in _REQUIRED_INTERNAL_METHODS
            if not callable(getattr(client, name, None))
        ]
        if missing:
            raise MultipartTransportCompatibilityError(
                "MinIO multipart internals are unavailable: " + ", ".join(missing)
            )
        if max_object_bytes < 1:
            raise ValueError("max_object_bytes must be positive")
        if part_size_bytes < _MIN_PART_SIZE_BYTES:
            raise ValueError("part_size_bytes must be at least 5 MiB")
        if part_size_bytes > max_object_bytes:
            raise ValueError("part_size_bytes cannot exceed max_object_bytes")
        if (max_object_bytes + part_size_bytes - 1) // part_size_bytes > _MAX_PARTS:
            raise ValueError("configured object size would exceed 10000 parts")

        self._client = client
        self._max_object_bytes = max_object_bytes
        self._part_size_bytes = part_size_bytes

    @staticmethod
    def _validate_key(key: str) -> None:
        try:
            MinioAudioStorage._validate_key(key)  # noqa: SLF001 - shared fence.
        except InvalidAudioObjectKey:
            raise

    def _validate_session(self, session: MultipartUploadSession) -> None:
        if session.bucket != AUDIO_QUARANTINE_BUCKET:
            raise AudioStorageBoundsError(
                "creator multipart uploads must remain in audio-quarantine"
            )
        self._validate_key(session.key)
        if not session.upload_id or len(session.upload_id) > 2048:
            raise AudioStorageBoundsError("multipart upload ID is invalid")
        if session.expected_size_bytes < 1 or session.expected_size_bytes > self._max_object_bytes:
            raise AudioStorageBoundsError(
                "multipart object length is outside configured bounds"
            )
        if session.part_size_bytes != self._part_size_bytes:
            raise MultipartUploadConflict("multipart part-size contract changed")
        if session.content_type not in ALLOWED_CREATOR_AUDIO_TYPES:
            raise AudioStorageBoundsError("multipart audio content type is invalid")
        if session.expected_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            session.expected_sha256
        ):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")

    def begin(
        self,
        *,
        expected_size_bytes: int,
        content_type: str,
        expected_sha256: str | None = None,
        object_key: str | None = None,
    ) -> MultipartUploadSession:
        if expected_size_bytes < 1 or expected_size_bytes > self._max_object_bytes:
            raise AudioStorageBoundsError(
                "multipart object length is outside configured bounds"
            )
        normalized_content_type = content_type.casefold().strip()
        if normalized_content_type not in ALLOWED_CREATOR_AUDIO_TYPES:
            raise AudioStorageBoundsError("multipart audio content type is invalid")
        if expected_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            expected_sha256
        ):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")

        key = object_key or MinioAudioStorage.new_object_key()
        self._validate_key(key)
        headers: dict[str, str] = {
            "Content-Type": normalized_content_type,
            "X-Amz-Meta-Declared-Size": str(expected_size_bytes),
        }
        if expected_sha256 is not None:
            headers["X-Amz-Meta-Sha256"] = expected_sha256
        upload_id = self._client._create_multipart_upload(  # noqa: SLF001
            AUDIO_QUARANTINE_BUCKET,
            key,
            headers,
        )
        if not isinstance(upload_id, str) or not upload_id or len(upload_id) > 2048:
            raise MultipartTransportCompatibilityError(
                "MinIO returned an invalid multipart upload ID"
            )
        return MultipartUploadSession(
            bucket=AUDIO_QUARANTINE_BUCKET,
            key=key,
            upload_id=upload_id,
            expected_size_bytes=expected_size_bytes,
            content_type=normalized_content_type,
            expected_sha256=expected_sha256,
            part_size_bytes=self._part_size_bytes,
        )

    def list_parts(self, session: MultipartUploadSession) -> MultipartResumeState:
        self._validate_session(session)
        parts: list[MultipartPartRecord] = []
        marker: str | None = None

        while True:
            result = self._client._list_parts(  # noqa: SLF001
                session.bucket,
                session.key,
                session.upload_id,
                max_parts=1000,
                part_number_marker=marker,
            )
            raw_parts = list(getattr(result, "parts", ()) or ())
            for raw in raw_parts:
                part_number = int(raw.part_number)
                size = int(raw.size or 0)
                etag = str(raw.etag)
                if part_number != len(parts) + 1:
                    raise MultipartUploadConflict(
                        "multipart parts are missing or non-sequential"
                    )
                if size < 1 or size > self._part_size_bytes:
                    raise AudioStorageBoundsError("stored multipart part size is invalid")
                parts.append(
                    MultipartPartRecord(
                        part_number=part_number,
                        etag=etag,
                        size=size,
                    )
                )
                if len(parts) > _MAX_PARTS:
                    raise AudioStorageBoundsError("multipart upload has too many parts")

            if not bool(getattr(result, "is_truncated", False)):
                break
            next_marker = getattr(result, "next_part_number_marker", None)
            if next_marker is None or str(next_marker) == marker:
                raise MultipartTransportCompatibilityError(
                    "MinIO multipart pagination did not advance"
                )
            marker = str(next_marker)

        received = sum(part.size for part in parts)
        if received > session.expected_size_bytes:
            raise AudioStorageBoundsError(
                "multipart parts exceed the declared object size"
            )
        return MultipartResumeState(
            parts=tuple(parts),
            received_size_bytes=received,
            next_part_number=len(parts) + 1,
            complete=received == session.expected_size_bytes,
        )

    def upload_part(
        self,
        session: MultipartUploadSession,
        *,
        part_number: int,
        data: bytes,
    ) -> MultipartPartRecord:
        self._validate_session(session)
        if not isinstance(data, bytes):
            raise TypeError("multipart part data must be bytes")
        state = self.list_parts(session)
        if state.complete:
            raise MultipartUploadConflict("multipart upload is already complete")
        if part_number != state.next_part_number:
            raise MultipartUploadConflict(
                f"expected multipart part {state.next_part_number}"
            )

        remaining = session.expected_size_bytes - state.received_size_bytes
        expected_part_size = min(self._part_size_bytes, remaining)
        if len(data) != expected_part_size:
            raise AudioStorageBoundsError(
                "multipart part length does not match the bounded resume contract"
            )
        etag = self._client._upload_part(  # noqa: SLF001
            session.bucket,
            session.key,
            data,
            None,
            session.upload_id,
            part_number,
        )
        if not isinstance(etag, str) or not etag:
            raise MultipartTransportCompatibilityError(
                "MinIO returned an invalid multipart ETag"
            )
        return MultipartPartRecord(
            part_number=part_number,
            etag=etag,
            size=len(data),
        )

    def _hash_completed_object(
        self,
        session: MultipartUploadSession,
    ) -> tuple[int, str]:
        response = self._client.get_object(session.bucket, session.key)
        digest = sha256()
        total = 0
        try:
            while True:
                chunk = response.read(min(1024 * 1024, session.expected_size_bytes + 1 - total))
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("MinIO object reads must return bytes")
                value = bytes(chunk)
                total += len(value)
                if total > session.expected_size_bytes:
                    raise AudioStorageBoundsError(
                        "completed multipart object exceeds its declaration"
                    )
                digest.update(value)
        finally:
            try:
                response.close()
            finally:
                response.release_conn()
        return total, digest.hexdigest()

    def complete(self, session: MultipartUploadSession) -> StoredAudioObject:
        self._validate_session(session)
        state = self.list_parts(session)
        if not state.complete:
            raise MultipartUploadConflict("multipart upload is incomplete")

        result = self._client._complete_multipart_upload(  # noqa: SLF001
            session.bucket,
            session.key,
            session.upload_id,
            [Part(part.part_number, part.etag) for part in state.parts],
        )
        try:
            size, actual_sha256 = self._hash_completed_object(session)
            if size != session.expected_size_bytes:
                raise AudioStorageBoundsError(
                    "completed multipart object length does not match"
                )
            if (
                session.expected_sha256 is not None
                and actual_sha256 != session.expected_sha256
            ):
                raise AudioChecksumMismatch(
                    "completed multipart checksum does not match"
                )
            stat = self._client.stat_object(session.bucket, session.key)
        except Exception:
            self._client.remove_object(session.bucket, session.key)
            raise

        metadata = dict(getattr(stat, "metadata", {}) or {})
        metadata["sha256"] = actual_sha256
        return StoredAudioObject(
            bucket=session.bucket,
            key=session.key,
            size=size,
            sha256=actual_sha256,
            etag=getattr(result, "etag", None) or getattr(stat, "etag", None),
            version_id=getattr(result, "version_id", None)
            or getattr(stat, "version_id", None),
            content_type=getattr(stat, "content_type", None)
            or session.content_type,
            last_modified=getattr(stat, "last_modified", None),
            metadata=metadata,
        )

    def abort(self, session: MultipartUploadSession) -> None:
        self._validate_session(session)
        try:
            self._client._abort_multipart_upload(  # noqa: SLF001
                session.bucket,
                session.key,
                session.upload_id,
            )
        except S3Error as error:
            if error.code != "NoSuchUpload":
                raise


def multipart_contract_snapshot() -> Any:
    """Small introspection target for tests and release diagnostics."""
    return SimpleNamespace(
        sdk_version=PINNED_MINIO_SDK_VERSION,
        required_methods=_REQUIRED_INTERNAL_METHODS,
        min_part_size_bytes=_MIN_PART_SIZE_BYTES,
        max_parts=_MAX_PARTS,
    )
