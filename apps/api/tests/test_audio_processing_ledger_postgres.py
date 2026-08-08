from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest

from app.services.audio_processing import AudioProbe


NOW = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)
SET_ID = UUID("00000000-0000-4000-8000-00000000e101")
REVIEW_ID = UUID("00000000-0000-4000-8000-00000000e102")
ASSET_ID = UUID("00000000-0000-4000-8000-00000000e103")
KEY = "objects/ee/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
DERIVATIVE_KEY = "objects/dd/dddddddddddddddddddddddddddd"
CHECKSUM = "e" * 64
DERIVATIVE_CHECKSUM = "d" * 64


@pytest.fixture
def processing_db():
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is required")
    database = import_module("app.core.database")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'processing-ledger-fixture',
                  'https://soundcloud.com/fixture/processing-ledger-fixture',
                  'Processing Ledger Fixture', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (SET_ID,),
            )
            cursor.execute(
                """
                insert into rights_reviews (
                  id, set_id, provider_id, provider_external_id,
                  requested_stream, requested_download,
                  allow_stream, allow_download, status,
                  submitted_by, decided_by, decision_reason, decided_at,
                  created_at, updated_at
                ) values (
                  %s, %s,
                  (select id from providers where key = 'soundcloud'),
                  'processing-ledger-fixture',
                  true, true, true, true, 'approved',
                  'integration-admin', 'integration-admin',
                  'approved fixture', %s, %s, %s
                )
                """,
                (REVIEW_ID, SET_ID, NOW, NOW, NOW),
            )
            cursor.execute(
                """
                insert into audio_assets (
                  id, rights_review_id, bucket_name, object_key,
                  checksum_sha256, size_bytes, content_type,
                  state, expires_at, created_at, updated_at
                ) values (
                  %s, %s, 'audio-quarantine', %s,
                  %s, 23, 'audio/flac', 'quarantine',
                  %s, %s, %s
                )
                """,
                (
                    ASSET_ID,
                    REVIEW_ID,
                    KEY,
                    CHECKSUM,
                    NOW + timedelta(days=30),
                    NOW,
                    NOW,
                ),
            )
    try:
        yield pool
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                cursor.execute(
                    "delete from audio_versions where audio_asset_id = %s",
                    (ASSET_ID,),
                )
                cursor.execute(
                    "delete from audio_processing_jobs where audio_asset_id = %s",
                    (ASSET_ID,),
                )
                cursor.execute("delete from audio_assets where id = %s", (ASSET_ID,))
                cursor.execute("delete from audio_permissions where rights_review_id = %s", (REVIEW_ID,))
                cursor.execute("delete from rights_review_events where rights_review_id = %s", (REVIEW_ID,))
                cursor.execute("delete from rights_evidence where rights_review_id = %s", (REVIEW_ID,))
                cursor.execute("delete from rights_reviews where id = %s", (REVIEW_ID,))
                cursor.execute("delete from sets where id = %s", (SET_ID,))
        pool.close()


