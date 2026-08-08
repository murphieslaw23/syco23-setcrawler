from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest

from app.repositories.audio_lifecycle_postgres import PostgresAudioLifecycleRepository
from app.services.audio_lifecycle import AudioLifecycleExecutor


NOW = datetime(2026, 8, 2, 20, 0, tzinfo=UTC)
SET_ID = UUID("00000000-0000-4000-8000-00000000f101")
REVIEW_ID = UUID("00000000-0000-4000-8000-00000000f102")
ASSET_ID = UUID("00000000-0000-4000-8000-00000000f103")
JOB_ID = UUID("00000000-0000-4000-8000-00000000f104")
CLAIM_TOKEN = UUID("00000000-0000-4000-8000-00000000f105")
KEY = "objects/ff/ffffffffffffffffffffffffffffffff"
CHECKSUM = "f" * 64


class StorageMustNotBeTouched:
    def __init__(self) -> None:
        self.calls = 0

    def stat(self, *_: object, **__: object) -> object:
        self.calls += 1
        raise AssertionError("stale exhausted claim reached storage")

    def copy_to(self, *_: object, **__: object) -> object:
        self.calls += 1
        raise AssertionError("stale exhausted claim reached storage")

    def delete(self, *_: object, **__: object) -> None:
        self.calls += 1
        raise AssertionError("stale exhausted claim reached storage")


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for stale lifecycle claim verification",
)
def test_postgres_stale_claim_at_attempt_budget_is_terminalized_before_storage() -> None:
    database = import_module("app.core.database")

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = PostgresAudioLifecycleRepository(pool, clock=lambda: NOW)
    storage = StorageMustNotBeTouched()

    try:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into sets (
                      id, source, source_id, canonical_url, title,
                      duration_seconds, published_at, set_score, review_status
                    ) values (
                      %s, 'soundcloud', 'stale-lifecycle-source',
                      'https://soundcloud.com/fixture/stale-lifecycle-source',
                      'Stale Lifecycle Fixture', 3600,
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
                      'stale-lifecycle-source',
                      true, true, true, true, 'approved',
                      'integration-admin', 'integration-admin',
                      'approved for stale-claim fixture', %s, %s, %s
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
                      %s, 23, 'audio/mpeg', 'quarantine',
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
                cursor.execute(
                    """
                    insert into audio_asset_lifecycle_jobs (
                      id, audio_asset_id, action, status,
                      claim_token, claim_started_at, attempt_count,
                      actor, reason, created_at, updated_at
                    ) values (
                      %s, %s, 'approve', 'claimed',
                      %s, %s, 3,
                      'integration-admin', 'stale claim fixture', %s, %s
                    )
                    """,
                    (
                        JOB_ID,
                        ASSET_ID,
                        CLAIM_TOKEN,
                        NOW - timedelta(minutes=30),
                        NOW - timedelta(hours=1),
                        NOW - timedelta(minutes=30),
                    ),
                )

        executor = AudioLifecycleExecutor(
            repository,
            storage,
            clock=lambda: NOW,
            max_attempts=3,
            retry_delay=timedelta(minutes=5),
            claim_timeout=timedelta(minutes=15),
        )

        assert executor.run_once(limit=1, now=NOW) == 0
        assert storage.calls == 0

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select status, attempt_count, claim_token,
                           claim_started_at, next_retry_at, last_error
                    from audio_asset_lifecycle_jobs
                    where id = %s
                    """,
                    (JOB_ID,),
                ).fetchone()
        assert row == {
            "status": "failed",
            "attempt_count": 3,
            "claim_token": None,
            "claim_started_at": None,
            "next_retry_at": None,
            "last_error": "stale claim exhausted retry budget",
        }
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                cursor.execute(
                    "delete from audio_asset_lifecycle_jobs where id = %s",
                    (JOB_ID,),
                )
                cursor.execute("delete from audio_assets where id = %s", (ASSET_ID,))
                cursor.execute("delete from rights_reviews where id = %s", (REVIEW_ID,))
                cursor.execute("delete from sets where id = %s", (SET_ID,))
        pool.close()
