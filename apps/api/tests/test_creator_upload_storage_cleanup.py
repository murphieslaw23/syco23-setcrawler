from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.audio_storage import AUDIO_QUARANTINE_BUCKET
from app.services.creator_upload_storage import MinioCreatorUploadStorage


PART_SIZE = 5 * 1024 * 1024
PAYLOAD = b"syco23"


class _StatFailureClient:
    def __init__(self) -> None:
        self.bucket: str | None = None
        self.key: str | None = None
        self.upload_id = "upload-stat-failure"
        self.parts: dict[int, bytes] = {}
        self.removed: list[tuple[str, str]] = []

    def _create_multipart_upload(
        self,
        bucket: str,
        key: str,
        headers: dict[str, str],
    ) -> str:
        self.bucket = bucket
        self.key = key
        assert headers["Content-Type"] == "audio/mpeg"
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
    ) -> Any:
        assert bucket == self.bucket
        assert key == self.key
        assert upload_id == self.upload_id
        assert [part.part_number for part in parts] == [1]
        return SimpleNamespace(etag="complete-etag", version_id=None)

    def _abort_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
    ) -> None:
        raise AssertionError("abort is not part of this verification path")

    def stat_object(self, bucket: str, key: str) -> Any:
        assert bucket == self.bucket
        assert key == self.key
        raise RuntimeError("stat unavailable")

    def get_object(self, bucket: str, key: str) -> Any:
        raise AssertionError("hashing must not start after stat failure")

    def remove_object(self, bucket: str, key: str) -> None:
        self.removed.append((bucket, key))


def test_stat_failure_after_completion_deletes_quarantine_object() -> None:
    client = _StatFailureClient()
    storage = MinioCreatorUploadStorage(
        client,
        max_object_bytes=PART_SIZE,
        part_size_bytes=PART_SIZE,
    )
    handle = storage.start(
        expected_size_bytes=len(PAYLOAD),
        content_type="audio/mpeg",
        expected_sha256=None,
    )
    part = storage.upload_part(handle, part_number=1, data=PAYLOAD)

    with pytest.raises(RuntimeError, match="stat unavailable"):
        storage.complete(handle, [part])

    assert client.removed == [(AUDIO_QUARANTINE_BUCKET, handle.key)]
