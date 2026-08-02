from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5, sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.audio_multipart import (
    PINNED_MINIO_SDK_VERSION,
    MinioMultipartAudioTransport,
    MultipartTransportCompatibilityError,
    MultipartUploadConflict,
)
from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    AudioChecksumMismatch,
    AudioStorageBoundsError,
)


PART_SIZE = 5 * 1024 * 1024
TAIL = b"SYCO23-MULTIPART-TAIL"
FIRST_PART = b"a" * PART_SIZE
FULL_DATA = FIRST_PART + TAIL
FULL_SHA256 = sha256(FULL_DATA).hexdigest()
OBJECT_KEY = "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@dataclass(frozen=True)
class _StoredPart:
    part_number: int
    etag: str
    size: int


class _Response(BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.released = False

    def release_conn(self) -> None:
        self.released = True


class _FakeMinio:
    def __init__(self) -> None:
        self.headers: dict[str, str] | None = None
        self.upload_id = "upload-syco23-23"
        self.parts: dict[int, bytes] = {}
        self.objects: dict[str, bytes] = {}
        self.removed: list[tuple[str, str]] = []
        self.aborted = False
        self.complete_calls = 0

    def _create_multipart_upload(
        self,
        bucket: str,
        key: str,
        headers: dict[str, str],
    ) -> str:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert key == OBJECT_KEY
        self.headers = dict(headers)
        return self.upload_id

    def _list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        *,
        max_parts: int,
        part_number_marker: str | None,
    ) -> Any:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert key == OBJECT_KEY
        assert upload_id == self.upload_id
        assert max_parts == 1000
        marker = int(part_number_marker or 0)
        selected = [
            _StoredPart(
                part_number=number,
                etag=md5(data, usedforsecurity=False).hexdigest(),
                size=len(data),
            )
            for number, data in sorted(self.parts.items())
            if number > marker
        ]
        return SimpleNamespace(
            parts=selected,
            is_truncated=False,
            next_part_number_marker=None,
        )

    def _upload_part(
        self,
        bucket: str,
        key: str,
        data: bytes,
        headers: dict[str, str] | None,
        upload_id: str,
        part_number: int,
    ) -> str:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert key == OBJECT_KEY
        assert upload_id == self.upload_id
        assert headers is None
        self.parts[part_number] = data
        return md5(data, usedforsecurity=False).hexdigest()

    def _complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[Any],
    ) -> Any:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert upload_id == self.upload_id
        assert [part.part_number for part in parts] == sorted(self.parts)
        self.complete_calls += 1
        value = b"".join(self.parts[index] for index in sorted(self.parts))
        self.objects[key] = value
        return SimpleNamespace(etag="multipart-etag", version_id="version-23")

    def _abort_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
    ) -> None:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert key == OBJECT_KEY
        assert upload_id == self.upload_id
        self.aborted = True
        self.parts.clear()

    def get_object(self, bucket: str, key: str) -> _Response:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        return _Response(self.objects[key])

    def stat_object(self, bucket: str, key: str) -> Any:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        value = self.objects[key]
        return SimpleNamespace(
            size=len(value),
            etag="stat-etag",
            version_id="stat-version",
            content_type="audio/mpeg",
            last_modified=None,
            metadata={"x-amz-meta-declared-size": str(len(value))},
        )

    def remove_object(self, bucket: str, key: str) -> None:
        self.removed.append((bucket, key))
        self.objects.pop(key, None)


def _transport(client: Any | None = None) -> MinioMultipartAudioTransport:
    return MinioMultipartAudioTransport(
        client or _FakeMinio(),
        max_object_bytes=PART_SIZE * 2,
        part_size_bytes=PART_SIZE,
        sdk_version=PINNED_MINIO_SDK_VERSION,
    )


def _session(
    transport: MinioMultipartAudioTransport,
    *,
    expected_sha256: str | None = FULL_SHA256,
) -> Any:
    return transport.begin(
        expected_size_bytes=len(FULL_DATA),
        content_type=" Audio/MPEG ",
        expected_sha256=expected_sha256,
        object_key=OBJECT_KEY,
    )


def test_adapter_fails_closed_on_sdk_or_internal_contract_change() -> None:
    with pytest.raises(
        MultipartTransportCompatibilityError,
        match="minio==7.2.20",
    ):
        MinioMultipartAudioTransport(
            _FakeMinio(),
            max_object_bytes=PART_SIZE * 2,
            part_size_bytes=PART_SIZE,
            sdk_version="7.2.21",
        )

    incomplete_client = SimpleNamespace(
        _create_multipart_upload=lambda *_args, **_kwargs: "upload"
    )
    with pytest.raises(
        MultipartTransportCompatibilityError,
        match="internals are unavailable",
    ):
        MinioMultipartAudioTransport(
            incomplete_client,
            max_object_bytes=PART_SIZE * 2,
            part_size_bytes=PART_SIZE,
            sdk_version=PINNED_MINIO_SDK_VERSION,
        )


