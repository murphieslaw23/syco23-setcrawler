from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from pathlib import Path
from uuid import UUID

import pytest

from app.repositories.memory import InMemoryRepository
from app.schemas.rights import (
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
)
from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    StoredAudioObject,
)
from app.services.provider_contracts import AuthorizedAudioCandidate


SET_ID = UUID("00000000-0000-4000-8000-000000000002")
FIXED_NOW = datetime(2026, 8, 2, 2, 30, tzinfo=UTC)
EVIDENCE_URL = "https://rights.example/evidence/23"
OBJECT_KEY = "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHECKSUM = "a" * 64


def _evidence() -> RightsEvidenceInput:
    return RightsEvidenceInput(
        evidence_type=RightsEvidenceType.provider_permission,
        reference_url=EVIDENCE_URL,
        assertions={
            "rights_holder": True,
            "allows_distribution": True,
            "allows_derivatives": True,
        },
    )


def _candidate(
    *,
    provider_key: str = "soundcloud",
    external_id: str = "sc-k-zmk",
    evidence_url: str = EVIDENCE_URL,
) -> AuthorizedAudioCandidate:
    return AuthorizedAudioCandidate(
        provider_key=provider_key,
        external_id=external_id,
        source_url="https://api.soundcloud.com/tracks/sc-k-zmk/download",
        evidence_references=(evidence_url,),
        expected_sha256=CHECKSUM,
        evidence={"official_download": True},
    )


def _review_repository(*, approve_download: bool) -> tuple[InMemoryRepository, object]:
    repository = InMemoryRepository.seeded()
    review = repository.create_rights_review(
        RightsReviewCreate(
            set_id=SET_ID,
            provider_key="soundcloud",
            provider_external_id="sc-k-zmk",
            requested_stream=True,
            requested_download=True,
            evidence=[_evidence()],
        ),
        actor="admin-23",
    )
    if approve_download:
        approved = repository.approve_rights_review(
            review.id,
            actor="admin-23",
            allow_stream=True,
            allow_download=True,
            reason="Official download and evidence verified",
        )
        assert approved is not None
        review = approved
    return repository, review


def _stored() -> StoredAudioObject:
    return StoredAudioObject(
        bucket=AUDIO_QUARANTINE_BUCKET,
        key=OBJECT_KEY,
        size=23,
        sha256=CHECKSUM,
        etag="etag-23",
        version_id=None,
        content_type="audio/mpeg",
        metadata={"sha256": CHECKSUM},
    )


