from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import ceil
from typing import Any, Iterable

from minio.datatypes import Part

from app.schemas.creator_upload import ALLOWED_CREATOR_AUDIO_TYPES
from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    AudioChecksumMismatch,
    AudioStorageBoundsError,
    AudioStorageError,
    MinioAudioStorage,
    StoredAudioObject,
)


MAX_MULTIPART_PARTS = 10_000


class MultipartUploadConflict(AudioStorageError):
    pass


@dataclass(frozen=True, slots=True)
class MultipartUploadHandle:
    bucket: str
    key: str
    upload_id: str
    expected_size_bytes: int
    content_type: str
    expected_sha256: str | None
    part_size_bytes: int

    @property
    def part_count(self) -> int:
        return ceil(self.expected_size_bytes / self.part_size_bytes)


@dataclass(frozen=True, slots=True)
class UploadedPart:
    part_number: int
    etag: str
    size_bytes: int
    checksum_sha256: str


class MinioCreatorUploadStorage:
    """Private resumable multipart transport for creator-upload quarantine.

    The pinned minio-py release exposes multipart primitives as internal
    methods. This adapter validates that exact surface once and keeps it out of
    routers, workers, and repository code.
    """

    def __init__(
        self,
        client: Any,
        *,
        max_object_bytes: int,
        part_size_bytes: int,
        hash_read_size_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_object_bytes < 1:
            raise ValueError("max_object_bytes must be positive")
        if part_size_bytes < 5 * 1024 * 1024:
            raise ValueError("part_size_bytes must be at least 5 MiB")
        if hash_read_size_bytes < 1:
            raise ValueError("hash_read_size_bytes must be positive")
        for method in (
            "_create_multipart_upload",
            "_upload_part",
            "_complete_multipart_upload",
            "_abort_multipart_upload",
            "get_object",
            "stat_object",
            "remove_object",
        ):
            if not callable(getattr(client, method, None)):
                raise AudioStorageError(
                    f"configured MinIO client is missing multipart method {method}"
                )
        self._client = client
        self._max_object_bytes = max_object_bytes
        self._part_size_bytes = part_size_bytes
        self._hash_read_size_bytes = hash_read_size_bytes

    def start(
        self,
        *,
        expected_size_bytes: int,
        content_type: str,
        expected_sha256: str | None,
    ) -> MultipartUploadHandle:
        self._validate_expected_size(expected_size_bytes)
        normalized_content_type = content_type.casefold().strip()
        if normalized_content_type not in ALLOWED_CREATOR_AUDIO_TYPES:
            raise AudioStorageBoundsError("creator upload content type is not allowed")
        if expected_sha256 is not None and (
            len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")

        part_count = ceil(expected_size_bytes / self._part_size_bytes)
        if part_count > MAX_MULTIPART_PARTS:
            raise AudioStorageBoundsError("creator upload requires too many parts")

        key = MinioAudioStorage.new_object_key()
        headers = {"Content-Type": normalized_content_type}
        if expected_sha256 is not None:
            headers["X-Amz-Meta-Expected-Sha256"] = expected_sha256
        upload_id = self._client._create_multipart_upload(
            AUDIO_QUARANTINE_BUCKET,
            key,
            headers,
        )
        if not isinstance(upload_id, str) or not upload_id:
            raise AudioStorageError("MinIO returned an invalid multipart upload id")
        return MultipartUploadHandle(
            bucket=AUDIO_QUARANTINE_BUCKET,
            key=key,
            upload_id=upload_id,
            expected_size_bytes=expected_size_bytes,
            content_type=normalized_content_type,
            expected_sha256=expected_sha256,
            part_size_bytes=self._part_size_bytes,
        )

    def upload_part(
        self,
        handle: MultipartUploadHandle,
        *,
        part_number: int,
        data: bytes,
    ) -> UploadedPart:
        self._validate_handle(handle)
        expected_length = self.expected_part_size(handle, part_number)
        if len(data) != expected_length:
            raise AudioStorageBoundsError(
                "multipart part length does not match the deterministic upload plan"
            )
        etag = self._client._upload_part(
            handle.bucket,
            handle.key,
            data,
            None,
            handle.upload_id,
            part_number,
        )
        if not isinstance(etag, str) or not etag:
            raise AudioStorageError("MinIO returned an invalid multipart part ETag")
        return UploadedPart(
            part_number=part_number,
            etag=etag,
            size_bytes=len(data),
            checksum_sha256=sha256(data).hexdigest(),
        )

    def complete(
        self,
        handle: MultipartUploadHandle,
        parts: Iterable[UploadedPart],
    ) -> StoredAudioObject:
        self._validate_handle(handle)
        ordered = sorted(parts, key=lambda part: part.part_number)
        expected_numbers = list(range(1, handle.part_count + 1))
        if [part.part_number for part in ordered] != expected_numbers:
            raise MultipartUploadConflict(
                "multipart completion requires every part exactly once"
            )
        for part in ordered:
            if part.size_bytes != self.expected_part_size(handle, part.part_number):
                raise MultipartUploadConflict(
                    "persisted multipart part size does not match the upload plan"
                )
            if (
                len(part.checksum_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in part.checksum_sha256
                )
            ):
                raise MultipartUploadConflict("multipart part checksum is invalid")
            if not part.etag:
                raise MultipartUploadConflict("multipart part ETag is missing")

        result = self._client._complete_multipart_upload(
            handle.bucket,
            handle.key,
            handle.upload_id,
            [Part(part.part_number, part.etag) for part in ordered],
        )
        stat = self._client.stat_object(handle.bucket, handle.key)
        actual_size = int(stat.size)
        if actual_size != handle.expected_size_bytes:
            self._delete_completed_object(handle)
            raise AudioStorageBoundsError(
                "completed multipart object size does not match the declaration"
            )
        actual_content_type = (
            getattr(stat, "content_type", None) or ""
        ).casefold().strip()
        if actual_content_type != handle.content_type:
            self._delete_completed_object(handle)
            raise AudioStorageBoundsError(
                "completed multipart content type does not match the declaration"
            )
        try:
            actual_sha256 = self._hash_completed_object(handle)
        except Exception:
            self._delete_completed_object(handle)
            raise
        if (
            handle.expected_sha256 is not None
            and actual_sha256 != handle.expected_sha256
        ):
            self._delete_completed_object(handle)
            raise AudioChecksumMismatch("completed multipart checksum does not match")

        metadata = dict(getattr(stat, "metadata", {}) or {})
        metadata["sha256"] = actual_sha256
        return StoredAudioObject(
            bucket=handle.bucket,
            key=handle.key,
            size=actual_size,
            sha256=actual_sha256,
            etag=getattr(result, "etag", None) or getattr(stat, "etag", None),
            version_id=(
                getattr(result, "version_id", None)
                or getattr(stat, "version_id", None)
            ),
            content_type=actual_content_type,
            last_modified=getattr(stat, "last_modified", None),
            metadata=metadata,
        )

    def abort(self, handle: MultipartUploadHandle) -> None:
        self._validate_handle(handle)
        try:
            self._client._abort_multipart_upload(
                handle.bucket,
                handle.key,
                handle.upload_id,
            )
        except Exception as error:
            if getattr(error, "code", None) != "NoSuchUpload":
                raise

    @staticmethod
    def expected_part_size(
        handle: MultipartUploadHandle,
        part_number: int,
    ) -> int:
        if part_number < 1 or part_number > handle.part_count:
            raise AudioStorageBoundsError("multipart part number is outside the plan")
        consumed = (part_number - 1) * handle.part_size_bytes
        return min(
            handle.part_size_bytes,
            handle.expected_size_bytes - consumed,
        )

    def _hash_completed_object(self, handle: MultipartUploadHandle) -> str:
        response = self._client.get_object(handle.bucket, handle.key)
        digest = sha256()
        bytes_read = 0
        try:
            while bytes_read < handle.expected_size_bytes:
                remaining = handle.expected_size_bytes - bytes_read
                chunk = response.read(min(self._hash_read_size_bytes, remaining))
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("MinIO object reads must return bytes")
                value = bytes(chunk)
                bytes_read += len(value)
                if bytes_read > handle.expected_size_bytes:
                    raise AudioStorageBoundsError(
                        "completed multipart object exceeds the declaration"
                    )
                digest.update(value)
            overflow = response.read(1)
            if bytes_read != handle.expected_size_bytes or overflow:
                raise AudioStorageBoundsError(
                    "completed multipart object length is inconsistent"
                )
            return digest.hexdigest()
        finally:
            try:
                response.close()
            finally:
                response.release_conn()

    def _delete_completed_object(self, handle: MultipartUploadHandle) -> None:
        self._client.remove_object(handle.bucket, handle.key)

    def _validate_expected_size(self, expected_size_bytes: int) -> None:
        if expected_size_bytes < 1 or expected_size_bytes > self._max_object_bytes:
            raise AudioStorageBoundsError(
                "creator upload size is outside configured bounds"
            )

    def _validate_handle(self, handle: MultipartUploadHandle) -> None:
        if handle.bucket != AUDIO_QUARANTINE_BUCKET:
            raise AudioStorageError("creator upload handle is not in quarantine")
        MinioAudioStorage._validate_key(handle.key)
        self._validate_expected_size(handle.expected_size_bytes)
        if handle.part_size_bytes != self._part_size_bytes:
            raise MultipartUploadConflict(
                "multipart upload part size differs from server configuration"
            )
        if not handle.upload_id:
            raise MultipartUploadConflict("multipart upload id is missing")