def test_begin_uses_private_quarantine_and_opaque_identity() -> None:
    client = _FakeMinio()
    transport = _transport(client)
    session = _session(transport)

    assert session.bucket == AUDIO_QUARANTINE_BUCKET
    assert session.key == OBJECT_KEY
    assert session.upload_id == client.upload_id
    assert session.content_type == "audio/mpeg"
    assert session.expected_size_bytes == len(FULL_DATA)
    assert client.headers == {
        "Content-Type": "audio/mpeg",
        "X-Amz-Meta-Declared-Size": str(len(FULL_DATA)),
        "X-Amz-Meta-Sha256": FULL_SHA256,
    }
    state = transport.list_parts(session)
    assert state.received_size_bytes == 0
    assert state.next_part_number == 1
    assert not state.complete


def test_upload_parts_are_exact_sequential_and_resumable() -> None:
    client = _FakeMinio()
    transport = _transport(client)
    session = _session(transport)

    with pytest.raises(AudioStorageBoundsError, match="part length"):
        transport.upload_part(session, part_number=1, data=b"short")
    with pytest.raises(MultipartUploadConflict, match="expected multipart part 1"):
        transport.upload_part(session, part_number=2, data=FIRST_PART)

    first = transport.upload_part(
        session,
        part_number=1,
        data=FIRST_PART,
    )
    assert first.size == PART_SIZE
    resumed = transport.list_parts(session)
    assert resumed.received_size_bytes == PART_SIZE
    assert resumed.next_part_number == 2
    assert not resumed.complete

    with pytest.raises(AudioStorageBoundsError, match="part length"):
        transport.upload_part(session, part_number=2, data=TAIL + b"overflow")
    final = transport.upload_part(session, part_number=2, data=TAIL)
    assert final.size == len(TAIL)
    complete_state = transport.list_parts(session)
    assert complete_state.complete
    assert complete_state.received_size_bytes == len(FULL_DATA)

    with pytest.raises(MultipartUploadConflict, match="already complete"):
        transport.upload_part(session, part_number=3, data=b"x")


def test_complete_hashes_final_object_and_returns_private_record() -> None:
    client = _FakeMinio()
    transport = _transport(client)
    session = _session(transport)
    transport.upload_part(session, part_number=1, data=FIRST_PART)
    transport.upload_part(session, part_number=2, data=TAIL)

    stored = transport.complete(session)

    assert client.complete_calls == 1
    assert stored.bucket == AUDIO_QUARANTINE_BUCKET
    assert stored.key == OBJECT_KEY
    assert stored.size == len(FULL_DATA)
    assert stored.sha256 == FULL_SHA256
    assert stored.etag == "multipart-etag"
    assert stored.version_id == "version-23"
    assert stored.content_type == "audio/mpeg"
    assert stored.metadata is not None
    assert stored.metadata["sha256"] == FULL_SHA256
    assert client.removed == []


def test_complete_rejects_incomplete_or_bad_checksum_and_deletes_object() -> None:
    client = _FakeMinio()
    transport = _transport(client)
    session = _session(transport, expected_sha256="0" * 64)

    with pytest.raises(MultipartUploadConflict, match="incomplete"):
        transport.complete(session)
    assert client.complete_calls == 0

    transport.upload_part(session, part_number=1, data=FIRST_PART)
    transport.upload_part(session, part_number=2, data=TAIL)
    with pytest.raises(AudioChecksumMismatch, match="checksum"):
        transport.complete(session)

    assert client.removed == [(AUDIO_QUARANTINE_BUCKET, OBJECT_KEY)]
    assert OBJECT_KEY not in client.objects


def test_abort_discards_parts_without_creating_an_object() -> None:
    client = _FakeMinio()
    transport = _transport(client)
    session = _session(transport)
    transport.upload_part(session, part_number=1, data=FIRST_PART)

    transport.abort(session)

    assert client.aborted
    assert client.parts == {}
    assert client.objects == {}


def test_source_contract_has_no_client_upload_or_public_url_surface() -> None:
    root = Path(__file__).parents[3]
    source = (root / "apps/api/app/services/audio_multipart.py").read_text()
    requirements = (root / "apps/api/requirements.txt").read_text()
    lowered = source.casefold()

    assert "minio==7.2.20" in requirements
    assert "presigned" not in lowered
    assert "public_url" not in lowered
    assert "presigned_put_object" not in lowered
    assert "_create_multipart_upload" in source
    assert "_upload_part" in source
    assert "_list_parts" in source
    assert "_complete_multipart_upload" in source
    assert "_abort_multipart_upload" in source
