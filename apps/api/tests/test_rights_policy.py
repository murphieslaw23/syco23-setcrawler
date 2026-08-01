import os
import re
from importlib import import_module
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.repositories.memory import InMemoryRepository
from app.schemas import (
    RightsDecisionAction,
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
    RightsReviewStatus,
)
from app.services.provider import build_provider_registry
from app.services.provider_contracts import (
    ProviderCapability,
    ProviderDescriptor,
    ProviderWorkload,
)
from app.services.provider_registry import ProviderRegistry
from app.services.rights_policy import evaluate_provider_audio_eligibility
from app.core.config import Settings


SET_ID = UUID("00000000-0000-4000-8000-000000000002")


def _evidence() -> RightsEvidenceInput:
    return RightsEvidenceInput(
        evidence_type=RightsEvidenceType.creator_attestation,
        reference_url="https://rights.example/attestations/23",
        assertions={
            "rights_holder": True,
            "allows_distribution": True,
            "allows_derivatives": True,
        },
    )


def _request() -> RightsReviewCreate:
    return RightsReviewCreate(
        set_id=SET_ID,
        provider_key="soundcloud",
        provider_external_id="sc-k-zmk",
        requested_stream=True,
        requested_download=False,
        evidence=[_evidence()],
    )


def test_builtin_providers_remain_ineligible_for_audio_acquisition() -> None:
    registry = build_provider_registry(
        Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        )
    )

    for provider_key in (
        "youtube",
        "mixcloud",
        "soundcloud",
        "archive-org",
        "audius",
        "rss",
        "ftm",
    ):
        decision = evaluate_provider_audio_eligibility(
            registry,
            provider_key=provider_key,
            evidence=(_evidence(),),
        )
        assert decision.eligible is False
        assert decision.reason == "provider_audio_capability_missing"


def test_incomplete_evidence_is_denied_before_any_acquisition_contract() -> None:
    incomplete = _evidence().model_copy(
        update={"assertions": {"rights_holder": True}}
    )
    descriptor = ProviderDescriptor(
        key="rights-fixture",
        display_name="Rights Fixture",
        capabilities=frozenset({ProviderCapability.authorized_audio}),
        workload_by_capability={
            ProviderCapability.authorized_audio: ProviderWorkload.audio,
        },
        adapter_factory=object,
        url_matchers=(re.compile(r"^https://rights\.example/"),),
    )

    decision = evaluate_provider_audio_eligibility(
        ProviderRegistry(
            {descriptor.key: descriptor},
            {descriptor.key: object()},
        ),
        provider_key=descriptor.key,
        evidence=(incomplete,),
    )

    assert decision.eligible is False
    assert decision.reason == "rights_evidence_incomplete"
    assert not hasattr(decision, "object_key")
    assert not hasattr(decision, "media_bytes")


def test_memory_rights_approval_keeps_stream_and_download_independent() -> None:
    repository = InMemoryRepository.seeded()
    review = repository.create_rights_review(_request(), actor="admin-23")

    assert review.status is RightsReviewStatus.pending
    assert review.allow_stream is False
    assert review.allow_download is False

    approved = repository.approve_rights_review(
        review.id,
        actor="admin-23",
        allow_stream=True,
        allow_download=False,
        reason="Creator attestation verified",
    )

    assert approved is not None
    assert approved.status is RightsReviewStatus.approved
    assert approved.allow_stream is True
    assert approved.allow_download is False
    assert repository.approve_rights_review(
        review.id,
        actor="admin-23",
        allow_stream=True,
        allow_download=False,
        reason="Creator attestation verified",
    ) == approved
    events = repository.list_rights_decisions(review.id)
    assert [item.action for item in events] == [RightsDecisionAction.approve]
    assert events[0].before_state["status"] == "pending"
    assert events[0].after_state["allow_stream"] is True
    assert events[0].after_state["allow_download"] is False


def test_review_cannot_grant_more_than_was_requested() -> None:
    repository = InMemoryRepository.seeded()
    review = repository.create_rights_review(_request(), actor="admin-23")

    try:
        repository.approve_rights_review(
            review.id,
            actor="admin-23",
            allow_stream=True,
            allow_download=True,
            reason="Too broad",
        )
    except ValueError as error:
        assert str(error) == "rights approval exceeds requested permissions"
    else:
        raise AssertionError("download permission exceeded the request")


def test_approved_review_can_expire_and_revoke_both_permissions() -> None:
    repository = InMemoryRepository.seeded()
    review = repository.create_rights_review(_request(), actor="admin-23")
    approved = repository.approve_rights_review(
        review.id,
        actor="admin-23",
        allow_stream=True,
        allow_download=False,
        reason="Creator attestation verified",
    )
    assert approved is not None

    expired = repository.expire_rights_review(
        review.id,
        actor="admin-23",
        reason="Evidence validity window ended",
    )

    assert expired is not None
    assert expired.status is RightsReviewStatus.expired
    assert expired.allow_stream is False
    assert expired.allow_download is False
    assert [
        item.action for item in repository.list_rights_decisions(review.id)
    ] == [RightsDecisionAction.approve, RightsDecisionAction.expire]


