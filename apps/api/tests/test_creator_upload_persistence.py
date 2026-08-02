from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.creator_upload import (
    CreatorUploadAttestation,
    CreatorUploadSession,
    CreatorUploadStart,
    CreatorUploadStatus,
    MAX_CREATOR_UPLOAD_BYTES,
)


FIXED_NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
JOB_ID = UUID("00000000-0000-4000-8000-000000009801")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009802")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000009803")
CHECKSUM = "a" * 64
OBJECT_KEY = "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STORAGE_UPLOAD_ID = "internal-upload-23"


def _session(**changes: object) -> CreatorUploadSession:
    values: dict[str, object] = {
        "audio_input_job_id": JOB_ID,
        "rights_review_id": REVIEW_ID,
        "expected_size_bytes": 23,
        "received_size_bytes": 0,
        "content_type": "audio/mpeg",
        "expected_sha256": CHECKSUM,
        "status": CreatorUploadStatus.initiated,
        "expires_at": FIXED_NOW + timedelta(hours=24),
        "created_by": "creator-23",
        "created_at": FIXED_NOW,
        "updated_at": FIXED_NOW,
    }
    values.update(changes)
    return CreatorUploadSession(**values)


def _active_storage() -> dict[str, str]:
    return {
        "staging_object_key": OBJECT_KEY,
        "storage_upload_id": STORAGE_UPLOAD_ID,
    }


def test_creator_upload_start_normalizes_and_bounds_input() -> None:
    start = CreatorUploadStart(
        expected_size_bytes=23,
        content_type=" Audio/MPEG ",
        expected_sha256=CHECKSUM,
    )

    assert start.content_type == "audio/mpeg"
    assert start.expected_sha256 == CHECKSUM

    with pytest.raises(ValidationError, match="less than or equal"):
        CreatorUploadStart(
            expected_size_bytes=MAX_CREATOR_UPLOAD_BYTES + 1,
            content_type="audio/mpeg",
        )
    with pytest.raises(ValidationError, match="content type"):
        CreatorUploadStart(
            expected_size_bytes=23,
            content_type="application/octet-stream",
        )
    with pytest.raises(ValidationError, match="lowercase hexadecimal"):
        CreatorUploadStart(
            expected_size_bytes=23,
            content_type="audio/mpeg",
            expected_sha256="A" * 64,
        )


def test_creator_attestation_requires_https_and_explicit_rights() -> None:
    attestation = CreatorUploadAttestation(
        reference_url="https://rights.example/attestations/23",
        assertions={
            "rights_holder": True,
            "allows_distribution": True,
            "allows_derivatives": True,
        },
        expected_version=2,
    )

    assert attestation.expected_version == 2

    with pytest.raises(ValidationError, match="HTTPS"):
        CreatorUploadAttestation(
            reference_url="http://rights.example/attestations/23",
            assertions={
                "rights_holder": True,
                "allows_distribution": True,
                "allows_derivatives": True,
            },
            expected_version=2,
        )
    with pytest.raises(ValidationError, match="ownership"):
        CreatorUploadAttestation(
            reference_url="https://rights.example/attestations/23",
            assertions={
                "rights_holder": True,
                "allows_distribution": True,
                "allows_derivatives": False,
            },
            expected_version=2,
        )


def test_creator_upload_progress_storage_and_attestation_are_fenced() -> None:
    assert _session().status is CreatorUploadStatus.initiated

    with pytest.raises(ValidationError, match="cannot carry storage"):
        _session(**_active_storage())
    with pytest.raises(ValidationError, match="private storage state"):
        _session(status=CreatorUploadStatus.uploading)
    with pytest.raises(ValidationError, match="exceed"):
        _session(received_size_bytes=24)
    with pytest.raises(ValidationError, match="complete upload"):
        _session(
            received_size_bytes=22,
            status=CreatorUploadStatus.awaiting_attestation,
            **_active_storage(),
        )
    with pytest.raises(ValidationError, match="require attestation"):
        _session(
            received_size_bytes=23,
            status=CreatorUploadStatus.completed,
            **_active_storage(),
        )
    with pytest.raises(ValidationError, match="only completed"):
        _session(
            attestation_evidence_id=EVIDENCE_ID,
            attested_by="creator-23",
            attested_at=FIXED_NOW,
        )
    with pytest.raises(ValidationError, match="expiry"):
        _session(expires_at=FIXED_NOW)

    completed = _session(
        received_size_bytes=23,
        status=CreatorUploadStatus.completed,
        attestation_evidence_id=EVIDENCE_ID,
        attested_by="creator-23",
        attested_at=FIXED_NOW + timedelta(minutes=1),
        version=3,
        **_active_storage(),
    )
    assert completed.received_size_bytes == completed.expected_size_bytes
    assert completed.attestation_evidence_id == EVIDENCE_ID
    assert completed.staging_object_key == OBJECT_KEY


def test_creator_upload_migration_is_private_and_job_bound() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260802120000_creator_upload_sessions.sql"
    )
    assert migration.exists()
    text = migration.read_text().casefold()

    assert "create table public.creator_upload_sessions" in text
    assert "references public.audio_input_jobs" in text
    assert "input_job.input_kind <> 'creator_upload'" in text
    assert "creator_attestation" in text
    assert "received_size_bytes = expected_size_bytes" in text
    assert "status = 'initiated'" in text
    assert "staging_object_key is null" in text
    assert "attestation is immutable" in text
    assert "alter table public.creator_upload_sessions enable row level security" in text
    assert "service_role" in text
    assert "never returned to the nuxt client" in text
    assert "public_url" not in text
    assert "presigned" not in text