def test_pending_or_stream_only_rights_cannot_queue_acquisition() -> None:
    audio = import_module("app.repositories.audio")
    rights_repository, pending = _review_repository(approve_download=False)
    repository = audio.InMemoryAudioRepository(
        rights_repository,
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(audio.AudioAcquisitionPersistenceDenied, match="approved"):
        repository.queue_provider_acquisition(
            pending.id,
            provider_item_external_id="sc-k-zmk",
            candidate=_candidate(),
            actor="admin-23",
        )

    approved_stream_only = rights_repository.approve_rights_review(
        pending.id,
        actor="admin-23",
        allow_stream=True,
        allow_download=False,
        reason="Streaming only",
    )
    assert approved_stream_only is not None
    with pytest.raises(audio.AudioAcquisitionPersistenceDenied, match="download"):
        repository.queue_provider_acquisition(
            pending.id,
            provider_item_external_id="sc-k-zmk",
            candidate=_candidate(),
            actor="admin-23",
        )


def test_queue_requires_review_provider_identity_and_matching_evidence() -> None:
    audio = import_module("app.repositories.audio")
    rights_repository, review = _review_repository(approve_download=True)
    repository = audio.InMemoryAudioRepository(
        rights_repository,
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(audio.AudioAcquisitionPersistenceDenied, match="provider"):
        repository.queue_provider_acquisition(
            review.id,
            provider_item_external_id="other-source",
            candidate=_candidate(),
            actor="admin-23",
        )
    with pytest.raises(audio.AudioAcquisitionPersistenceDenied, match="provider"):
        repository.queue_provider_acquisition(
            review.id,
            provider_item_external_id="sc-k-zmk",
            candidate=_candidate(provider_key="archive-org"),
            actor="admin-23",
        )
    with pytest.raises(audio.AudioAcquisitionPersistenceDenied, match="evidence"):
        repository.queue_provider_acquisition(
            review.id,
            provider_item_external_id="sc-k-zmk",
            candidate=_candidate(
                evidence_url="https://rights.example/evidence/other"
            ),
            actor="admin-23",
        )


def test_queue_is_idempotent_for_one_active_candidate() -> None:
    audio = import_module("app.repositories.audio")
    schemas = import_module("app.schemas.audio")
    rights_repository, review = _review_repository(approve_download=True)
    repository = audio.InMemoryAudioRepository(
        rights_repository,
        clock=lambda: FIXED_NOW,
    )

    first = repository.queue_provider_acquisition(
        review.id,
        provider_item_external_id="sc-k-zmk",
        candidate=_candidate(),
        actor="admin-23",
    )
    second = repository.queue_provider_acquisition(
        review.id,
        provider_item_external_id="sc-k-zmk",
        candidate=_candidate(),
        actor="admin-23",
    )

    assert first == second
    assert first.status is schemas.AudioInputStatus.queued
    assert first.input_kind is schemas.AudioInputKind.provider_acquisition
    assert first.provider_key == "soundcloud"
    assert first.provider_item_external_id == "sc-k-zmk"
    assert first.candidate_external_id == "sc-k-zmk"
    assert first.expected_sha256 == CHECKSUM
    assert len(repository.jobs) == 1
    assert repository.assets == {}


def test_claim_and_completion_atomically_create_quarantine_asset() -> None:
    audio = import_module("app.repositories.audio")
    schemas = import_module("app.schemas.audio")
    rights_repository, review = _review_repository(approve_download=True)
    repository = audio.InMemoryAudioRepository(
        rights_repository,
        clock=lambda: FIXED_NOW,
    )
    queued = repository.queue_provider_acquisition(
        review.id,
        provider_item_external_id="sc-k-zmk",
        candidate=_candidate(),
        actor="admin-23",
    )

    claimed = repository.claim_audio_job(queued.id, claim_ttl_seconds=300)
    assert claimed is not None
    assert claimed.status is schemas.AudioInputStatus.processing
    assert claimed.attempt_count == 1
    assert claimed.claim_started_at == FIXED_NOW

    result = repository.complete_audio_acquisition(
        claimed.id,
        claim_started_at=FIXED_NOW,
        stored=_stored(),
    )

    assert result is not None
    completed, asset = result
    assert completed.status is schemas.AudioInputStatus.completed
    assert completed.audio_asset_id == asset.id
    assert completed.finished_at == FIXED_NOW
    assert asset.rights_review_id == review.id
    assert asset.bucket_name == AUDIO_QUARANTINE_BUCKET
    assert asset.object_key == OBJECT_KEY
    assert asset.checksum_sha256 == CHECKSUM
    assert asset.size_bytes == 23
    assert asset.state is schemas.AudioAssetState.quarantine
    assert asset.expires_at == FIXED_NOW + timedelta(days=30)
    assert repository.get_audio_asset(asset.id) == asset


def test_stale_claim_cannot_create_an_asset() -> None:
    audio = import_module("app.repositories.audio")
    rights_repository, review = _review_repository(approve_download=True)
    repository = audio.InMemoryAudioRepository(
        rights_repository,
        clock=lambda: FIXED_NOW,
    )
    queued = repository.queue_provider_acquisition(
        review.id,
        provider_item_external_id="sc-k-zmk",
        candidate=_candidate(),
        actor="admin-23",
    )
    claimed = repository.claim_audio_job(queued.id, claim_ttl_seconds=300)
    assert claimed is not None

    result = repository.complete_audio_acquisition(
        claimed.id,
        claim_started_at=FIXED_NOW - timedelta(seconds=1),
        stored=_stored(),
    )

    assert result is None
    assert repository.assets == {}


def test_audio_job_migration_is_private_fenced_and_quarantine_only() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260802030000_audio_acquisition_jobs.sql"
    )
    assert migration.exists()
    text = migration.read_text().casefold()

    assert "create table public.audio_input_jobs" in text
    assert "provider_acquisition" in text
    assert "creator_upload" in text
    assert "claim_started_at" in text
    assert "audio_asset_id" in text
    assert "audio_input_jobs_one_active_candidate_idx" in text
    assert "alter table public.audio_input_jobs enable row level security" in text
    assert "grant" in text and "service_role" in text
    assert "grant" not in "\n".join(
        line for line in text.splitlines()
        if "anon" in line
    )
    assert "audio-quarantine" in text
    assert "^objects/" in text
    assert "public_url" not in text


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for audio transaction verification",
)
def test_postgres_completion_fences_claim_and_links_one_quarantine_asset() -> None:
    database = import_module("app.core.database")
    audio = import_module("app.repositories.audio")
    postgres = import_module("app.repositories.postgres")
    rights = import_module("app.schemas.rights")

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    rights_repository = postgres.PostgresRepository(pool)
    repository = audio.PostgresAudioRepository(pool, clock=lambda: FIXED_NOW)
    set_id = UUID("00000000-0000-4000-8000-000000009701")
    provider_item_id = UUID("00000000-0000-4000-8000-000000009702")
    review_id = None
    job_id = None
    asset_id = None

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'audio-source',
                  'https://soundcloud.com/fixture/audio-source',
                  'Audio Fixture Set', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (set_id,),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  'audio-source',
                  'https://soundcloud.com/fixture/audio-source',
                  'Audio Fixture Set'
                )
                """,
                (provider_item_id,),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values (%s, %s, 'source', true)
                """,
                (set_id, provider_item_id),
            )
    try:
        review = rights_repository.create_rights_review(
            rights.RightsReviewCreate(
                set_id=set_id,
                provider_key="soundcloud",
                provider_external_id="audio-source",
                requested_stream=True,
                requested_download=True,
                evidence=[_evidence()],
            ),
            actor="integration-admin",
        )
        review_id = review.id
        approved = rights_repository.approve_rights_review(
            review.id,
            actor="integration-admin",
            allow_stream=True,
            allow_download=True,
            reason="Official download and evidence verified",
        )
        assert approved is not None

        candidate = _candidate(external_id="audio-source")
        queued = repository.queue_provider_acquisition(
            review.id,
            provider_item_external_id="audio-source",
            candidate=candidate,
            actor="integration-admin",
        )
        job_id = queued.id
        claimed = repository.claim_audio_job(queued.id, claim_ttl_seconds=300)
        assert claimed is not None
        assert repository.complete_audio_acquisition(
            claimed.id,
            claim_started_at=FIXED_NOW - timedelta(seconds=1),
            stored=_stored(),
        ) is None

        completed = repository.complete_audio_acquisition(
            claimed.id,
            claim_started_at=FIXED_NOW,
            stored=_stored(),
        )
        assert completed is not None
        completed_job, asset = completed
        asset_id = asset.id
        assert completed_job.audio_asset_id == asset.id

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select j.status, j.audio_asset_id, a.state, a.bucket_name,
                           a.object_key, a.checksum_sha256, a.expires_at
                    from audio_input_jobs j
                    join audio_assets a on a.id = j.audio_asset_id
                    where j.id = %s
                    """,
                    (queued.id,),
                ).fetchone()
        assert row["status"] == "completed"
        assert row["audio_asset_id"] == asset.id
        assert row["state"] == "quarantine"
        assert row["bucket_name"] == AUDIO_QUARANTINE_BUCKET
        assert row["object_key"] == OBJECT_KEY
        assert row["checksum_sha256"] == CHECKSUM
        assert row["expires_at"] == FIXED_NOW + timedelta(days=30)
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                if job_id is not None:
                    cursor.execute("delete from audio_input_jobs where id = %s", (job_id,))
                if asset_id is not None:
                    cursor.execute("delete from audio_assets where id = %s", (asset_id,))
                if review_id is not None:
                    cursor.execute(
                        "delete from audio_permissions where rights_review_id = %s",
                        (review_id,),
                    )
                    cursor.execute(
                        "delete from rights_review_events where rights_review_id = %s",
                        (review_id,),
                    )
                    cursor.execute(
                        "delete from rights_evidence where rights_review_id = %s",
                        (review_id,),
                    )
                    cursor.execute("delete from rights_reviews where id = %s", (review_id,))
                cursor.execute("delete from set_provider_items where set_id = %s", (set_id,))
                cursor.execute("delete from provider_items where id = %s", (provider_item_id,))
                cursor.execute("delete from sets where id = %s", (set_id,))
        pool.close()
