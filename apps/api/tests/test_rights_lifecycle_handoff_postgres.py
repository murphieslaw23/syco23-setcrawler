from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest

from app.schemas.rights import (
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
    RightsReviewStatus,
)


NOW = datetime(2026, 8, 8, 1, 30, tzinfo=UTC)
CHECKSUM = "d" * 64


def _ids(offset: int) -> tuple[UUID, UUID, UUID]:
    return (
        UUID(f"00000000-0000-4000-8000-{offset + 1:012d}"),
        UUID(f"00000000-0000-4000-8000-{offset + 2:012d}"),
        UUID(f"00000000-0000-4000-8000-{offset + 3:012d}"),
    )


def _create_fixture(pool: object, repository: object, *, offset: int):
    set_id, provider_item_id, asset_id = _ids(offset)
    external_id = f"rights-lifecycle-{offset}"
    object_key = f"objects/dd/{offset:032d}"

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', %s, %s,
                  'Rights Lifecycle Fixture', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (
                    set_id,
                    external_id,
                    f"https://soundcloud.com/fixture/{external_id}",
                ),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  %s, %s, 'Rights Lifecycle Fixture'
                )
                """,
                (
                    provider_item_id,
                    external_id,
                    f"https://soundcloud.com/fixture/{external_id}",
                ),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values (%s, %s, 'source', true)
                """,
                (set_id, provider_item_id),
            )

    review = repository.create_rights_review(
        RightsReviewCreate(
            set_id=set_id,
            provider_key="soundcloud",
            provider_external_id=external_id,
            requested_stream=True,
            requested_download=True,
            evidence=[
                RightsEvidenceInput(
                    evidence_type=RightsEvidenceType.creator_attestation,
                    reference_url=f"https://rights.example/handoff/{offset}",
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
                  %s, 23, 'audio/mpeg', 'quarantine',
                  %s, %s, %s
                )
                """,
                (
                    asset_id,
                    review.id,
                    object_key,
                    CHECKSUM,
                    NOW + timedelta(days=30),
                    NOW,
                    NOW,
                ),
            )
    return set_id, provider_item_id, asset_id, review


def _cleanup(pool: object, *, set_id: UUID, provider_item_id: UUID, review_id: UUID) -> None:
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("set local session_replication_role = replica")
            cursor.execute(
                """
                delete from audio_asset_lifecycle_tombstones
                where audio_asset_id in (
                  select id from audio_assets where rights_review_id = %s
                )
                """,
                (review_id,),
            )
            cursor.execute(
                """
                delete from audio_asset_lifecycle_jobs
                where audio_asset_id in (
                  select id from audio_assets where rights_review_id = %s
                )
                """,
                (review_id,),
            )
            cursor.execute(
                "delete from audio_assets where rights_review_id = %s",
                (review_id,),
            )
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
            cursor.execute(
                "delete from provider_items where id = %s",
                (provider_item_id,),
            )
            cursor.execute("delete from sets where id = %s", (set_id,))


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for rights lifecycle handoff verification",
)
@pytest.mark.parametrize(
    ("decision", "expected_action", "offset"),
    (("approve", "approve", 97100), ("reject", "reject", 97200)),
)
def test_rights_decision_atomically_enqueues_one_matching_lifecycle_job(
    decision: str,
    expected_action: str,
    offset: int,
) -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = postgres.PostgresRepository(pool)
    set_id = provider_item_id = review_id = None

    try:
        set_id, provider_item_id, asset_id, review = _create_fixture(
            pool,
            repository,
            offset=offset,
        )
        review_id = review.id
        reason = f"{decision} verified quarantine asset"
        if decision == "approve":
            result = repository.approve_rights_review(
                review.id,
                actor="integration-admin",
                allow_stream=True,
                allow_download=True,
                reason=reason,
            )
            assert result is not None
            assert result.status is RightsReviewStatus.approved
        else:
            result = repository.reject_rights_review(
                review.id,
                actor="integration-admin",
                reason=reason,
            )
            assert result is not None
            assert result.status is RightsReviewStatus.rejected

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                jobs = cursor.execute(
                    """
                    select audio_asset_id, action, status, actor, reason
                    from audio_asset_lifecycle_jobs
                    where audio_asset_id = %s
                    order by created_at, id
                    """,
                    (asset_id,),
                ).fetchall()
        assert jobs == [
            {
                "audio_asset_id": asset_id,
                "action": expected_action,
                "status": "queued",
                "actor": "integration-admin",
                "reason": reason,
            }
        ]

        if decision == "approve":
            replay = repository.approve_rights_review(
                review.id,
                actor="integration-admin",
                allow_stream=True,
                allow_download=True,
                reason=reason,
            )
        else:
            replay = repository.reject_rights_review(
                review.id,
                actor="integration-admin",
                reason=reason,
            )
        assert replay == result

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                counts = cursor.execute(
                    """
                    select
                      (select count(*) from audio_asset_lifecycle_jobs
                       where audio_asset_id = %s) as jobs,
                      (select count(*) from rights_review_events
                       where rights_review_id = %s) as events
                    """,
                    (asset_id, review.id),
                ).fetchone()
        assert counts == {"jobs": 1, "events": 1}
    finally:
        if set_id is not None and provider_item_id is not None and review_id is not None:
            _cleanup(
                pool,
                set_id=set_id,
                provider_item_id=provider_item_id,
                review_id=review_id,
            )
        pool.close()


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for rights lifecycle handoff verification",
)
def test_lifecycle_enqueue_failure_rolls_back_rights_approval_and_permission() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = postgres.PostgresRepository(pool)
    set_id = provider_item_id = review_id = None
    trigger_name = "reject_rights_lifecycle_handoff_fixture"
    function_name = "reject_rights_lifecycle_handoff_fixture"

    try:
        set_id, provider_item_id, asset_id, review = _create_fixture(
            pool,
            repository,
            offset=97300,
        )
        review_id = review.id
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    create or replace function public.{function_name}()
                    returns trigger
                    language plpgsql
                    as $$
                    begin
                      if new.audio_asset_id = '{asset_id}'::uuid then
                        raise exception 'forced lifecycle handoff failure';
                      end if;
                      return new;
                    end
                    $$
                    """
                )
                cursor.execute(
                    f"""
                    create trigger {trigger_name}
                    before insert on public.audio_asset_lifecycle_jobs
                    for each row execute function public.{function_name}()
                    """
                )

        with pytest.raises(Exception, match="forced lifecycle handoff failure"):
            repository.approve_rights_review(
                review.id,
                actor="integration-admin",
                allow_stream=True,
                allow_download=True,
                reason="must roll back atomically",
            )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                state = cursor.execute(
                    """
                    select status, allow_stream, allow_download,
                           decided_by, decision_reason, decided_at
                    from rights_reviews where id = %s
                    """,
                    (review.id,),
                ).fetchone()
                counts = cursor.execute(
                    """
                    select
                      (select count(*) from audio_permissions
                       where rights_review_id = %s) as permissions,
                      (select count(*) from rights_review_events
                       where rights_review_id = %s) as events,
                      (select count(*) from audio_asset_lifecycle_jobs
                       where audio_asset_id = %s) as jobs
                    """,
                    (review.id, review.id, asset_id),
                ).fetchone()
        assert state == {
            "status": "pending",
            "allow_stream": False,
            "allow_download": False,
            "decided_by": None,
            "decision_reason": None,
            "decided_at": None,
        }
        assert counts == {"permissions": 0, "events": 0, "jobs": 0}
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"drop trigger if exists {trigger_name} on public.audio_asset_lifecycle_jobs")
                cursor.execute(f"drop function if exists public.{function_name}()")
        if set_id is not None and provider_item_id is not None and review_id is not None:
            _cleanup(
                pool,
                set_id=set_id,
                provider_item_id=provider_item_id,
                review_id=review_id,
            )
        pool.close()


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for rights lifecycle handoff verification",
)
def test_rights_approval_without_quarantine_asset_remains_valid_and_queues_nothing() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = postgres.PostgresRepository(pool)
    set_id, provider_item_id, _ = _ids(97400)
    review_id = None
    external_id = "rights-lifecycle-no-asset"

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', %s, %s,
                  'Rights Lifecycle No Asset', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (
                    set_id,
                    external_id,
                    f"https://soundcloud.com/fixture/{external_id}",
                ),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  %s, %s, 'Rights Lifecycle No Asset'
                )
                """,
                (
                    provider_item_id,
                    external_id,
                    f"https://soundcloud.com/fixture/{external_id}",
                ),
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
        review = repository.create_rights_review(
            RightsReviewCreate(
                set_id=set_id,
                provider_key="soundcloud",
                provider_external_id=external_id,
                requested_stream=True,
                requested_download=False,
                evidence=[
                    RightsEvidenceInput(
                        evidence_type=RightsEvidenceType.creator_attestation,
                        reference_url="https://rights.example/handoff/no-asset",
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
        approved = repository.approve_rights_review(
            review.id,
            actor="integration-admin",
            allow_stream=True,
            allow_download=False,
            reason="metadata-only approval remains valid",
        )
        assert approved is not None
        assert approved.status is RightsReviewStatus.approved
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                assert cursor.execute(
                    """
                    select count(*) as count
                    from audio_asset_lifecycle_jobs jobs
                    join audio_assets assets on assets.id = jobs.audio_asset_id
                    where assets.rights_review_id = %s
                    """,
                    (review.id,),
                ).fetchone()["count"] == 0
    finally:
        if review_id is not None:
            _cleanup(
                pool,
                set_id=set_id,
                provider_item_id=provider_item_id,
                review_id=review_id,
            )
        pool.close()