def test_approval_must_grant_at_least_one_requested_permission() -> None:
    repository = InMemoryRepository.seeded()
    review = repository.create_rights_review(_request(), actor="admin-23")

    with pytest.raises(
        ValueError,
        match="rights approval must grant a requested permission",
    ):
        repository.approve_rights_review(
            review.id,
            actor="admin-23",
            allow_stream=False,
            allow_download=False,
            reason="No permission granted",
        )


def test_rights_review_api_is_admin_only_and_hides_storage_identity(
    client_as_viewer: TestClient,
    client_as_editor: TestClient,
    client_as_admin: TestClient,
) -> None:
    payload = _request().model_dump(mode="json")
    assert client_as_viewer.post("/rights-reviews", json=payload).status_code == 403
    assert client_as_editor.post("/rights-reviews", json=payload).status_code == 403

    created = client_as_admin.post("/rights-reviews", json=payload)
    assert created.status_code == 201
    body = created.json()
    serialized = str(body).casefold()
    assert "object_key" not in serialized
    assert "bucket" not in serialized
    assert "minio" not in serialized

    approved = client_as_admin.post(
        f"/rights-reviews/{body['id']}/approve",
        json={
            "allow_stream": True,
            "allow_download": False,
            "reason": "Creator attestation verified",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["allow_stream"] is True
    assert approved.json()["allow_download"] is False


def test_rights_migration_is_private_audited_and_acquisition_free() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260802010000_rights_policy_foundation.sql"
    ).read_text().casefold()

    for table in (
        "rights_reviews",
        "rights_evidence",
        "audio_permissions",
        "audio_assets",
        "audio_versions",
        "rights_review_events",
    ):
        assert f"create table public.{table}" in migration
        assert f"alter table public.{table} enable row level security" in migration
    assert "prevent_rights_event_mutation" in migration
    assert "before update or delete on public.rights_review_events" in migration
    assert "object_key" in migration
    assert "unique (object_key)" in migration
    assert "public_url" not in migration
    assert "media_bytes" not in migration


def test_runtime_has_no_audio_worker_or_route() -> None:
    from app.workers.celery_app import celery_app

    assert all("audio" not in module for module in celery_app.conf.imports)
    assert "audio" not in str(celery_app.conf.task_routes)


def test_postgres_repository_implements_rights_contract() -> None:
    from app.repositories.postgres import PostgresRepository

    for method_name in (
        "create_rights_review",
        "get_rights_review",
        "list_rights_reviews",
        "approve_rights_review",
        "reject_rights_review",
        "expire_rights_review",
        "list_rights_decisions",
    ):
        assert callable(getattr(PostgresRepository, method_name, None))


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for rights transaction verification",
)
def test_postgres_rights_approval_is_audited_without_creating_audio_assets() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = postgres.PostgresRepository(pool)
    set_id = UUID("00000000-0000-4000-8000-000000009601")
    provider_item_id = UUID("00000000-0000-4000-8000-000000009602")
    review_id = None
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'rights-source',
                  'https://soundcloud.com/fixture/rights-source',
                  'Rights Fixture Set', 3600,
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
                  'rights-source',
                  'https://soundcloud.com/fixture/rights-source',
                  'Rights Fixture Set'
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
        created = repository.create_rights_review(
            _request().model_copy(
                update={
                    "set_id": set_id,
                    "provider_external_id": "rights-source",
                }
            ),
            actor="integration-admin",
        )
        review_id = created.id

        approved = repository.approve_rights_review(
            created.id,
            actor="integration-admin",
            allow_stream=True,
            allow_download=False,
            reason="Creator attestation verified",
        )

        assert approved is not None
        assert approved.status is RightsReviewStatus.approved
        assert approved.allow_stream is True
        assert approved.allow_download is False
        assert repository.approve_rights_review(
            created.id,
            actor="integration-admin",
            allow_stream=True,
            allow_download=False,
            reason="Creator attestation verified",
        ) == approved
        assert [
            item.action for item in repository.list_rights_decisions(created.id)
        ] == [RightsDecisionAction.approve]
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                assert cursor.execute(
                    "select count(*) as count from audio_assets"
                ).fetchone()["count"] == 0
                permission = cursor.execute(
                    """
                    select allow_stream, allow_download
                    from audio_permissions where rights_review_id = %s
                    """,
                    (created.id,),
                ).fetchone()
        assert permission == {
            "allow_stream": True,
            "allow_download": False,
        }
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
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
                    (set_id,),
                )
                cursor.execute(
                    "delete from provider_items where id = %s",
                    (provider_item_id,),
                )
                cursor.execute("delete from sets where id = %s", (set_id,))
        pool.close()
