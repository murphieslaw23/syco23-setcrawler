from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.repositories.creator_upload import CreatorUploadPersistenceDenied
from app.repositories.creator_upload_multipart import (
    InMemoryCreatorUploadMultipartRepository,
)
from app.schemas.audio import AudioInputJob, AudioInputKind
from app.schemas.creator_upload import CreatorUploadSession, CreatorUploadStatus
from app.services.creator_upload_storage import MultipartUploadHandle


NOW = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-000000009921")
JOB_ID = UUID("00000000-0000-4000-8000-000000009922")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009923")


class _CreatorRepository:
    def __init__(self, session: CreatorUploadSession) -> None:
        self.sessions = {session.id: session}

    def get_creator_upload(self, session_id: UUID) -> CreatorUploadSession | None:
        return self.sessions.get(session_id)


class _AudioRepository:
    def __init__(self, job: AudioInputJob) -> None:
        self.jobs = {job.id: job}


def test_manifest_identity_drift_raises_persistence_denied_without_mutation() -> None:
    session = CreatorUploadSession(
        id=SESSION_ID,
        audio_input_job_id=JOB_ID,
        rights_review_id=REVIEW_ID,
        expected_size_bytes=5 * 1024 * 1024 + 23,
        content_type="audio/mpeg",
        expected_sha256="a" * 64,
        expires_at=NOW + timedelta(hours=24),
        created_by="creator-23",
        created_at=NOW,
        updated_at=NOW,
    )
    job = AudioInputJob(
        id=JOB_ID,
        rights_review_id=REVIEW_ID,
        input_kind=AudioInputKind.creator_upload,
        candidate_external_id=f"creator-upload:{SESSION_ID}",
        expected_sha256="a" * 64,
        created_by="creator-23",
        created_at=NOW,
        updated_at=NOW,
    )
    creator = _CreatorRepository(session)
    audio = _AudioRepository(job)
    repository = InMemoryCreatorUploadMultipartRepository(
        creator,
        audio,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    handle = MultipartUploadHandle(
        bucket="audio-quarantine",
        key="objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        upload_id="multipart-error-contract",
        expected_size_bytes=session.expected_size_bytes + 1,
        content_type=session.content_type,
        expected_sha256=session.expected_sha256,
        part_size_bytes=5 * 1024 * 1024,
    )

    with pytest.raises(
        CreatorUploadPersistenceDenied,
        match="size does not match",
    ):
        repository.attach_manifest(
            SESSION_ID,
            expected_version=0,
            handle=handle,
        )

    assert creator.sessions[SESSION_ID].status is CreatorUploadStatus.initiated
    assert repository.manifests == {}
