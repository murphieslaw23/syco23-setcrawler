from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    AudioChecksumMismatch,
    AudioStorageBoundsError,
    AudioStorageError,
)
from app.services.creator_upload_storage import (
    MinioCreatorUploadStorage,
    MultipartUploadConflict,
    MultipartUploadHandle,
)


PART_SIZE = 5 * 1024 * 1024
FIRST_PART = b"a" * PART_SIZE
FINAL_PART = b"xyz"
PAYLOAD = FIRST_PART + FINAL_PART
CHECKSUM = sha256(PAYLOAD).hexdigest()


class _ObjectResponse(BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.released = False

    def release_conn(self) -> None:
        self.released = True


@dataclass
class _MultipartResult:
    etag: str = "complete-etag"
    version_id: str | None = None


class _NoSuchUpload(RuntimeError):
    code = "NoSuchUpload"


class _FakeMinioClient:
    def __init__(self) -> None:
        self.upload_id = "upload-23"
        self.bucket: str | None = None
        self.key: str | None = None
        self.headers: dict[str, str] | None = None
        self.parts: dict[int, bytes] = {}
        self.completed: bytes | None = None
        self.read_payload: bytes | None = None
        self.stat_content_type: str | None = None
        self.removed: list[tuple[str, str]] = []
        self.abort_count = 0
        self.missing_on_abort = False
        self.last_response: _ObjectResponse | None = None

    def _create_multipart_upload(
        self,
        bucket: str,
        key: str,
        headers: dict[str, str],
    ) -> str:
        self.bucket = bucket
        self.key = key
        self.headers = headers
        return self.upload_id

    def _upload_part(
        self,
        bucket: str,
        key: str,
        data: bytes,
        headers: dict[str, str] | None,
        upload_id: str,
        part_number: int,
    ) -> str:
        assert bucket == self.bucket
        assert key == self.key
        assert headers is None
        assert upload_id == self.upload_id
        self.parts[part_number] = data
        return f"etag-{part_number}"

    def _complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[Any],
    ) -> _MultipartResult:
        assert bucket == self.bucket
        assert key == self.key
        assert upload_id == self.upload_id
        self.completed = b"".join(
            self.parts[item.part_number]
            for item in parts
        )
        return _MultipartResult()

    def _abort_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
    ) -> None:
        assert bucket == self.bucket
        assert key == self.key
        assert upload_id == self.upload_id
        self.abort_count += 1
        if self.missing_on_abort:
            raise _NoSuchUpload()

    def stat_object(self, bucket: str, key: str) -> Any:
        assert bucket == self.bucket
        assert key == self.key
        assert self.completed is not None
        return SimpleNamespace(
            size=len(self.completed),
            etag="stat-etag",
            version_id=None,
            content_type=(
                self.stat_content_type
                or (self.headers or {}).get("Content-Type")
            ),
            metadata=dict(self.headers or {}),
            last_modified=None,
        )

    def get_object(self, bucket: str, key: str) -> _ObjectResponse:
        assert bucket == self.bucket
        assert key == self.key
        assert self.completed is not None
        value = self.completed if self.read_payload is None else self.read_payload
        self.last_response = _ObjectResponse(value)
        return self.last_response

    def remove_object(self, bucket: str, key: str) -> None:
        self.removed.append((bucket, key))


def _storage(client: Any | None = None) -> MinioCreatorUploadStorage:
    return MinioCreatorUploadStorage(
        client or _FakeMinioClient(),
        max_object_bytes=10 * PART_SIZE,
        part_size_bytes=PART_SIZE,
        hash_read_size_bytes=1024 * 1024,
    )


def _started(
    client: _FakeMinioClient,
    *,
    expected_sha256: str | None = CHECKSUM,
) -> tuple[MinioCreatorUploadStorage, MultipartUploadHandle]:
    storage = _storage(client)
    handle = storage.start(
        expected_size_bytes=len(PAYLOAD),
        content_type="Audio/MPEG",
        expected_sha256=expected_sha256,
    )
    return storage, handle


def _uploaded_parts(
    storage: MinioCreatorUploadStorage,
    handle: MultipartUploadHandle,
) -> list[Any]:
    return [
        storage.upload_part(handle, part_number=1, data=FIRST_PART),
        storage.upload_part(handle, part_number=2, data=FINAL_PART),
    ]


def test_constructor_rejects_incompatible_minio_clients() -> None:
    with pytest.raises(AudioStorageError, match="_create_multipart_upload"):
        _storage(object())


