import os
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.repositories.memory import InMemoryRepository
from app.schemas import (
    MergeCandidateStatus,
    MergeDecisionAction,
    JobType,
    SetDetail,
    SetSource,
)
from app.services.merge_scoring import score_set_merge
from app.services.normalizer import RawSetPayload, duplicate_fingerprint
from app.services.import_pipeline import process_payload


def _set(
    value: int,
    *,
    source: SetSource,
    title: str,
    artists: list[str],
    event: str | None,
    year: int | None,
    duration: int,
    aliases: list[str] | None = None,
) -> SetDetail:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    return SetDetail(
        id=UUID(f"00000000-0000-4000-8000-{value:012d}"),
        source=source,
        source_id=f"source-{value}",
        canonical_url=f"https://provider.example/sets/{value}",
        title=title,
        duration_seconds=duration,
        published_at=(
            datetime(year, 6, 1, tzinfo=UTC) if year is not None else None
        ),
        set_score=0.8,
        review_status="inbox",
        artist_names=artists,
        event_name=event,
        year=year,
        raw_payload={"artist_aliases": aliases or []},
        created_at=now,
        updated_at=now,
    )


def test_merge_score_explains_renamed_and_shortened_sets() -> None:
    left = _set(
        101,
        source=SetSource.youtube,
        title="DJ Hyper @ South Side Teknival 2026 — Full Liveset",
        artists=["DJ Hyper"],
        aliases=["Hyper"],
        event="South Side Teknival",
        year=2026,
        duration=5_060,
    )
    right = _set(
        102,
        source=SetSource.soundcloud,
        title="Hyper - South Side live set",
        artists=["Hyper"],
        event="South Side Teknival",
        year=2026,
        duration=5_075,
    )

    result = score_set_merge(left, right)

    assert result.score >= 0.75
    assert result.components.title_artist >= 0.75
    assert result.components.event == 1
    assert result.components.date_year == 1
    assert result.components.duration >= 0.95
    assert result.components.aliases == 1
    assert result.reasons == sorted(result.reasons)


def test_merge_score_keeps_unrelated_sets_below_suggestion_threshold() -> None:
    left = _set(
        103,
        source=SetSource.youtube,
        title="Acid Assembly Warehouse Mix",
        artists=["Acid Assembly"],
        event="Warehouse Session",
        year=2026,
        duration=4_800,
    )
    right = _set(
        104,
        source=SetSource.soundcloud,
        title="Noisekraft Ground Pressure",
        artists=["Noisekraft"],
        event="Ground Pressure",
        year=2024,
        duration=2_700,
    )

    assert score_set_merge(left, right).score < 0.45


def test_cross_provider_fingerprint_creates_no_silent_duplicate() -> None:
    repository = InMemoryRepository.seeded()
    existing = repository.sets[
        UUID("00000000-0000-4000-8000-000000000001")
    ]
    fingerprint = duplicate_fingerprint(
        existing.title,
        existing.duration_seconds or 0,
    )
    existing.raw_payload["duplicate_fingerprint"] = fingerprint
    cross_provider = RawSetPayload(
        source=SetSource.soundcloud,
        source_id="same-recording-on-soundcloud",
        canonical_url=(
            "https://soundcloud.com/fixture/same-recording-on-soundcloud"
        ),
        title=existing.title,
        duration_seconds=existing.duration_seconds,
        raw_payload={},
    )

    assert repository.find_duplicate(cross_provider, fingerprint) is None
    same_url_other_provider = cross_provider.model_copy(
        update={"canonical_url": existing.canonical_url}
    )
    assert repository.find_duplicate(same_url_other_provider, fingerprint) is None

    exact = cross_provider.model_copy(
        update={
            "source": existing.source,
            "source_id": existing.source_id,
            "canonical_url": existing.canonical_url,
        }
    )
    assert repository.find_duplicate(exact, fingerprint) == existing.id


