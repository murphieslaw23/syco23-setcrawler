from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.audio_storage import (
    AUDIO_ORIGINALS_BUCKET,
    AUDIO_QUARANTINE_BUCKET,
    MinioAudioStorage,
)


KEY = "objects/cc/cccccccccccccccccccccccccccccccc"
CHECKSUM = "c" * 64


class CopyClient:
    def __init__(self) -> None:
        self.objects = {(AUDIO_QUARANTINE_BUCKET, KEY): b"private-audio"}
        self.copy_calls: list[tuple[str, str, Any]] = []

    def copy_object(
        self,
        bucket_name: str,
        object_name: str,
        source: Any,
    ) -> SimpleNamespace:
        self.copy_calls.append((bucket_name, object_name, source))
        self.objects[(bucket_name, object_name)] = self.objects[
            (source.bucket_name, source.object_name)
        ]
        return SimpleNamespace(etag="copied", version_id=None)

    def stat_object(self, bucket_name: str, object_name: str) -> SimpleNamespace:
        value = self.objects[(bucket_name, object_name)]
        return SimpleNamespace(
            size=len(value),
            etag="stat",
            version_id=None,
            content_type="audio/mpeg",
            last_modified=None,
            metadata={"sha256": CHECKSUM},
        )


def test_copy_to_preserves_the_exact_opaque_key_across_private_buckets() -> None:
    client = CopyClient()
    storage = MinioAudioStorage(
        client,
        max_object_bytes=1024,
        part_size_bytes=5 * 1024 * 1024,
    )

    promoted = storage.copy_to(
        AUDIO_QUARANTINE_BUCKET,
        KEY,
        AUDIO_ORIGINALS_BUCKET,
        KEY,
    )

    assert promoted.bucket == AUDIO_ORIGINALS_BUCKET
    assert promoted.key == KEY
    assert promoted.sha256 == CHECKSUM
    assert client.copy_calls[0][0:2] == (AUDIO_ORIGINALS_BUCKET, KEY)
    assert client.copy_calls[0][2].bucket_name == AUDIO_QUARANTINE_BUCKET
    assert client.copy_calls[0][2].object_name == KEY