def test_start_creates_private_opaque_quarantine_upload() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)

    assert handle.bucket == AUDIO_QUARANTINE_BUCKET
    assert handle.upload_id == "upload-23"
    assert handle.key.startswith("objects/")
    assert handle.part_count == 2
    assert client.headers == {
        "Content-Type": "audio/mpeg",
        "X-Amz-Meta-Expected-Sha256": CHECKSUM,
    }

    with pytest.raises(AudioStorageBoundsError, match="content type"):
        storage.start(
            expected_size_bytes=23,
            content_type="application/octet-stream",
            expected_sha256=None,
        )
    with pytest.raises(AudioStorageBoundsError, match="configured bounds"):
        storage.start(
            expected_size_bytes=10 * PART_SIZE + 1,
            content_type="audio/mpeg",
            expected_sha256=None,
        )


def test_part_upload_follows_deterministic_plan() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)

    first = storage.upload_part(handle, part_number=1, data=FIRST_PART)
    final = storage.upload_part(handle, part_number=2, data=FINAL_PART)

    assert first.size_bytes == PART_SIZE
    assert first.checksum_sha256 == sha256(FIRST_PART).hexdigest()
    assert final.size_bytes == len(FINAL_PART)
    assert client.parts == {1: FIRST_PART, 2: FINAL_PART}

    with pytest.raises(AudioStorageBoundsError, match="length"):
        storage.upload_part(handle, part_number=1, data=b"short")
    with pytest.raises(AudioStorageBoundsError, match="part number"):
        storage.upload_part(handle, part_number=3, data=b"invalid")


def test_completion_requires_every_part_and_verifies_full_object_checksum() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)
    first, final = _uploaded_parts(storage, handle)

    with pytest.raises(MultipartUploadConflict, match="every part"):
        storage.complete(handle, [first])

    stored = storage.complete(handle, [final, first])

    assert stored.bucket == AUDIO_QUARANTINE_BUCKET
    assert stored.key == handle.key
    assert stored.size == len(PAYLOAD)
    assert stored.sha256 == CHECKSUM
    assert stored.content_type == "audio/mpeg"
    assert stored.etag == "complete-etag"
    assert client.last_response is not None
    assert client.last_response.closed
    assert client.last_response.released
    assert client.removed == []


def test_checksum_mismatch_deletes_completed_object() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client, expected_sha256="f" * 64)

    with pytest.raises(AudioChecksumMismatch, match="checksum"):
        storage.complete(handle, _uploaded_parts(storage, handle))

    assert client.removed == [(AUDIO_QUARANTINE_BUCKET, handle.key)]


def test_content_type_mismatch_deletes_completed_object() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)
    client.stat_content_type = "application/octet-stream"

    with pytest.raises(AudioStorageBoundsError, match="content type"):
        storage.complete(handle, _uploaded_parts(storage, handle))

    assert client.removed == [(AUDIO_QUARANTINE_BUCKET, handle.key)]


def test_verification_read_failure_deletes_completed_object() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)
    client.read_payload = PAYLOAD[:-1]

    with pytest.raises(AudioStorageBoundsError, match="length is inconsistent"):
        storage.complete(handle, _uploaded_parts(storage, handle))

    assert client.removed == [(AUDIO_QUARANTINE_BUCKET, handle.key)]
    assert client.last_response is not None
    assert client.last_response.closed
    assert client.last_response.released


def test_abort_is_idempotent_when_upload_is_already_missing() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)

    storage.abort(handle)
    client.missing_on_abort = True
    storage.abort(handle)

    assert client.abort_count == 2


def test_handle_cannot_cross_bucket_or_part_size_configuration() -> None:
    client = _FakeMinioClient()
    storage, handle = _started(client)

    wrong_bucket = MultipartUploadHandle(
        bucket="audio-originals",
        key=handle.key,
        upload_id=handle.upload_id,
        expected_size_bytes=handle.expected_size_bytes,
        content_type=handle.content_type,
        expected_sha256=handle.expected_sha256,
        part_size_bytes=handle.part_size_bytes,
    )
    with pytest.raises(AudioStorageError, match="quarantine"):
        storage.abort(wrong_bucket)

    wrong_part_size = MultipartUploadHandle(
        bucket=handle.bucket,
        key=handle.key,
        upload_id=handle.upload_id,
        expected_size_bytes=handle.expected_size_bytes,
        content_type=handle.content_type,
        expected_sha256=handle.expected_sha256,
        part_size_bytes=handle.part_size_bytes * 2,
    )
    with pytest.raises(MultipartUploadConflict, match="part size"):
        storage.abort(wrong_part_size)