def test_pipeline_persists_cross_provider_match_as_review_suggestion() -> None:
    repository = InMemoryRepository.seeded()
    existing_id = UUID("00000000-0000-4000-8000-000000000001")
    existing = repository.get_set(existing_id)
    job = repository.create_job(
        url="https://soundcloud.com/fixture/murph-south-side",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    payload = RawSetPayload(
        source=SetSource.soundcloud,
        source_id="murph-south-side",
        canonical_url="https://soundcloud.com/fixture/murph-south-side",
        title=existing.title,
        description=existing.description,
        duration_seconds=existing.duration_seconds,
        published_at=existing.published_at,
        raw_payload={},
    )

    new_id = process_payload(repository, job.id, payload)

    assert new_id is not None and new_id != existing_id
    page = repository.list_merge_candidates(status=None, limit=50, offset=0)
    assert page.total == 1
    suggestion = page.items[0]
    assert suggestion.status is MergeCandidateStatus.pending
    assert {suggestion.source_set_id, suggestion.target_set_id} == {
        new_id,
        existing_id,
    }


def test_memory_merge_approval_retains_sources_and_is_reversible() -> None:
    repository = InMemoryRepository.seeded()
    target_id = UUID("00000000-0000-4000-8000-000000000001")
    source_id = UUID("00000000-0000-4000-8000-000000000002")
    score = score_set_merge(
        repository.get_set(source_id),
        repository.get_set(target_id),
    )
    candidate = repository.create_merge_candidate(
        source_set_id=source_id,
        target_set_id=target_id,
        score=score,
    )

    assert repository.create_merge_candidate(
        source_set_id=source_id,
        target_set_id=target_id,
        score=score,
    ).id == candidate.id

    approved = repository.approve_merge_candidate(candidate.id, actor="admin-23")

    assert approved is not None
    assert approved.status is MergeCandidateStatus.approved
    assert repository.get_set(source_id).duplicate_of_id == target_id
    assert repository.list_set_provider_sources(source_id) == []
    target_sources = repository.list_set_provider_sources(target_id)
    assert {item.provider_key for item in target_sources} == {
        "youtube",
        "soundcloud",
    }
    decisions = repository.list_merge_decisions(candidate.id)
    assert [item.action for item in decisions] == [MergeDecisionAction.approve]
    assert decisions[0].before_state["source_provider_items"]
    assert decisions[0].after_state["target_provider_items"]
    active = repository.list_sets(
        source=None,
        status=None,
        min_score=None,
        search=None,
        limit=50,
        offset=0,
    )
    assert active.total == 5
    assert repository.stats()["total_sets"] == 5

    restored = repository.restore_merge_candidate(candidate.id, actor="admin-23")

    assert restored is not None
    assert restored.status is MergeCandidateStatus.restored
    assert repository.get_set(source_id).duplicate_of_id is None
    assert len(repository.list_set_provider_sources(source_id)) == 1
    assert len(repository.list_set_provider_sources(target_id)) == 1
    assert repository.stats()["total_sets"] == 6
    assert [item.action for item in repository.list_merge_decisions(candidate.id)] == [
        MergeDecisionAction.approve,
        MergeDecisionAction.restore,
    ]


def test_reject_records_immutable_evidence_without_moving_sources() -> None:
    repository = InMemoryRepository.seeded()
    target_id = UUID("00000000-0000-4000-8000-000000000001")
    source_id = UUID("00000000-0000-4000-8000-000000000002")
    candidate = repository.create_merge_candidate(
        source_set_id=source_id,
        target_set_id=target_id,
        score=score_set_merge(
            repository.get_set(source_id),
            repository.get_set(target_id),
        ),
    )

    rejected = repository.reject_merge_candidate(candidate.id, actor="admin-23")

    assert rejected is not None
    assert rejected.status is MergeCandidateStatus.rejected
    assert len(repository.list_set_provider_sources(source_id)) == 1
    assert len(repository.list_set_provider_sources(target_id)) == 1
    assert repository.list_merge_decisions(candidate.id)[0].action is (
        MergeDecisionAction.reject
    )


def test_merge_review_api_is_admin_only(
    repository: InMemoryRepository,
    client_as_viewer: TestClient,
    client_as_editor: TestClient,
    client_as_admin: TestClient,
) -> None:
    target_id = UUID("00000000-0000-4000-8000-000000000001")
    source_id = UUID("00000000-0000-4000-8000-000000000002")
    candidate = repository.create_merge_candidate(
        source_set_id=source_id,
        target_set_id=target_id,
        score=score_set_merge(
            repository.get_set(source_id),
            repository.get_set(target_id),
        ),
    )

    assert client_as_viewer.get("/merge-candidates").status_code == 403
    assert client_as_editor.get("/merge-candidates").status_code == 403
    listing = client_as_admin.get("/merge-candidates")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == str(candidate.id)
    source_listing = client_as_admin.get(f"/sets/{source_id}/sources")
    assert source_listing.status_code == 200
    assert source_listing.json()[0]["provider_key"] == "soundcloud"
    assert "raw_metadata" not in source_listing.json()[0]

    assert client_as_editor.post(
        f"/merge-candidates/{candidate.id}/approve"
    ).status_code == 403
    approved = client_as_admin.post(
        f"/merge-candidates/{candidate.id}/approve"
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    target_listing = client_as_admin.get(f"/sets/{target_id}/sources")
    assert {item["provider_key"] for item in target_listing.json()} == {
        "youtube",
        "soundcloud",
    }
    restored = client_as_admin.post(
        f"/merge-candidates/{candidate.id}/restore"
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "restored"


def test_canonical_merge_migration_is_reversible_and_audited() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260801230000_canonical_set_merge.sql"
    ).read_text().casefold()

    for table in ("merge_candidates", "merge_decisions"):
        assert f"create table public.{table}" in migration
        assert f"alter table public.{table} enable row level security" in migration
    assert "add column if not exists merged_into_id" in migration
    assert "prevent_merge_decision_mutation" in migration
    assert "before update or delete on public.merge_decisions" in migration
    assert "delete from public.provider_items" not in migration
    assert "on delete restrict" in migration


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for canonical merge transactions",
)
def test_postgres_canonical_merge_round_trip() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = postgres.PostgresRepository(pool)
    source_id = UUID("00000000-0000-4000-8000-000000009501")
    target_id = UUID("00000000-0000-4000-8000-000000009502")
    source_item_id = UUID("00000000-0000-4000-8000-000000009503")
    target_item_id = UUID("00000000-0000-4000-8000-000000009504")
    candidate_id = None
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values
                  (%s, 'soundcloud', 'merge-source',
                   'https://soundcloud.com/fixture/merge-source',
                   'DJ Hyper South Side Teknival', 5060,
                   '2026-06-01T00:00:00Z', 0.8, 'inbox'),
                  (%s, 'youtube', 'merge-target',
                   'https://www.youtube.com/watch?v=merge-target',
                   'DJ Hyper South Side Teknival', 5075,
                   '2026-06-01T00:00:00Z', 0.8, 'accepted')
                """,
                (source_id, target_id),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values
                  (%s, (select id from providers where key = 'soundcloud'),
                   'merge-source',
                   'https://soundcloud.com/fixture/merge-source',
                   'DJ Hyper South Side Teknival'),
                  (%s, (select id from providers where key = 'youtube'),
                   'merge-target',
                   'https://www.youtube.com/watch?v=merge-target',
                   'DJ Hyper South Side Teknival')
                """,
                (source_item_id, target_item_id),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values
                  (%s, %s, 'source', true),
                  (%s, %s, 'source', true)
                """,
                (source_id, source_item_id, target_id, target_item_id),
            )
    try:
        suggestions = repository.suggest_merge_candidates(source_id)
        assert len(suggestions) == 1
        candidate = suggestions[0]
        candidate_id = candidate.id

        approved = repository.approve_merge_candidate(
            candidate.id,
            actor="integration-admin",
        )

        assert approved is not None
        assert repository.get_set(source_id).duplicate_of_id == target_id
        assert repository.list_set_provider_sources(source_id) == []
        assert len(repository.list_set_provider_sources(target_id)) == 2

        restored = repository.restore_merge_candidate(
            candidate.id,
            actor="integration-admin",
        )

        assert restored is not None
        assert repository.get_set(source_id).duplicate_of_id is None
        assert len(repository.list_set_provider_sources(source_id)) == 1
        assert len(repository.list_set_provider_sources(target_id)) == 1
        assert [
            item.action for item in repository.list_merge_decisions(candidate.id)
        ] == [MergeDecisionAction.approve, MergeDecisionAction.restore]
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                if candidate_id is not None:
                    cursor.execute(
                        "delete from merge_decisions where merge_candidate_id = %s",
                        (candidate_id,),
                    )
                    cursor.execute(
                        "delete from merge_candidates where id = %s",
                        (candidate_id,),
                    )
                cursor.execute(
                    "delete from set_provider_items where set_id in (%s, %s)",
                    (source_id, target_id),
                )
                cursor.execute(
                    "delete from provider_items where id in (%s, %s)",
                    (source_item_id, target_item_id),
                )
                cursor.execute(
                    "delete from sets where id in (%s, %s)",
                    (source_id, target_id),
                )
        pool.close()
