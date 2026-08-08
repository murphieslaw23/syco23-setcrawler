from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest

from app.repositories.audio_lifecycle_postgres import (
    PostgresAudioLifecycleRepository,
)
from app.schemas.audio_lifecycle import AudioLifecycleAction
from app.schemas.rights import (
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
)


NOW = datetime(2026, 8, 2, 19, 30, tzinfo=UTC)
SET_ID = UUID("00000000-0000-4000-8000-00000000d101")
PROVIDER_ITEM_ID = UUID("00000000-0000-4000-8000-00000000d102")
EXPIRED_ASSET_ID = UUID("00000000-0000-4000-8000-00000000d103")
FUTURE_ASSET_ID = UUID("00000000-0000-4000-8000-00000000d104")
EXPIRED_KEY = "objects/dd/dddddddddddddddddddddddddddddddd"
FUTURE_KEY = "objects/ee/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
CHECKSUM = "d" * 64


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for expiry claim verification",
)
def test_postgres_only_enqueues_expired_unclaimed_quarantine_assets() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    rights_repository = postgres.PostgresRepository(pool)
    repository = PostgresAudioLifecycleRepository(pool, clock=lambda: NOW)
    review_id = None
    expiry_job_id = None

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'lifecycle-expiry-source',
                  'https://soundcloud.com/fixture/lifecycle-expiry-source',
                  'Lifecycle Expiry Fixture', 3600,
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
                  'lifecycle-expiry-source',
                  'https://soundcloud.com/fixture/lifecycle-expiry-source',
                  'Lifecycle Expiry Fixture'
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
                provider_external_id="lifecycle-expiry-source",
                requested_stream=True,
                requested_download=True,
                evidence=[
                    RightsEvidenceInput(
                        evidence_type=RightsEvidenceType.provider_permission,
                        reference_url="https://rights.example/lifecycle-expiry/23",
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
            reason="expiry fixture approved",
        )
        assert approved is not None

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
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
                        (
                            EXPIRED_ASSET_ID,
                            review.id,
                            EXPIRED_KEY,
                            CHECKSUM,
                            NOW - timedelta(seconds=1),
                            NOW,
                            NOW,
                        ),
                        (
                            FUTURE_ASSET_ID,
                            review.id,
                            FUTURE_KEY,
                            CHECKSUM,
                            NOW + timedelta(days=30),
                            NOW,
                            NOW,
                        ),
                    ),
                )

        queued = repository.enqueue_expired_assets(
            limit=10,
            actor="system-expiry",
            reason="quarantine retention expired",
            now=NOW,
        )
        assert len(queued) == 1
        assert queued[0].audio_asset_id == EXPIRED_ASSET_ID
        assert queued[0].action is AudioLifecycleAction.expire
        expiry_job_id = queued[0].id

        duplicate = repository.enqueue_expired_assets(
            limit=10,
            actor="system-expiry",
            reason="quarantine retention expired",
            now=NOW,
        )
        assert duplicate == []

        claimed = repository.claim_due(limit=10, now=NOW)
        assert len(claimed) == 1
        assert claimed[0].id == expiry_job_id
        assert claimed[0].claim_token is not None

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                future_jobs = cursor.execute(
                    """
                    select count(*) as count
                    from audio_asset_lifecycle_jobs
                    where audio_asset_id = %s
                    """,
                    (FUTURE_ASSET_ID,),
                ).fetchone()["count"]
        assert future_jobs == 0
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                if expiry_job_id is not None:
                    cursor.execute(
                        "delete from audio_asset_lifecycle_jobs where id = %s",
                        (expiry_job_id,),
                    )
                cursor.execute(
                    "delete from audio_assets where id in (%s, %s)",
                    (EXPIRED_ASSET_ID, FUTURE_ASSET_ID),
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
