import os
import re
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest

from app.repositories.memory import InMemoryRepository
from app.schemas import JobStatus, SearchProfileCreate
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    DiscoveryPage,
    ProviderItemPayload,
    ProviderCapability,
    ProviderDescriptor,
    ProviderWorkload,
)
from app.services.provider_registry import ProviderRegistry


def _provider_item() -> ProviderItemPayload:
    return ProviderItemPayload(
        provider_key="archive-org",
        external_id="warehouse-set-23",
        canonical_url="https://archive.org/details/warehouse-set-23",
        title="Warehouse Set 23",
        creator_name="DJ Fixture",
        embed_url="https://archive.org/embed/warehouse-set-23",
        artwork_candidates=("https://archive.org/services/img/warehouse-set-23",),
        download_candidates=(
            AuthorizedAudioCandidate(
                provider_key="archive-org",
                external_id="warehouse-set-23:original.mp3",
                source_url=(
                    "https://archive.org/download/warehouse-set-23/original.mp3"
                ),
                evidence_references=(
                    "https://creativecommons.org/licenses/by/4.0/",
                ),
                evidence={"policy": "reference_only_no_fetch"},
            ),
        ),
        raw_metadata={"identifier": "warehouse-set-23"},
        provenance={"source": "archive_org_metadata_api"},
        license_evidence={
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        },
    )


def _claimed_profile_job(repository: InMemoryRepository):
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Archive search",
            query="warehouse set",
            source="archive-org",
            parameters={"query": "warehouse set"},
        )
    )
    queued = repository.queue_profile_with_creation(profile.id)
    assert queued is not None
    job, created = queued
    assert created is True
    claimed = repository.claim_job(job.id)
    assert claimed is not None and claimed.started_at is not None
    return profile, claimed


def test_memory_repository_atomically_persists_provider_discovery() -> None:
    repository = InMemoryRepository()
    profile, claimed = _claimed_profile_job(repository)
    item = _provider_item()

    completed = repository.complete_provider_discovery(
        claimed.id,
        claimed.started_at,
        provider_key="archive-org",
        items=(item,),
        next_cursor="25",
    )

    assert completed is not None
    assert completed.status is JobStatus.completed
    assert completed.details["outcome"] == "provider_metadata_persisted"
    assert completed.details["provider_item_count"] == 1
    assert completed.details["provider_external_ids"] == ["warehouse-set-23"]
    assert repository.get_provider_item(
        "archive-org", "warehouse-set-23"
    ) == item
    updated_profile = repository.get_profile(profile.id)
    assert updated_profile is not None
    assert updated_profile.next_page_token == "25"
    assert updated_profile.last_run_at is not None


def test_provider_discovery_rejects_stale_ownership_without_writes() -> None:
    repository = InMemoryRepository()
    _, claimed = _claimed_profile_job(repository)

    result = repository.complete_provider_discovery(
        claimed.id,
        claimed.started_at + timedelta(seconds=1),
        provider_key="archive-org",
        items=(_provider_item(),),
        next_cursor=None,
    )

    assert result is None
    assert repository.get_provider_item("archive-org", "warehouse-set-23") is None


def test_provider_discovery_rejects_cross_provider_page() -> None:
    repository = InMemoryRepository()
    _, claimed = _claimed_profile_job(repository)
    wrong = _provider_item().model_copy(update={"provider_key": "audius"})

    try:
        repository.complete_provider_discovery(
            claimed.id,
            claimed.started_at,
            provider_key="archive-org",
            items=(wrong,),
            next_cursor=None,
        )
    except ValueError as error:
        assert str(error) == "provider discovery item mismatch"
    else:
        raise AssertionError("cross-provider page was accepted")


def test_provider_runtime_migration_is_reference_only_and_registers_launch_rows() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260801210000_provider_discovery_runtime.sql"
    ).read_text().casefold()

    for provider in ("archive-org", "mixcloud", "audius", "rss"):
        assert f"'{provider}'" in migration
    for column in (
        "creator_name",
        "artwork_candidates",
        "download_candidates",
        "provenance",
        "license_evidence",
    ):
        assert f"add column if not exists {column}" in migration
    assert "media_bytes" not in migration
    assert "local_path" not in migration
    assert "audio_objects" not in migration


