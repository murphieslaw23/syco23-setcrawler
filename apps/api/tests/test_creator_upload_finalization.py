from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.repositories.creator_upload import CreatorUploadPersistenceDenied
from app.schemas.audio import (
    AudioAssetRecord,
    AudioAssetState,
    AudioBucket,
    AudioInputJob,
    AudioInputKind,
    AudioInputStatus,
)
from app.schemas.creator_upload import (
    CreatorUploadAttestation,
    CreatorUploadSession,
    CreatorUploadStatus,
)
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)
from app.services.audio_storage import AUDIO_QUARANTINE_BUCKET, StoredAudioObject
from app.services.creator_upload_finalization import (
    CreatorUploadCompletionReceipt,
    CreatorUploadFinalizationCompensationError,
    CreatorUploadFinalizationConflict,
    CreatorUploadFinalizer,
)
from app.services.creator_upload_storage import (
    MinioCreatorUploadStorage,
    MultipartUploadHandle,
    UploadedPart,
)


NOW = datetime(2026, 8, 2, 16, 0, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-000000009981")
JOB_ID = UUID("00000000-0000-4000-8000-000000009982")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009983")
ASSET_ID = UUID("00000000-0000-4000-8000-000000009984")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000009985")
OBJECT_KEY = "objects/dd/dddddddddddddddddddddddddddddddd"
UPLOAD_ID = "finalization-upload-23"
PAYLOAD = b"creator-finalization-23"
PAYLOAD_SHA = sha256(PAYLOAD).hexdigest()
PART_SIZE = 5 * 1024 * 1024


def _session(
    *,
    status: CreatorUploadStatus = CreatorUploadStatus.awaiting_attestation,
    version: int = 2,
) -> CreatorUploadSession:
    complete = status is CreatorUploadStatus.completed
    return CreatorUploadSession(
        id=SESSION_ID,
        audio_input_job_id=JOB_ID,
        rights_review_id=REVIEW_ID,
        expected_size_bytes=len(PAYLOAD),
        received_size_bytes=len(PAYLOAD),
        content_type="audio/mpeg",
        expected_sha256=PAYLOAD_SHA,
        staging_object_key=OBJECT_KEY,
        storage_upload_id=UPLOAD_ID,
        status=status,
        attestation_evidence_id=EVIDENCE_ID if complete else None,
        attested_by="creator-23" if complete else None,
        attested_at=NOW + timedelta(minutes=1) if complete else None,
        expires_at=NOW + timedelta(hours=24),
        created_by="creator-23",
        version=version,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )


def _manifest() -> CreatorUploadManifest:
    return CreatorUploadManifest(
        session_id=SESSION_ID,
        part_size_bytes=PART_SIZE,
        expected_part_count=1,
        created_at=NOW,
    )


def _part_record() -> CreatorUploadPartRecord:
    return CreatorUploadPartRecord(
        session_id=SESSION_ID,
        part_number=1,
        etag="etag-final-23",
        size_bytes=len(PAYLOAD),
        checksum_sha256=PAYLOAD_SHA,
        created_at=NOW,
    )


def _uploaded_part() -> UploadedPart:
    record = _part_record()
    return UploadedPart(
        part_number=record.part_number,
        etag=record.etag,
        size_bytes=record.size_bytes,
        checksum_sha256=record.checksum_sha256,
    )


def _handle() -> MultipartUploadHandle:
    return MultipartUploadHandle(
        bucket=AUDIO_QUARANTINE_BUCKET,
        key=OBJECT_KEY,
        upload_id=UPLOAD_ID,
        expected_size_bytes=len(PAYLOAD),
        content_type="audio/mpeg",
        expected_sha256=PAYLOAD_SHA,
        part_size_bytes=PART_SIZE,
    )


def _stored() -> StoredAudioObject:
    return StoredAudioObject(
        bucket=AUDIO_QUARANTINE_BUCKET,
        key=OBJECT_KEY,
        size=len(PAYLOAD),
        sha256=PAYLOAD_SHA,
        etag="completed-etag",
        version_id="version-23",
        content_type="audio/mpeg",
        metadata={"sha256": PAYLOAD_SHA},
    )


def _attestation(*, expected_version: int = 2) -> CreatorUploadAttestation:
    return CreatorUploadAttestation(
        reference_url="https://rights.example/attestations/final-23",
        assertions={
            "rights_holder": True,
            "allows_distribution": True,
            "allows_derivatives": True,
        },
        expected_version=expected_version,
    )


def _completed_records() -> tuple[CreatorUploadSession, AudioInputJob, AudioAssetRecord]:
    completed = _session(status=CreatorUploadStatus.completed, version=3)
    job = AudioInputJob(
        id=JOB_ID,
        rights_review_id=REVIEW_ID,
        input_kind=AudioInputKind.creator_upload,
        candidate_external_id=f"creator-upload:{SESSION_ID}",
        expected_sha256=PAYLOAD_SHA,
        status=AudioInputStatus.completed,
        attempt_count=1,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        audio_asset_id=ASSET_ID,
        created_by="creator-23",
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )
    asset = AudioAssetRecord(
        id=ASSET_ID,
        rights_review_id=REVIEW_ID,
        state=AudioAssetState.quarantine,
        bucket_name=AudioBucket.quarantine,
        object_key=OBJECT_KEY,
        checksum_sha256=PAYLOAD_SHA,
        size_bytes=len(PAYLOAD),
        content_type="audio/mpeg",
        expires_at=NOW + timedelta(days=30),
        created_at=NOW + timedelta(minutes=1),
        updated_at=NOW + timedelta(minutes=1),
    )
    return completed, job, asset


class _NoSuchUpload(RuntimeError):
    code = "NoSuchUpload"


class _Response(BytesIO):
    def release_conn(self) -> None:
        pass


class _ResumeClient:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []

    def _create_multipart_upload(self, *_args: Any, **_kwargs: Any) -> str:
        return UPLOAD_ID

    def _upload_part(self, *_args: Any, **_kwargs: Any) -> str:
        return "etag"

    def _complete_multipart_upload(self, *_args: Any, **_kwargs: Any) -> Any:
        raise _NoSuchUpload("already committed")

    def _abort_multipart_upload(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def stat_object(self, bucket: str, key: str) -> Any:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert key == OBJECT_KEY
        return SimpleNamespace(
            size=len(PAYLOAD),
            content_type="audio/mpeg",
            etag="existing-etag",
            version_id="existing-version",
            last_modified=None,
            metadata={},
        )

    def get_object(self, bucket: str, key: str) -> _Response:
        assert bucket == AUDIO_QUARANTINE_BUCKET
        assert key == OBJECT_KEY
        return _Response(PAYLOAD)

    def remove_object(self, bucket: str, key: str) -> None:
        self.removed.append((bucket, key))


def test_storage_completion_resumes_an_already_committed_private_object() -> None:
    client = _ResumeClient()
    storage = MinioCreatorUploadStorage(
        client,
        max_object_bytes=PART_SIZE,
        part_size_bytes=PART_SIZE,
    )

    stored = storage.complete(_handle(), [_uploaded_part()])

    assert stored.bucket == AUDIO_QUARANTINE_BUCKET
    assert stored.key == OBJECT_KEY
    assert stored.sha256 == PAYLOAD_SHA
    assert stored.etag == "existing-etag"
    assert stored.version_id == "existing-version"
    assert client.removed == []


class _Repository:
    def __init__(self, *, parts: list[CreatorUploadPartRecord] | None = None) -> None:
        self.session = _session()
        self.parts = [_part_record()] if parts is None else parts
        self.complete_calls = 0
        self.complete_error: Exception | None = None
        self.abort_calls = 0

    def get_creator_upload(self, session_id: UUID) -> CreatorUploadSession | None:
        return self.session if session_id == SESSION_ID else None

    def get_manifest(self, session_id: UUID) -> CreatorUploadManifest | None:
        return _manifest() if session_id == SESSION_ID else None

    def list_parts(self, session_id: UUID) -> list[CreatorUploadPartRecord]:
        assert session_id == SESSION_ID
        return list(self.parts)

    def complete_creator_upload(self, session_id: UUID, **kwargs: Any) -> Any:
        assert session_id == SESSION_ID
        assert kwargs["attestation"] == _attestation()
        assert kwargs["actor"] == "creator-23"
        assert kwargs["stored"] == _stored()
        self.complete_calls += 1
        if self.complete_error is not None:
            error = self.complete_error
            self.complete_error = None
            raise error
        records = _completed_records()
        self.session = records[0]
        return records

    def abort_creator_upload(self, session_id: UUID, *, reason: str) -> CreatorUploadSession:
        assert session_id == SESSION_ID
        assert reason
        self.abort_calls += 1
        self.session = CreatorUploadSession.model_validate(
            {
                **self.session.model_dump(),
                "status": CreatorUploadStatus.aborted,
                "version": self.session.version + 1,
                "updated_at": NOW + timedelta(minutes=2),
            }
        )
        return self.session


class _MultipartStorage:
    def __init__(self) -> None:
        self.complete_calls = 0
        self.abort_calls = 0

    def complete(self, handle: MultipartUploadHandle, parts: list[UploadedPart]) -> StoredAudioObject:
        assert handle == _handle()
        assert parts == [_uploaded_part()]
        self.complete_calls += 1
        return _stored()

    def abort(self, handle: MultipartUploadHandle) -> None:
        assert handle == _handle()
        self.abort_calls += 1


class _ObjectStorage:
    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, str]] = []
        self.delete_error: Exception | None = None

    def delete(self, bucket: str, key: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_calls.append((bucket, key))


def _finalizer(
    *,
    parts: list[CreatorUploadPartRecord] | None = None,
) -> tuple[CreatorUploadFinalizer, _Repository, _MultipartStorage, _ObjectStorage]:
    repository = _Repository(parts=parts)
    multipart = _MultipartStorage()
    objects = _ObjectStorage()
    return (
        CreatorUploadFinalizer(repository, multipart, objects),
        repository,
        multipart,
        objects,
    )


def test_finalization_commits_storage_then_atomic_database_state() -> None:
    finalizer, repository, multipart, objects = _finalizer()

    result = finalizer.finalize(
        SESSION_ID,
        attestation=_attestation(),
        actor="creator-23",
    )

    assert isinstance(result, CreatorUploadCompletionReceipt)
    assert result.session_id == SESSION_ID
    assert result.status is CreatorUploadStatus.completed
    assert result.version == 3
    assert result.attested_by == "creator-23"
    assert multipart.complete_calls == 1
    assert repository.complete_calls == 1
    assert objects.delete_calls == []
    serialized = asdict(result)
    assert "bucket" not in serialized
    assert "object_key" not in serialized
    assert "storage_upload_id" not in serialized
    assert "etag" not in serialized
    assert "version_id" not in serialized
    assert "url" not in serialized


def test_transient_database_failure_leaves_verified_object_for_retry() -> None:
    finalizer, repository, multipart, objects = _finalizer()
    repository.complete_error = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        finalizer.finalize(
            SESSION_ID,
            attestation=_attestation(),
            actor="creator-23",
        )

    assert repository.session.status is CreatorUploadStatus.awaiting_attestation
    assert repository.abort_calls == 0
    assert objects.delete_calls == []

    result = finalizer.finalize(
        SESSION_ID,
        attestation=_attestation(),
        actor="creator-23",
    )
    assert result.status is CreatorUploadStatus.completed
    assert multipart.complete_calls == 2
    assert repository.complete_calls == 2


def test_rights_denial_deletes_verified_object_and_durably_aborts() -> None:
    finalizer, repository, _multipart, objects = _finalizer()
    repository.complete_error = CreatorUploadPersistenceDenied("rights expired")

    with pytest.raises(CreatorUploadPersistenceDenied, match="rights expired"):
        finalizer.finalize(
            SESSION_ID,
            attestation=_attestation(),
            actor="creator-23",
        )

    assert repository.abort_calls == 1
    assert repository.session.status is CreatorUploadStatus.aborted
    assert objects.delete_calls == [(AUDIO_QUARANTINE_BUCKET, OBJECT_KEY)]


def test_rights_denial_preserves_cleanup_failures() -> None:
    finalizer, repository, _multipart, objects = _finalizer()
    repository.complete_error = CreatorUploadPersistenceDenied("rights expired")
    objects.delete_error = RuntimeError("storage cleanup unavailable")

    with pytest.raises(CreatorUploadFinalizationCompensationError) as error:
        finalizer.finalize(
            SESSION_ID,
            attestation=_attestation(),
            actor="creator-23",
        )

    assert isinstance(error.value.primary_error, CreatorUploadPersistenceDenied)
    assert len(error.value.compensation_errors) == 1
    assert repository.abort_calls == 1


def test_missing_or_conflicting_ledger_blocks_remote_completion() -> None:
    finalizer, repository, multipart, _objects = _finalizer(parts=[])

    with pytest.raises(CreatorUploadFinalizationConflict, match="every planned part"):
        finalizer.finalize(
            SESSION_ID,
            attestation=_attestation(),
            actor="creator-23",
        )

    assert multipart.complete_calls == 0
    assert repository.complete_calls == 0


def test_completed_replay_returns_receipt_without_storage_access() -> None:
    finalizer, repository, multipart, objects = _finalizer()
    repository.session = _session(status=CreatorUploadStatus.completed, version=3)

    result = finalizer.finalize(
        SESSION_ID,
        attestation=_attestation(expected_version=2),
        actor="creator-23",
    )

    assert result.status is CreatorUploadStatus.completed
    assert result.version == 3
    assert multipart.complete_calls == 0
    assert objects.delete_calls == []
