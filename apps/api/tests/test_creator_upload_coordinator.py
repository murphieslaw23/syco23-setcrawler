from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from app.repositories.creator_upload_multipart import CreatorUploadMultipartConflict
from app.schemas.creator_upload import CreatorUploadSession, CreatorUploadStatus
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)
from app.services.creator_upload_coordinator import (
    CreatorUploadCompensationError,
    CreatorUploadCoordinator,
)
from app.services.creator_upload_storage import (
    MultipartUploadHandle,
    UploadedPart,
)


NOW = datetime(2026, 8, 2, 14, 30, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-000000009901")
JOB_ID = UUID("00000000-0000-4000-8000-000000009902")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009903")
OBJECT_KEY = "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
UPLOAD_ID = "private-upload-23"
PART_SIZE = 5 * 1024 * 1024
PAYLOAD = b"creator-part"
PAYLOAD_SHA = sha256(PAYLOAD).hexdigest()


def _session(
    *,
    status: CreatorUploadStatus = CreatorUploadStatus.initiated,
    version: int = 0,
    received_size_bytes: int = 0,
    with_storage: bool = False,
) -> CreatorUploadSession:
    return CreatorUploadSession(
        id=SESSION_ID,
        audio_input_job_id=JOB_ID,
        rights_review_id=REVIEW_ID,
        expected_size_bytes=len(PAYLOAD),
        received_size_bytes=received_size_bytes,
        content_type="audio/mpeg",
        expected_sha256=PAYLOAD_SHA,
        staging_object_key=OBJECT_KEY if with_storage else None,
        storage_upload_id=UPLOAD_ID if with_storage else None,
        status=status,
        expires_at=NOW + timedelta(hours=24),
        created_by="creator-23",
        version=version,
        created_at=NOW,
        updated_at=NOW,
    )


def _handle() -> MultipartUploadHandle:
    return MultipartUploadHandle(
        bucket="audio-quarantine",
        key=OBJECT_KEY,
        upload_id=UPLOAD_ID,
        expected_size_bytes=len(PAYLOAD),
        content_type="audio/mpeg",
        expected_sha256=PAYLOAD_SHA,
        part_size_bytes=PART_SIZE,
    )


def _manifest() -> CreatorUploadManifest:
    return CreatorUploadManifest(
        session_id=SESSION_ID,
        part_size_bytes=PART_SIZE,
        expected_part_count=1,
        created_at=NOW,
    )


def _part() -> UploadedPart:
    return UploadedPart(
        part_number=1,
        etag="etag-23",
        size_bytes=len(PAYLOAD),
        checksum_sha256=PAYLOAD_SHA,
    )


class _CreatorRepository:
    def __init__(self, session: CreatorUploadSession, events: list[str]) -> None:
        self.session = session
        self.events = events
        self.abort_error: Exception | None = None

    def get_creator_upload(self, session_id: UUID) -> CreatorUploadSession | None:
        return self.session if session_id == self.session.id else None

    def abort_creator_upload(
        self,
        session_id: UUID,
        *,
        reason: str,
    ) -> CreatorUploadSession:
        self.events.append("persist-abort")
        if self.abort_error is not None:
            raise self.abort_error
        assert session_id == self.session.id
        assert reason
        self.session = CreatorUploadSession.model_validate(
            {
                **self.session.model_dump(),
                "status": CreatorUploadStatus.aborted,
                "version": self.session.version + 1,
                "updated_at": NOW + timedelta(minutes=1),
            }
        )
        return self.session


class _LedgerRepository:
    def __init__(
        self,
        creator: _CreatorRepository,
        events: list[str],
        *,
        manifest: CreatorUploadManifest | None = None,
        existing_part: CreatorUploadPartRecord | None = None,
    ) -> None:
        self.creator = creator
        self.events = events
        self.manifest = manifest
        self.existing_part = existing_part
        self.attach_error: Exception | None = None
        self.record_error: Exception | None = None

    def get_manifest(self, session_id: UUID) -> CreatorUploadManifest | None:
        assert session_id == SESSION_ID
        return self.manifest

    def get_part(
        self,
        session_id: UUID,
        part_number: int,
    ) -> CreatorUploadPartRecord | None:
        assert session_id == SESSION_ID
        assert part_number == 1
        return self.existing_part

    def attach_manifest(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        handle: MultipartUploadHandle,
    ) -> tuple[CreatorUploadSession, CreatorUploadManifest]:
        self.events.append("attach-manifest")
        if self.attach_error is not None:
            raise self.attach_error
        assert expected_version == 0
        self.manifest = _manifest()
        self.creator.session = _session(
            status=CreatorUploadStatus.uploading,
            version=1,
            with_storage=True,
        )
        return self.creator.session, self.manifest

    def record_part(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        part: UploadedPart,
    ) -> tuple[CreatorUploadSession, CreatorUploadPartRecord]:
        self.events.append("record-part")
        if self.record_error is not None:
            raise self.record_error
        assert session_id == SESSION_ID
        assert expected_version == 1
        record = CreatorUploadPartRecord(
            session_id=SESSION_ID,
            part_number=part.part_number,
            etag=part.etag,
            size_bytes=part.size_bytes,
            checksum_sha256=part.checksum_sha256,
            created_at=NOW,
        )
        self.existing_part = record
        self.creator.session = _session(
            status=CreatorUploadStatus.awaiting_attestation,
            version=2,
            received_size_bytes=len(PAYLOAD),
            with_storage=True,
        )
        return self.creator.session, record


class _Storage:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.start_calls = 0
        self.upload_calls = 0
        self.abort_calls = 0
        self.abort_error: Exception | None = None

    def start(self, **kwargs: Any) -> MultipartUploadHandle:
        self.events.append("remote-start")
        self.start_calls += 1
        assert kwargs == {
            "expected_size_bytes": len(PAYLOAD),
            "content_type": "audio/mpeg",
            "expected_sha256": PAYLOAD_SHA,
        }
        return _handle()

    def upload_part(
        self,
        handle: MultipartUploadHandle,
        *,
        part_number: int,
        data: bytes,
    ) -> UploadedPart:
        self.events.append("remote-part")
        self.upload_calls += 1
        assert handle == _handle()
        assert part_number == 1
        assert data == PAYLOAD
        return _part()

    def abort(self, handle: MultipartUploadHandle) -> None:
        self.events.append("remote-abort")
        self.abort_calls += 1
        assert handle == _handle()
        if self.abort_error is not None:
            raise self.abort_error


def _coordinator(
    *,
    session: CreatorUploadSession | None = None,
    manifest: CreatorUploadManifest | None = None,
    existing_part: CreatorUploadPartRecord | None = None,
) -> tuple[
    CreatorUploadCoordinator,
    _CreatorRepository,
    _LedgerRepository,
    _Storage,
    list[str],
]:
    events: list[str] = []
    creator = _CreatorRepository(session or _session(), events)
    ledger = _LedgerRepository(
        creator,
        events,
        manifest=manifest,
        existing_part=existing_part,
    )
    storage = _Storage(events)
    coordinator = CreatorUploadCoordinator(creator, ledger, storage)
    return coordinator, creator, ledger, storage, events


def test_start_creates_manifest_and_returns_safe_progress() -> None:
    coordinator, creator, ledger, storage, events = _coordinator()

    progress = coordinator.start(SESSION_ID, expected_version=0)

    assert events == ["remote-start", "attach-manifest"]
    assert storage.start_calls == 1
    assert ledger.manifest is not None
    assert creator.session.status is CreatorUploadStatus.uploading
    assert progress.session_id == SESSION_ID
    assert progress.version == 1
    assert progress.expected_part_count == 1
    assert progress.part_size_bytes == PART_SIZE
    assert progress.received_size_bytes == 0
    serialized = progress.model_dump()
    assert "staging_object_key" not in serialized
    assert "storage_upload_id" not in serialized
    assert "object_key" not in serialized
    assert "upload_id" not in serialized
    assert "url" not in serialized


def test_start_replays_existing_manifest_without_remote_start() -> None:
    coordinator, _creator, _ledger, storage, events = _coordinator(
        session=_session(
            status=CreatorUploadStatus.uploading,
            version=1,
            with_storage=True,
        ),
        manifest=_manifest(),
    )

    progress = coordinator.start(SESSION_ID, expected_version=0)

    assert events == []
    assert storage.start_calls == 0
    assert progress.version == 1
    assert progress.status is CreatorUploadStatus.uploading


def test_start_aborts_new_remote_upload_when_manifest_persistence_fails() -> None:
    coordinator, _creator, ledger, storage, events = _coordinator()
    ledger.attach_error = CreatorUploadMultipartConflict("version conflict")

    with pytest.raises(CreatorUploadMultipartConflict, match="version conflict"):
        coordinator.start(SESSION_ID, expected_version=0)

    assert events == ["remote-start", "attach-manifest", "remote-abort"]
    assert storage.abort_calls == 1


def test_start_reports_both_primary_and_abort_failure() -> None:
    coordinator, _creator, ledger, storage, _events = _coordinator()
    ledger.attach_error = RuntimeError("database unavailable")
    storage.abort_error = RuntimeError("minio unavailable")

    with pytest.raises(CreatorUploadCompensationError) as error:
        coordinator.start(SESSION_ID, expected_version=0)

    assert isinstance(error.value.primary_error, RuntimeError)
    assert isinstance(error.value.compensation_error, RuntimeError)


def test_exact_part_replay_uses_ledger_without_remote_rewrite() -> None:
    record = CreatorUploadPartRecord(
        session_id=SESSION_ID,
        part_number=1,
        etag="etag-23",
        size_bytes=len(PAYLOAD),
        checksum_sha256=PAYLOAD_SHA,
        created_at=NOW,
    )
    coordinator, _creator, _ledger, storage, events = _coordinator(
        session=_session(
            status=CreatorUploadStatus.awaiting_attestation,
            version=2,
            received_size_bytes=len(PAYLOAD),
            with_storage=True,
        ),
        manifest=_manifest(),
        existing_part=record,
    )

    progress = coordinator.upload_part(
        SESSION_ID,
        expected_version=1,
        part_number=1,
        data=PAYLOAD,
    )

    assert events == []
    assert storage.upload_calls == 0
    assert progress.version == 2
    assert progress.status is CreatorUploadStatus.awaiting_attestation


def test_conflicting_part_replay_is_rejected_before_remote_write() -> None:
    record = CreatorUploadPartRecord(
        session_id=SESSION_ID,
        part_number=1,
        etag="etag-other",
        size_bytes=len(PAYLOAD),
        checksum_sha256="0" * 64,
        created_at=NOW,
    )
    coordinator, _creator, _ledger, storage, _events = _coordinator(
        session=_session(
            status=CreatorUploadStatus.uploading,
            version=1,
            with_storage=True,
        ),
        manifest=_manifest(),
        existing_part=record,
    )

    with pytest.raises(CreatorUploadMultipartConflict, match="conflicts"):
        coordinator.upload_part(
            SESSION_ID,
            expected_version=1,
            part_number=1,
            data=PAYLOAD,
        )

    assert storage.upload_calls == 0


def test_part_persistence_failure_aborts_durable_state_before_remote_upload() -> None:
    coordinator, creator, ledger, storage, events = _coordinator(
        session=_session(
            status=CreatorUploadStatus.uploading,
            version=1,
            with_storage=True,
        ),
        manifest=_manifest(),
    )
    ledger.record_error = RuntimeError("ledger unavailable")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        coordinator.upload_part(
            SESSION_ID,
            expected_version=1,
            part_number=1,
            data=PAYLOAD,
        )

    assert events == [
        "remote-part",
        "record-part",
        "persist-abort",
        "remote-abort",
    ]
    assert creator.session.status is CreatorUploadStatus.aborted
    assert storage.abort_calls == 1


def test_part_failure_reports_incomplete_compensation() -> None:
    coordinator, creator, ledger, storage, _events = _coordinator(
        session=_session(
            status=CreatorUploadStatus.uploading,
            version=1,
            with_storage=True,
        ),
        manifest=_manifest(),
    )
    ledger.record_error = RuntimeError("ledger unavailable")
    creator.abort_error = RuntimeError("abort persistence unavailable")
    storage.abort_error = RuntimeError("remote abort unavailable")

    with pytest.raises(CreatorUploadCompensationError) as error:
        coordinator.upload_part(
            SESSION_ID,
            expected_version=1,
            part_number=1,
            data=PAYLOAD,
        )

    assert isinstance(error.value.primary_error, RuntimeError)
    assert len(error.value.compensation_errors) == 2


def test_successful_part_write_updates_ledger_and_progress() -> None:
    coordinator, creator, ledger, storage, events = _coordinator(
        session=_session(
            status=CreatorUploadStatus.uploading,
            version=1,
            with_storage=True,
        ),
        manifest=_manifest(),
    )

    progress = coordinator.upload_part(
        SESSION_ID,
        expected_version=1,
        part_number=1,
        data=PAYLOAD,
    )

    assert events == ["remote-part", "record-part"]
    assert storage.upload_calls == 1
    assert ledger.existing_part is not None
    assert creator.session.status is CreatorUploadStatus.awaiting_attestation
    assert progress.status is CreatorUploadStatus.awaiting_attestation
    assert progress.received_size_bytes == len(PAYLOAD)
    assert progress.version == 2
