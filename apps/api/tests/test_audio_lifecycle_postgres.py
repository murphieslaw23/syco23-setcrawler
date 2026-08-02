from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest

from app.repositories.audio_lifecycle import AudioLifecycleConflict
from app.repositories.audio_lifecycle_postgres import (
    PostgresAudioLifecycleRepository,
)
from app.schemas.audio_lifecycle import AudioLifecycleAction, AudioStorageOutcome
from app.schemas.rights import (
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
)


NOW = datetime(2026, 8, 2, 18, 30, tzinfo=UTC)
SET_ID = UUID("00000000-0000-4000-8000-00000000b101")
PROVIDER_ITEM_ID = UUID("00000000-0000-4000-8000-00000000b102")
ASSET_ID = UUID("00000000-0000-4000-8000-00000000b103")
WRONG_TOKEN = UUID("00000000-0000-4000-8000-00000000b104")
KEY = "objects/bb/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CHECKSUM = "b" * 64


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for lifecycle transaction verification",
)
def test_postgres_lifecycle_completion_is_claim_fenced_and_atomic() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    rights_repository = postgres.PostgresRepository(pool)
    repository = PostgresAudioLifecycleRepository(pool, clock=lambda: NOW)
    review_id = None
    job_id = None

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'lifecycle-source',
                  'https://soundcloud.com/fixture/lifecycle-source',
                  'Lifecycle Fixture Set', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (SET_ID,),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  'lifecycle-source',
                  'https://soundcloud.com/fixture/lifecycle-source',
                  'Lifecycle Fixture Set'
                )
                """,
                (PROVIDER_ITEM_ID,),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values (%s, %s, 'source', true)
                """,
                (SET_ID, PROVIDER_ITEM_ID),
            )
    try:
        review = rights_repository.create_rights_review(
            RightsReviewCreate(
                set_id=SET_ID,
                provider_key="soundcloud",
                provider_external_id="lifecycle-source",
                requested_stream=True,
                requested_download=True,
                evidence=[
                    RightsEvidenceInput(
                        evidence_type=RightsEvidenceType.provider_permission,
                        reference_url="https://rights.example/lifecycle/23",
                        assertions={
                            "rights_holder": True,
                            "allows_distribution": True,
                            "allows_derivatives": True,
                        },
                    )
                ],
            ),
            actor="integration-admin",
        )
        review_id = review.id
        approved = rights_repository.approve_rights_review(
            review.id,
            actor="integration-admin",
            allow_stream=True,
            allow_download=True,
            reason="lifecycle fixture approved",
        )
        assert approved is not None

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into audio_assets (
                      id, rights_review_id, bucket_name, object_key,
                      checksum_sha256, size_bytes, content_type,
                      state, expires_at, created_at, updated_at
                    ) values (
                      %s, %s, 'audio-quarantine', %s,
                      %s, 23, 'audio/mpeg',
                      'quarantine', %s, %s, %s
                    )
                    """,
                    (
                        ASSET_ID,
                        review.id,
                        KEY,
                        CHECKSUM,
                        NOW + timedelta(days=30),
                        NOW,
                        NOW,
                    ),
                )

        queued = repository.enqueue_lifecycle(
            ASSET_ID,
            action=AudioLifecycleAction.approve,
            actor="integration-admin",
            reason="promote verified asset",
        )
        job_id = queued.id
        claimed = repository.claim_due(limit=1, now=NOW)
        assert len(claimed) == 1
        assert claimed[0].claim_token is not None

        with pytest.raises(AudioLifecycleConflict, match="claim changed"):
            repository.complete_lifecycle(
                queued.id,
                claim_token=WRONG_TOKEN,
                storage_outcome=AudioStorageOutcome.promoted,
                destination_key=KEY,
            )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                unchanged = cursor.execute(
                    "select state, bucket_name from audio_assets where id = %s",
                    (ASSET_ID,),
                ).fetchone()
                tombstone_count = cursor.execute(
                    """
                    select count(*) as count
                    from audio_asset_lifecycle_tombstones
                    where lifecycle_job_id = %s
                    """,
                    (queued.id,),
                ).fetchone()["count"]
        assert unchanged == {
            "state": "quarantine",
            "bucket_name": "audio-quarantine",
        }
        assert tombstone_count == 0

        completed, tombstone, asset = repository.complete_lifecycle(
            queued.id,
            claim_token=claimed[0].claim_token,
            storage_outcome=AudioStorageOutcome.promoted,
            destination_key=KEY,
        )
        assert completed.status.value == "completed"
        assert tombstone.storage_outcome is AudioStorageOutcome.promoted
        assert asset.state.value == "approved"
        assert asset.bucket_name.value == "audio-originals"

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select j.status, a.state, a.bucket_name, a.object_key,
                           t.storage_outcome, t.checksum_sha256, t.size_bytes
                    from audio_asset_lifecycle_jobs j
                    join audio_assets a on a.id = j.audio_asset_id
                    join audio_asset_lifecycle_tombstones t
                      on t.lifecycle_job_id = j.id
                    where j.id = %s
                    """,
                    (queued.id,),
                ).fetchone()
        assert row["status"] == "completed"
        assert row["state"] == "approved"
        assert row["bucket_name"] == "audio-originals"
        assert row["object_key"] == KEY
        assert row["storage_outcome"] == "promoted"
        assert row["checksum_sha256"] == CHECKSUM
        assert row["size_bytes"] == 23
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                if job_id is not None:
                    cursor.execute(
                        """
                        delete from audio_asset_lifecycle_tombstones
                        where lifecycle_job_id = %s
                        """,
                        (job_id,),
                    )
                    cursor.execute(
                        "delete from audio_asset_lifecycle_jobs where id = %s",
                        (job_id,),
                    )
                cursor.execute(
                    "delete from audio_assets where id = %s",
                    (ASSET_ID,),
                )
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
                    cursor.execute(
                        "delete from rights_reviews where id = %s",
                        (review_id,),
                    )
                cursor.execute(
                    "delete from set_provider_items where set_id = %s",
                    (SET_ID,),
                )
                cursor.execute(
                    "delete from provider_items where id = %s",
                    (PROVIDER_ITEM_ID,),
                )
                cursor.execute("delete from sets where id = %s", (SET_ID,))
        pool.close()
