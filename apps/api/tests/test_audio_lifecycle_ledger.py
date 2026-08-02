from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.audio_lifecycle import (
    AudioLifecycleAction,
    AudioLifecycleJob,
    AudioLifecycleJobStatus,
    AudioLifecycleTombstone,
    AudioStorageOutcome,
)


NOW = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
ASSET_ID = UUID("00000000-0000-4000-8000-000000009901")
JOB_ID = UUID("00000000-0000-4000-8000-000000009902")
CLAIM_TOKEN = UUID("00000000-0000-4000-8000-000000009903")
CHECKSUM = "c" * 64


def test_lifecycle_job_requires_claim_and_terminal_fences() -> None:
    queued = AudioLifecycleJob(
        audio_asset_id=ASSET_ID,
        action=AudioLifecycleAction.expire,
        actor="system-expiry",
        reason="quarantine exceeded 30 days",
    )
    assert queued.status is AudioLifecycleJobStatus.queued

    with pytest.raises(ValidationError, match="claim fence"):
        AudioLifecycleJob(
            audio_asset_id=ASSET_ID,
            action=AudioLifecycleAction.expire,
            status=AudioLifecycleJobStatus.claimed,
            actor="system-expiry",
            reason="quarantine exceeded 30 days",
        )
    with pytest.raises(ValidationError, match="only claimed"):
        AudioLifecycleJob(
            audio_asset_id=ASSET_ID,
            action=AudioLifecycleAction.expire,
            claim_token=CLAIM_TOKEN,
            claim_started_at=NOW,
            actor="system-expiry",
            reason="quarantine exceeded 30 days",
        )
    with pytest.raises(ValidationError, match="completion time"):
        AudioLifecycleJob(
            audio_asset_id=ASSET_ID,
            action=AudioLifecycleAction.expire,
            status=AudioLifecycleJobStatus.completed,
            actor="system-expiry",
            reason="quarantine exceeded 30 days",
        )


def test_tombstone_keeps_bounded_audit_evidence() -> None:
    tombstone = AudioLifecycleTombstone(
        lifecycle_job_id=JOB_ID,
        audio_asset_id=ASSET_ID,
        action=AudioLifecycleAction.expire,
        actor="system-expiry",
        reason="quarantine exceeded 30 days",
        storage_outcome=AudioStorageOutcome.deleted,
        checksum_sha256=CHECKSUM,
        size_bytes=23,
        before_state={"state": "quarantine"},
        after_state={"state": "expired"},
    )
    assert tombstone.storage_outcome is AudioStorageOutcome.deleted

    with pytest.raises(ValidationError, match="String should match pattern"):
        AudioLifecycleTombstone(
            lifecycle_job_id=JOB_ID,
            audio_asset_id=ASSET_ID,
            action=AudioLifecycleAction.expire,
            actor="system-expiry",
            reason="quarantine exceeded 30 days",
            storage_outcome=AudioStorageOutcome.deleted,
            checksum_sha256="not-a-checksum",
            size_bytes=23,
            before_state={},
            after_state={},
        )


def test_lifecycle_migration_is_private_claimable_and_immutable() -> None:
    root = Path(__file__).parents[3]
    migration = root / "supabase/migrations/20260802170000_audio_lifecycle_ledger.sql"
    text = migration.read_text().casefold()

    assert "create table public.audio_asset_lifecycle_jobs" in text
    assert "create table public.audio_asset_lifecycle_tombstones" in text
    assert "audio_asset_lifecycle_one_active_idx" in text
    assert "claim_token" in text and "claim_started_at" in text
    assert "audio_assets_quarantine_expiry_idx" in text
    assert "tombstones are immutable" in text
    assert "alter table public.audio_asset_lifecycle_jobs enable row level security" in text
    assert "alter table public.audio_asset_lifecycle_tombstones enable row level security" in text
    assert "service_role" in text
    assert "public_url" not in text
    assert "presigned" not in text

    authenticated_grants = [
        line for line in text.splitlines()
        if line.strip().startswith("grant") and "authenticated" in line
    ]
    anon_grants = [
        line for line in text.splitlines()
        if line.strip().startswith("grant") and "anon" in line
    ]
    assert authenticated_grants == []
    assert anon_grants == []
