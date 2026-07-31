from __future__ import annotations

import os
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.repositories.memory import InMemoryRepository
from app.schemas import JobStatus, JobType, SetSource
from app.services.heuristic import ScoreResult
from app.services.normalizer import RawSetPayload
from app.services.provider_sources import (
    SourceIntegrityError,
    legacy_source_to_provider_key,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _score() -> ScoreResult:
    return ScoreResult(
        score=0.81,
        accepted=True,
        auto_accept=True,
        reasons=["duration:0.6", "strong:liveset"],
    )


def _payload(source: SetSource, suffix: str) -> RawSetPayload:
    if source is SetSource.youtube:
        canonical_url = f"https://www.youtube.com/watch?v={suffix}"
    elif source is SetSource.soundcloud:
        canonical_url = f"https://soundcloud.com/syco23/{suffix}"
    else:
        canonical_url = f"https://freeteknomusic.org/sets/{suffix}"
    return RawSetPayload(
        source=source,
        source_id=suffix,
        canonical_url=canonical_url,
        title=f"SYCO23 LIVESET {suffix}",
        description="Metadata-only provider import",
        duration_seconds=4_200,
        published_at=datetime(2026, 7, 31, tzinfo=UTC),
        raw_payload={
            "channel": "SYCO23",
            "local_path": "/tmp/forbidden.mp3",
            "nested": {
                "media_bytes": b"forbidden",
                "license": "metadata-only",
            },
            "filesystem": Path("/tmp/forbidden.mp3"),
        },
    )


def _persist(repository, payload: RawSetPayload):
    job = repository.create_job(
        url=payload.canonical_url,
        source=payload.source,
        job_type=JobType.url_import,
    )
    claim = repository.claim_job(job.id)
    assert claim is not None
    assert claim.started_at is not None
    set_id = repository.persist_processed_set(
        payload=payload,
        score=_score(),
        candidates=[],
        job_id=job.id,
        fingerprint=f"fingerprint-{payload.source_id}",
        claim_started_at=claim.started_at,
    )
    return job.id, set_id


def test_legacy_source_mapping_is_explicit_and_total() -> None:
    assert {
        source: legacy_source_to_provider_key(source)
        for source in SetSource
    } == {
        SetSource.youtube: "youtube",
        SetSource.soundcloud: "soundcloud",
        SetSource.freeteknomusic: "ftm",
    }


def test_memory_repository_dual_writes_and_preserves_api_shape() -> None:
    repository = InMemoryRepository()
    payload = _payload(SetSource.soundcloud, f"memory-{uuid4().hex}")

    job_id, set_id = _persist(repository, payload)

    assert set_id is not None
    detail = repository.get_set(set_id)
    assert detail is not None
    assert detail.source is SetSource.soundcloud
    assert detail.source_id == payload.source_id
    assert set(detail.model_dump()) == {
        "id",
        "source",
        "source_id",
        "canonical_url",
        "title",
        "duration_seconds",
        "published_at",
        "set_score",
        "review_status",
        "artist_names",
        "event_name",
        "city",
        "primary_image_url",
        "score_reasons",
        "import_job_id",
        "duplicate_of_id",
        "description",
        "venue",
        "year",
        "raw_payload",
        "candidates",
        "images",
        "created_at",
        "updated_at",
    }
    projection = repository._provider_sources[set_id]
    assert projection.provider_key == "soundcloud"
    assert projection.external_id == payload.source_id
    assert projection.is_primary is True
    assert projection.raw_metadata == {
        "channel": "SYCO23",
        "nested": {"license": "metadata-only"},
    }
    assert repository.get_job(job_id).status is JobStatus.completed


def test_memory_repository_rejects_projection_mismatch() -> None:
    repository = InMemoryRepository.seeded()
    set_id = next(iter(repository.sets))
    projection = repository._provider_sources[set_id]
    repository._provider_sources[set_id] = projection.model_copy(
        update={"external_id": "corrupted-source-id"}
    )

    with pytest.raises(SourceIntegrityError, match="source projection mismatch"):
        repository.get_set(set_id)
    with pytest.raises(SourceIntegrityError, match="source projection mismatch"):
        repository.list_sets(
            source=None,
            status=None,
            min_score=None,
            search=None,
            limit=20,
            offset=0,
        )


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL dual-write verification",
)
def test_postgres_repository_dual_write_mismatch_and_rollback() -> None:
    assert TEST_DATABASE_URL is not None
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(TEST_DATABASE_URL)
    pool.open()
    repository = postgres.PostgresRepository(pool)
    suffix = uuid4().hex
    payload = _payload(SetSource.youtube, f"dual-{suffix}")
    rollback_payload = _payload(SetSource.soundcloud, f"rollback-{suffix}")
    job_ids: list[UUID] = []
    set_ids: list[UUID] = []
    trigger_name = f"reject_source_link_{suffix[:12]}"
    function_name = f"reject_source_link_{suffix[:12]}"

    try:
        job_id, set_id = _persist(repository, payload)
        job_ids.append(job_id)
        assert set_id is not None
        set_ids.append(set_id)

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                linked = cursor.execute(
                    """
                    select
                        providers.key,
                        provider_items.external_id,
                        provider_items.raw_metadata,
                        links.is_primary
                    from public.set_provider_items as links
                    join public.provider_items
                      on provider_items.id = links.provider_item_id
                    join public.providers
                      on providers.id = provider_items.provider_id
                    where links.set_id = %s
                      and links.relationship = 'source'
                    """,
                    (set_id,),
                ).fetchone()
                assert linked == {
                    "key": "youtube",
                    "external_id": payload.source_id,
                    "raw_metadata": {
                        "channel": "SYCO23",
                        "nested": {"license": "metadata-only"},
                    },
                    "is_primary": True,
                }

                cursor.execute(
                    """
                    update public.provider_items
                    set external_id = 'corrupted-source-id'
                    where id = (
                        select provider_item_id
                        from public.set_provider_items
                        where set_id = %s and is_primary
                    )
                    """,
                    (set_id,),
                )

        with pytest.raises(SourceIntegrityError, match="source projection mismatch"):
            repository.get_set(set_id)

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update public.provider_items
                    set external_id = %s
                    where id = (
                        select provider_item_id
                        from public.set_provider_items
                        where set_id = %s and is_primary
                    )
                    """,
                    (payload.source_id, set_id),
                )
                cursor.execute(
                    f"""
                    create function public.{function_name}()
                    returns trigger language plpgsql as $$
                    begin
                      raise exception 'test source-link failure';
                    end
                    $$
                    """
                )
                cursor.execute(
                    f"""
                    create trigger {trigger_name}
                    before insert on public.set_provider_items
                    for each row execute function public.{function_name}()
                    """
                )

        rollback_job = repository.create_job(
            url=rollback_payload.canonical_url,
            source=rollback_payload.source,
            job_type=JobType.url_import,
        )
        job_ids.append(rollback_job.id)
        rollback_claim = repository.claim_job(rollback_job.id)
        assert rollback_claim is not None
        assert rollback_claim.started_at is not None
        with pytest.raises(Exception, match="test source-link failure"):
            repository.persist_processed_set(
                payload=rollback_payload,
                score=_score(),
                candidates=[],
                job_id=rollback_job.id,
                fingerprint=f"fingerprint-{rollback_payload.source_id}",
                claim_started_at=rollback_claim.started_at,
            )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                assert cursor.execute(
                    "select count(*) from public.sets where source_id = %s",
                    (rollback_payload.source_id,),
                ).fetchone()[0] == 0
                assert cursor.execute(
                    """
                    select count(*)
                    from public.provider_items
                    where external_id = %s
                    """,
                    (rollback_payload.source_id,),
                ).fetchone()[0] == 0
                state = cursor.execute(
                    "select status, result_set_id from import_jobs where id = %s",
                    (rollback_job.id,),
                ).fetchone()
                assert state["status"] == "processing"
                assert state["result_set_id"] is None
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"drop trigger if exists {trigger_name} on public.set_provider_items")
                cursor.execute(f"drop function if exists public.{function_name}()")
                if set_ids:
                    cursor.execute("delete from public.sets where id = any(%s)", (set_ids,))
                if job_ids:
                    cursor.execute("delete from public.import_jobs where id = any(%s)", (job_ids,))
                cursor.execute(
                    """
                    delete from public.provider_items
                    where external_id in (%s, %s)
                    """,
                    (payload.source_id, rollback_payload.source_id),
                )
        pool.close()