def approve_asset(pool) -> None:
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update audio_assets
                set state = 'approved',
                    bucket_name = 'audio-originals',
                    expires_at = null,
                    updated_at = %s
                where id = %s
                """,
                (NOW, ASSET_ID),
            )


def probe(codec: str = "flac", bitrate: int = 1_000_000) -> AudioProbe:
    return AudioProbe(
        codec_name=codec,
        format_name=codec,
        duration_seconds=3600.0,
        bit_rate=bitrate,
        sample_rate=48_000,
        channels=2,
        tags={"artist": "SYCO23", "title": "Ledger Fixture"},
    )


def test_approved_original_queues_exactly_one_processing_job(processing_db) -> None:
    approve_asset(processing_db)
    with processing_db.connection() as connection:
        with connection.cursor() as cursor:
            rows = cursor.execute(
                "select audio_asset_id, status, attempt_count from audio_processing_jobs where audio_asset_id = %s",
                (ASSET_ID,),
            ).fetchall()
            cursor.execute(
                "update audio_assets set updated_at = %s where id = %s",
                (NOW + timedelta(seconds=1), ASSET_ID),
            )
            count = cursor.execute(
                "select count(*) as count from audio_processing_jobs where audio_asset_id = %s",
                (ASSET_ID,),
            ).fetchone()["count"]
    assert rows == [{"audio_asset_id": ASSET_ID, "status": "queued", "attempt_count": 0}]
    assert count == 1


def test_claim_transitions_approved_asset_to_processing(processing_db) -> None:
    from app.repositories.audio_processing_postgres import PostgresAudioProcessingRepository

    approve_asset(processing_db)
    repository = PostgresAudioProcessingRepository(processing_db, clock=lambda: NOW)
    jobs = repository.claim_due(limit=1, now=NOW, stale_before=NOW - timedelta(minutes=15), max_attempts=3)

    assert len(jobs) == 1
    assert jobs[0].attempt_count == 1
    assert jobs[0].claim_token is not None
    with processing_db.connection() as connection:
        with connection.cursor() as cursor:
            row = cursor.execute("select state, bucket_name from audio_assets where id = %s", (ASSET_ID,)).fetchone()
    assert row == {"state": "processing", "bucket_name": "audio-originals"}


def test_reuse_completion_records_original_probe_and_marks_ready(processing_db) -> None:
    from app.repositories.audio_processing_postgres import PostgresAudioProcessingRepository

    approve_asset(processing_db)
    repository = PostgresAudioProcessingRepository(processing_db, clock=lambda: NOW)
    job = repository.claim_due(limit=1, now=NOW, stale_before=NOW - timedelta(minutes=15), max_attempts=3)[0]
    repository.complete_reuse(job.id, claim_token=job.claim_token, probe=probe("mp3", 256_000))

    with processing_db.connection() as connection:
        with connection.cursor() as cursor:
            asset = cursor.execute("select state, bucket_name from audio_assets where id = %s", (ASSET_ID,)).fetchone()
            versions = cursor.execute(
                """
                select version_type, bucket_name, object_key, codec_name,
                       bit_rate, sample_rate, channels, metadata_tags
                from audio_versions where audio_asset_id = %s
                """,
                (ASSET_ID,),
            ).fetchall()
    assert asset == {"state": "ready", "bucket_name": "audio-originals"}
    assert versions == [{
        "version_type": "original",
        "bucket_name": "audio-originals",
        "object_key": KEY,
        "codec_name": "mp3",
        "bit_rate": 256_000,
        "sample_rate": 48_000,
        "channels": 2,
        "metadata_tags": {"artist": "SYCO23", "title": "Ledger Fixture"},
    }]


def test_derivative_completion_records_both_versions_atomically(processing_db) -> None:
    from app.repositories.audio_processing_postgres import PostgresAudioProcessingRepository
    from app.services.audio_storage import StoredAudioObject

    approve_asset(processing_db)
    repository = PostgresAudioProcessingRepository(processing_db, clock=lambda: NOW)
    job = repository.claim_due(limit=1, now=NOW, stale_before=NOW - timedelta(minutes=15), max_attempts=3)[0]
    repository.complete_derivative(
        job.id,
        claim_token=job.claim_token,
        original_probe=probe("flac", 1_000_000),
        derivative=StoredAudioObject(
            bucket="audio-derivatives",
            key=DERIVATIVE_KEY,
            size=42,
            sha256=DERIVATIVE_CHECKSUM,
            etag="etag",
            version_id=None,
            content_type="audio/mpeg",
        ),
        derivative_probe=probe("mp3", 256_000),
    )

    with processing_db.connection() as connection:
        with connection.cursor() as cursor:
            state = cursor.execute("select state from audio_assets where id = %s", (ASSET_ID,)).fetchone()["state"]
            versions = cursor.execute(
                "select version_type, bucket_name, object_key, codec_name, bit_rate from audio_versions where audio_asset_id = %s order by version_type",
                (ASSET_ID,),
            ).fetchall()
    assert state == "ready"
    assert versions == [
        {"version_type": "derivative", "bucket_name": "audio-derivatives", "object_key": DERIVATIVE_KEY, "codec_name": "mp3", "bit_rate": 256_000},
        {"version_type": "original", "bucket_name": "audio-originals", "object_key": KEY, "codec_name": "flac", "bit_rate": 1_000_000},
    ]


def test_terminal_processing_failure_preserves_original_and_marks_failed(processing_db) -> None:
    from app.repositories.audio_processing_postgres import PostgresAudioProcessingRepository

    approve_asset(processing_db)
    repository = PostgresAudioProcessingRepository(processing_db, clock=lambda: NOW)
    job = repository.claim_due(limit=1, now=NOW, stale_before=NOW - timedelta(minutes=15), max_attempts=1)[0]
    failed = repository.record_failure(
        job.id,
        claim_token=job.claim_token,
        error="AudioProbeError: corrupt input",
        retry_at=NOW + timedelta(minutes=5),
        max_attempts=1,
    )
    assert failed.status.value == "failed"
    with processing_db.connection() as connection:
        with connection.cursor() as cursor:
            asset = cursor.execute("select state, bucket_name, object_key from audio_assets where id = %s", (ASSET_ID,)).fetchone()
    assert asset == {"state": "failed", "bucket_name": "audio-originals", "object_key": KEY}