def test_generic_worker_discovers_and_persists_provider_metadata(monkeypatch) -> None:
    from app.core.config import Settings
    from app.workers import provider_discovery

    repository = InMemoryRepository()
    _, job = _claimed_profile_job(repository)
    repository.jobs[job.id] = job.model_copy(
        update={
            "status": JobStatus.queued,
            "started_at": None,
            "attempt_count": 0,
        }
    )

    class Adapter:
        async def discover(self, request):
            assert request.operation == "search"
            assert request.parameters == {"query": "warehouse set"}
            return DiscoveryPage(items=(_provider_item(),), next_cursor="25")

    descriptor = ProviderDescriptor(
        key="archive-org",
        display_name="Internet Archive",
        capabilities=frozenset({ProviderCapability.discovery}),
        workload_by_capability={
            ProviderCapability.discovery: ProviderWorkload.provider_api,
        },
        task_by_capability={
            ProviderCapability.discovery: (
                "app.workers.provider_discovery.discover_profile"
            ),
        },
        adapter_factory=Adapter,
        url_matchers=(re.compile(r"^https://archive\.org/details/"),),
        discovery_operations={"search": frozenset({"query"})},
    )
    registry = ProviderRegistry.build((descriptor,))
    monkeypatch.setattr(
        provider_discovery,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        provider_discovery,
        "get_provider_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        provider_discovery,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
    )

    result = provider_discovery.discover_profile.run(str(job.id))

    assert result == {"provider_item_count": 1}
    completed = repository.get_job(job.id)
    assert completed is not None
    assert completed.status is JobStatus.completed
    assert repository.get_provider_item(
        "archive-org", "warehouse-set-23"
    ) is not None


def test_celery_imports_generic_provider_worker_without_audio_routes() -> None:
    from app.workers.celery_app import celery_app

    assert "app.workers.provider_discovery" in celery_app.conf.imports
    routes = str(celery_app.conf.task_routes)
    assert "provider_discovery" not in routes
    assert "audio" not in routes


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for provider discovery persistence",
)
def test_postgres_atomically_persists_provider_discovery() -> None:
    database = import_module("app.core.database")
    postgres = import_module("app.repositories.postgres")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = postgres.PostgresRepository(pool)
    suffix = uuid4().hex
    external_id = f"runtime-{suffix}"
    profile_id = None
    job_id = None
    try:
        profile = repository.create_profile(
            SearchProfileCreate(
                name="Runtime archive search",
                query="warehouse set",
                source="archive-org",
                parameters={"query": "warehouse set"},
            )
        )
        profile_id = profile.id
        queued = repository.queue_profile_with_creation(profile.id)
        assert queued is not None
        job, created = queued
        job_id = job.id
        assert created is True
        claimed = repository.claim_job(job.id)
        assert claimed is not None and claimed.started_at is not None
        item = _provider_item().model_copy(update={"external_id": external_id})

        completed = repository.complete_provider_discovery(
            claimed.id,
            claimed.started_at,
            provider_key="archive-org",
            items=(item,),
            next_cursor="50",
        )

        assert completed is not None
        assert completed.status is JobStatus.completed
        assert repository.get_provider_item("archive-org", external_id) == item
        updated_profile = repository.get_profile(profile.id)
        assert updated_profile is not None
        assert updated_profile.next_page_token == "50"
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                if job_id is not None:
                    cursor.execute("delete from import_jobs where id = %s", (job_id,))
                if profile_id is not None:
                    cursor.execute(
                        "delete from search_profiles where id = %s",
                        (profile_id,),
                    )
                cursor.execute(
                    """
                    delete from provider_items
                    where external_id = %s
                      and provider_id = (
                        select id from providers where key = 'archive-org'
                      )
                    """,
                    (external_id,),
                )
        pool.close()
