from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from uuid import UUID

import pytest


NOW = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-00000000a001")
JOB_ID = UUID("00000000-0000-4000-8000-00000000a002")


def test_cleanup_schema_is_private_bounded_and_append_only() -> None:
    schema = import_module("app.schemas.creator_upload_cleanup")

    job = schema.CreatorUploadCleanupJob(
        id=JOB_ID,
        session_id=SESSION_ID,
        reason=schema.CreatorUploadCleanupReason.user_abort,
        status=schema.CreatorUploadCleanupStatus.queued,
        object_key="objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        storage_upload_id="private-upload-23",
        requested_by="admin-23",
        created_at=NOW,
        updated_at=NOW,
    )
    assert job.attempt_count == 0
    assert job.next_retry_at is None

    tombstone = schema.CreatorUploadCleanupTombstone(
        cleanup_job_id=JOB_ID,
        session_id=SESSION_ID,
        reason=job.reason,
        outcome=schema.CreatorUploadCleanupOutcome.retry,
        attempt_number=1,
        multipart_aborted=False,
        object_deleted=False,
        ledger_deleted=False,
        error_code="minio_unavailable",
        created_at=NOW,
    )
    with pytest.raises(Exception):
        tombstone.outcome = schema.CreatorUploadCleanupOutcome.completed

    public = schema.CreatorUploadCleanupReceipt.from_records(job, tombstone)
    serialized = public.model_dump()
    assert "object_key" not in serialized
    assert "storage_upload_id" not in serialized
    assert "etag" not in serialized
    assert "url" not in serialized


def test_cleanup_migration_is_service_role_only_and_tombstones_are_immutable() -> None:
    root = Path(__file__).parents[3]
    migration = root / "supabase/migrations/20260802180000_creator_upload_cleanup.sql"
    assert migration.exists()
    text = migration.read_text().casefold()

    assert "create table public.creator_upload_cleanup_jobs" in text
    assert "create table public.creator_upload_cleanup_tombstones" in text
    assert "unique (session_id)" in text
    assert "status in ('queued', 'processing', 'retry', 'completed', 'dead_letter')" in text
    assert "reason in ('user_abort', 'admin_abort', 'expired', 'rights_denied', 'verification_failed')" in text
    assert "cleanup tombstones are immutable" in text
    assert "enable row level security" in text
    assert "service_role" in text
    assert "authenticated" in text
    assert "revoke all" in text
    assert "never expose" in text


def test_cleanup_repository_contracts_are_importable() -> None:
    repository = import_module("app.repositories.creator_upload_cleanup")

    assert hasattr(repository, "InMemoryCreatorUploadCleanupRepository")
    assert hasattr(repository, "PostgresCreatorUploadCleanupRepository")
    assert hasattr(repository, "CreatorUploadCleanupConflict")


def test_local_compose_mounts_cleanup_after_part_ledger() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text().casefold()
    ledger = compose.index("20260802150000-creator-upload-part-ledger.sql")
    cleanup = compose.index("20260802180000-creator-upload-cleanup.sql")
    assert ledger < cleanup
