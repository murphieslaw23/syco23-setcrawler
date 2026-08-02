from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories.creator_upload_multipart import CreatorUploadMultipartConflict
from app.schemas.creator_upload import (
    CreatorUploadSession,
    CreatorUploadStart,
    CreatorUploadStatus,
)
from app.schemas.creator_upload_multipart import CreatorUploadManifest
from app.services.creator_upload_coordinator import CreatorUploadCoordinator


NOW = datetime(2026, 8, 2, 15, 30, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-000000009971")
JOB_ID = UUID("00000000-0000-4000-8000-000000009972")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009973")
OBJECT_KEY = "objects/cc/cccccccccccccccccccccccccccccccc"
UPLOAD_ID = "terminal-fixture"


def _session(
    status: CreatorUploadStatus,
    *,
    version: int,
    received_size_bytes: int = 0,
) -> CreatorUploadSession:
    active = status in {
        CreatorUploadStatus.uploading,
        CreatorUploadStatus.awaiting_attestation,
        CreatorUploadStatus.completed,
    }
    complete = status in {
        CreatorUploadStatus.awaiting_attestation,
        CreatorUploadStatus.completed,
    }
    return CreatorUploadSession(
        id=SESSION_ID,
        audio_input_job_id=JOB_ID,
        rights_review_id=REVIEW_ID,
        expected_size_bytes=23,
        received_size_bytes=23 if complete else received_size_bytes,
        content_type="audio/mpeg",
        staging_object_key=OBJECT_KEY if active or status is CreatorUploadStatus.aborted else None,
        storage_upload_id=UPLOAD_ID if active or status is CreatorUploadStatus.aborted else None,
        status=status,
        attestation_evidence_id=(
            UUID("00000000-0000-4000-8000-000000009974")
            if status is CreatorUploadStatus.completed
            else None
        ),
        attested_by="creator-23" if status is CreatorUploadStatus.completed else None,
        attested_at=(NOW + timedelta(minutes=1)) if status is CreatorUploadStatus.completed else None,
        expires_at=NOW + timedelta(hours=24),
        created_by="creator-23",
        version=version,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "status",
    [
        CreatorUploadStatus.aborted,
        CreatorUploadStatus.expired,
        CreatorUploadStatus.completed,
    ],
)
def test_terminal_sessions_cannot_resume(status: CreatorUploadStatus) -> None:
    session = _session(status, version=2)
    storage = SimpleNamespace(
        start=lambda **_kwargs: pytest.fail("terminal session reached storage")
    )
    coordinator = CreatorUploadCoordinator(
        SimpleNamespace(get_creator_upload=lambda _session_id: session),
        SimpleNamespace(get_manifest=lambda _session_id: None),
        storage,
    )

    with pytest.raises(CreatorUploadMultipartConflict, match="terminal"):
        coordinator.start(SESSION_ID, expected_version=2)


def test_awaiting_attestation_cannot_write_a_missing_part() -> None:
    session = _session(
        CreatorUploadStatus.awaiting_attestation,
        version=2,
    )
    manifest = CreatorUploadManifest(
        session_id=SESSION_ID,
        part_size_bytes=5 * 1024 * 1024,
        expected_part_count=1,
        created_at=NOW,
    )
    storage = SimpleNamespace(
        upload_part=lambda *_args, **_kwargs: pytest.fail(
            "complete upload rewrote remote state"
        )
    )
    coordinator = CreatorUploadCoordinator(
        SimpleNamespace(get_creator_upload=lambda _session_id: session),
        SimpleNamespace(
            get_manifest=lambda _session_id: manifest,
            get_part=lambda _session_id, _part_number: None,
        ),
        storage,
    )

    with pytest.raises(CreatorUploadMultipartConflict, match="missing part"):
        coordinator.upload_part(
            SESSION_ID,
            expected_version=2,
            part_number=1,
            data=b"x" * 23,
        )


def test_start_upload_durably_aborts_when_storage_initialization_fails() -> None:
    session = _session(CreatorUploadStatus.initiated, version=0)
    events: list[str] = []

    class _Creator:
        def create_creator_upload(self, review_id, *, payload, actor):
            assert review_id == REVIEW_ID
            assert payload.expected_size_bytes == 23
            assert actor == "creator-23"
            return object(), session

        def get_creator_upload(self, session_id):
            assert session_id == SESSION_ID
            return session

        def abort_creator_upload(self, session_id, *, reason):
            events.append("durable-abort")
            assert session_id == SESSION_ID
            assert reason
            return session

    class _Storage:
        def start(self, **_kwargs):
            events.append("remote-start")
            raise RuntimeError("minio unavailable")

    coordinator = CreatorUploadCoordinator(
        _Creator(),
        SimpleNamespace(get_manifest=lambda _session_id: None),
        _Storage(),
    )

    with pytest.raises(RuntimeError, match="minio unavailable"):
        coordinator.start_upload(
            REVIEW_ID,
            payload=CreatorUploadStart(
                expected_size_bytes=23,
                content_type="audio/mpeg",
            ),
            actor="creator-23",
        )

    assert events == ["remote-start", "durable-abort"]
