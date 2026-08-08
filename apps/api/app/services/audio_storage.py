from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BufferedIOBase
import re
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from app.core.config import Settings


AUDIO_QUARANTINE_BUCKET = "audio-quarantine"
AUDIO_ORIGINALS_BUCKET = "audio-originals"
AUDIO_DERIVATIVES_BUCKET = "audio-derivatives"
AUDIO_BUCKETS = (
    AUDIO_QUARANTINE_BUCKET,
    AUDIO_ORIGINALS_BUCKET,
    AUDIO_DERIVATIVES_BUCKET,
)

_OBJECT_KEY_PATTERN = re.compile(r"^objects/[0-9a-f]{2}/[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024


class AudioStorageError(RuntimeError):
    """Base error for bounded private audio storage operations."""


class AudioStorageBoundsError(AudioStorageError):
    pass


class AudioChecksumMismatch(AudioStorageError):
    pass


class InvalidAudioObjectKey(AudioStorageError):
    pass


class InvalidAudioBucket(AudioStorageError):
    pass


@dataclass(frozen=True, slots=True)
class StoredAudioObject:
    bucket: str
    key: str
    size: int
    sha256: str | None
    etag: str | None
    version_id: str | None
    content_type: str | None
    last_modified: Any = None
    metadata: dict[str, str] | None = None


class _HashingReader(BufferedIOBase):
    def __init__(self, source: BinaryIO, expected_length: int) -> None:
        self._source = source
        self._expected_length = expected_length
        self._bytes_read = 0
        self._hash = sha256()

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    @property
    def hexdigest(self) -> str:
        return self._hash.hexdigest()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        remaining = self._expected_length - self._bytes_read
        if remaining <= 0:
            return b""
        bounded_size = remaining if size is None or size < 0 else min(size, remaining)
        chunk = self._source.read(bounded_size)
        if chunk is None:
            return b""
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("audio storage streams must return bytes")
        value = bytes(chunk)
        if len(value) > remaining:
            value = value[:remaining]
        self._bytes_read += len(value)
        self._hash.update(value)
        return value


class MinioAudioStorage:
    def __init__(
        self,
        client: Any,
        *,
        max_object_bytes: int,
        part_size_bytes: int,
    ) -> None:
        if max_object_bytes < 1:
            raise ValueError("max_object_bytes must be positive")
        if part_size_bytes < _MIN_MULTIPART_PART_SIZE:
            raise ValueError("part_size_bytes must be at least 5 MiB")
        self._client = client
        self._max_object_bytes = max_object_bytes
        self._part_size_bytes = part_size_bytes

    @staticmethod
    def new_object_key() -> str:
        token = uuid4().hex
        return f"objects/{token[:2]}/{token}"

    @staticmethod
    def _validate_bucket(bucket: str) -> None:
        if bucket not in AUDIO_BUCKETS:
            raise InvalidAudioBucket("audio storage bucket is not allowed")

    @staticmethod
    def _validate_key(key: str) -> None:
        if not _OBJECT_KEY_PATTERN.fullmatch(key):
            raise InvalidAudioObjectKey("audio object key is invalid")

    def _validate_length(self, length: int) -> None:
        if length < 1 or length > self._max_object_bytes:
            raise AudioStorageBoundsError(
                "audio object length is outside configured bounds"
            )

    def put_stream(
        self,
        bucket: str,
        stream: BinaryIO,
        *,
        length: int,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> StoredAudioObject:
        self._validate_bucket(bucket)
        self._validate_length(length)
        if not content_type or len(content_type) > 255:
            raise AudioStorageBoundsError("audio content type is invalid")
        if expected_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            expected_sha256
        ):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")

        key = self.new_object_key()
        reader = _HashingReader(stream, length)
        metadata = {"sha256": expected_sha256} if expected_sha256 else None
        result = self._client.put_object(
            bucket,
            key,
            reader,
            length,
            content_type=content_type,
            part_size=self._part_size_bytes,
            metadata=metadata,
        )

        actual_sha256 = reader.hexdigest
        incomplete = reader.bytes_read != length
        overflow = stream.read(1)
        if incomplete or overflow:
            self._client.remove_object(bucket, key)
            raise AudioStorageBoundsError(
                "audio stream length does not match declared length"
            )
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            self._client.remove_object(bucket, key)
            raise AudioChecksumMismatch("audio object checksum does not match")

        return StoredAudioObject(
            bucket=bucket,
            key=key,
            size=length,
            sha256=actual_sha256,
            etag=getattr(result, "etag", None),
            version_id=getattr(result, "version_id", None),
            content_type=content_type,
            metadata={"sha256": actual_sha256},
        )

    def stat(self, bucket: str, key: str) -> StoredAudioObject:
        self._validate_bucket(bucket)
        self._validate_key(key)
        result = self._client.stat_object(bucket, key)
        metadata = dict(getattr(result, "metadata", {}) or {})
        digest = metadata.get("sha256") or metadata.get("x-amz-meta-sha256")
        return StoredAudioObject(
            bucket=bucket,
            key=key,
            size=int(result.size),
            sha256=digest,
            etag=getattr(result, "etag", None),
            version_id=getattr(result, "version_id", None),
            content_type=getattr(result, "content_type", None),
            last_modified=getattr(result, "last_modified", None),
            metadata=metadata,
        )

    @contextmanager
    def open_range(
        self,
        bucket: str,
        key: str,
        *,
        start: int,
        length: int,
    ) -> Iterator[Any]:
        self._validate_bucket(bucket)
        self._validate_key(key)
        if start < 0:
            raise AudioStorageBoundsError("range start must be non-negative")
        self._validate_length(length)
        response = self._client.get_object(
            bucket,
            key,
            offset=start,
            length=length,
        )
        try:
            yield response
        finally:
            try:
                response.close()
            finally:
                response.release_conn()

    def copy(
        self,
        source_bucket: str,
        source_key: str,
        destination_bucket: str,
    ) -> StoredAudioObject:
        return self.copy_to(
            source_bucket,
            source_key,
            destination_bucket,
            self.new_object_key(),
        )

    def copy_to(
        self,
        source_bucket: str,
        source_key: str,
        destination_bucket: str,
        destination_key: str,
    ) -> StoredAudioObject:
        self._validate_bucket(source_bucket)
        self._validate_key(source_key)
        self._validate_bucket(destination_bucket)
        self._validate_key(destination_key)
        self._client.copy_object(
            destination_bucket,
            destination_key,
            CopySource(source_bucket, source_key),
        )
        return self.stat(destination_bucket, destination_key)

    def delete(self, bucket: str, key: str) -> None:
        self._validate_bucket(bucket)
        self._validate_key(key)
        self._client.remove_object(bucket, key)

    def ensure_buckets(self) -> None:
        for bucket in AUDIO_BUCKETS:
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)
            try:
                self._client.delete_bucket_policy(bucket)
            except S3Error as error:
                if error.code not in {"NoSuchBucketPolicy", "NoSuchPolicy"}:
                    raise


def build_audio_storage(
    settings: Settings,
    *,
    client: Any | None = None,
) -> MinioAudioStorage:
    if not settings.audio_storage_enabled:
        raise AudioStorageError("private audio storage is disabled")
    selected_client = client or Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(),
        secure=settings.minio_secure,
    )
    return MinioAudioStorage(
        selected_client,
        max_object_bytes=settings.audio_max_object_bytes,
        part_size_bytes=settings.audio_multipart_part_size_bytes,
    )
