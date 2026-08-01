from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from threading import Barrier

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repositories.memory import InMemoryRepository
from app.schemas.profile import SearchProfile, SearchProfileCreate
from app.services.provider_contracts import (
    ProviderCapability,
    ProviderDescriptor,
    ProviderWorkload,
)
from app.services.provider_registry import ProviderRegistry
from app.workers.profile_scheduler import schedule_due_profiles
from conftest import RecordingDispatcher


class _SchedulerDispatcher:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.calls = []

    def dispatch_profile(self, job) -> None:
        assert self.repository.get_job(job.id) is not None
        self.calls.append(("profile", job))


class _DiscoveryAdapter:
    async def discover(self, request):
        return request


def test_profile_read_model_ignores_repository_only_columns() -> None:
    profile = SearchProfile(
        name="Repository row",
        query="warehouse liveset",
        deleted_at=None,
    )

    assert profile.name == "Repository row"


def _descriptor(
    *,
    key: str = "fixture",
    enabled_by_default: bool = True,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        key=key,
        display_name="Fixture Provider",
        capabilities=frozenset({ProviderCapability.discovery}),
        workload_by_capability={
            ProviderCapability.discovery: ProviderWorkload.provider_api,
        },
        task_by_capability={
            ProviderCapability.discovery: "app.workers.fixture.discover",
        },
        adapter_factory=_DiscoveryAdapter,
        url_matchers=(re.compile(r"^https://fixture\.example/items/"),),
        discovery_operations={"search": frozenset({"term"})},
        enabled_by_default=enabled_by_default,
    )


def test_health_preserves_public_fields_and_reports_registry_readiness() -> None:
    client = TestClient(
        create_app(
            InMemoryRepository.seeded(),
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
                youtube_api_key="configured-secret",
                audius_enabled=True,
                audius_api_bearer_token="audius-secret",
            ),
            dispatcher=RecordingDispatcher(),
        )
    )

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["service"] == "syco23-setcrawler-api"
    assert body["ready"] is True
    assert body["providers"]["youtube"]["display_name"] == "YouTube"
    assert body["providers"]["youtube"]["capabilities"] == [
        "discovery",
        "embed",
        "metadata",
    ]
    assert body["providers"]["youtube"]["workloads"]["discovery"] == "provider-api"
    assert "configured-secret" not in str(body)
    assert "audius-secret" not in str(body)


def test_disabled_provider_does_not_block_readiness_but_enabled_missing_does() -> None:
    disabled = TestClient(
        create_app(
            InMemoryRepository.seeded(),
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
                youtube_api_key="configured-secret",
            ),
            dispatcher=RecordingDispatcher(),
        )
    ).get("/health").json()
    enabled_missing = TestClient(
        create_app(
            InMemoryRepository.seeded(),
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
                youtube_api_key="configured-secret",
                audius_enabled=True,
            ),
            dispatcher=RecordingDispatcher(),
        )
    ).get("/health").json()

    assert disabled["ready"] is True
    assert disabled["providers"]["audius"]["reason"] == "provider_disabled"
    assert enabled_missing["ready"] is False
    assert enabled_missing["providers"]["audius"]["database_enabled"] is True


def test_fixture_provider_health_requires_registration_only() -> None:
    registry = ProviderRegistry.build((_descriptor(),))
    app = create_app(
        InMemoryRepository(),
        settings=Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
        dispatcher=RecordingDispatcher(),
    )

    with TestClient(app) as client:
        app.state.provider_registry = registry
        body = client.get("/health").json()

    assert body["providers"]["fixture"]["enabled"] is True
    assert body["providers"]["fixture"]["configuration_complete"] is True


def test_scheduler_dispatches_due_fixture_profile_from_descriptor() -> None:
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Fixture search",
            query="warehouse liveset",
            source="fixture",
            operation="search",
            parameters={"term": "warehouse liveset"},
            schedule_cron="0 6 * * *",
        )
    )
    dispatcher = _SchedulerDispatcher(repository)
    registry = ProviderRegistry.build((_descriptor(),))

    result = schedule_due_profiles(
        repository,
        dispatcher,
        registry,
        Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
        now=datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    )

    assert result == {"due": 1, "created": 1, "dispatched": 1}
    job = dispatcher.calls[0][1]
    assert job.details == {
        "provider_key": "fixture",
        "capability": "discovery",
        "operation": "search",
        "parameters": {"term": "warehouse liveset"},
        "query": "warehouse liveset",
    }
    updated = repository.get_profile(profile.id)
    assert updated is not None
    assert updated.last_scheduled_at == datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
    assert updated.next_scheduled_at == datetime(2026, 8, 2, 6, 0, tzinfo=UTC)


def test_scheduler_dispatches_enabled_archive_profile_through_generic_task() -> None:
    from app.services.provider import build_provider_registry

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Archive search",
            query="warehouse liveset",
            source="archive-org",
            operation="search",
            parameters={"query": "warehouse liveset"},
            schedule_cron="0 6 * * *",
        )
    )
    dispatcher = _SchedulerDispatcher(repository)
    settings = Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="live",
        archive_org_enabled=True,
    )

    result = schedule_due_profiles(
        repository,
        dispatcher,
        build_provider_registry(settings),
        settings,
        now=datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    )

    assert result == {"due": 1, "created": 1, "dispatched": 1}
    job = dispatcher.calls[0][1]
    assert job.details["provider_key"] == "archive-org"
    assert job.details["operation"] == "search"
    assert repository.get_profile(profile.id).last_scheduled_at is not None


def test_scheduler_runs_one_missed_occurrence_after_restart() -> None:
    repository = InMemoryRepository()
    profile = SearchProfile(
        name="Restarted search",
        query="warehouse liveset",
        source="fixture",
        operation="search",
        parameters={"term": "warehouse liveset"},
        schedule_cron="0 6 * * *",
        schedule_timezone="Europe/Berlin",
        created_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    repository.profiles[profile.id] = profile
    dispatcher = _SchedulerDispatcher(repository)

    result = schedule_due_profiles(
        repository,
        dispatcher,
        ProviderRegistry.build((_descriptor(),)),
        Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
        now=datetime(2026, 8, 1, 8, 15, tzinfo=UTC),
    )

    assert result == {"due": 1, "created": 1, "dispatched": 1}
    updated = repository.get_profile(profile.id)
    assert updated is not None
    assert updated.last_scheduled_at == datetime(2026, 8, 1, 8, 15, tzinfo=UTC)
    assert updated.next_scheduled_at == datetime(2026, 8, 2, 4, 0, tzinfo=UTC)


def test_scheduler_does_not_backfill_before_profile_creation() -> None:
    repository = InMemoryRepository()
    profile = SearchProfile(
        name="New search",
        query="warehouse liveset",
        source="fixture",
        operation="search",
        parameters={"term": "warehouse liveset"},
        schedule_cron="0 6 * * *",
        created_at=datetime(2026, 8, 1, 7, 0, tzinfo=UTC),
    )
    repository.profiles[profile.id] = profile
    dispatcher = _SchedulerDispatcher(repository)

    result = schedule_due_profiles(
        repository,
        dispatcher,
        ProviderRegistry.build((_descriptor(),)),
        Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
        now=datetime(2026, 8, 1, 8, 15, tzinfo=UTC),
    )

    assert result == {"due": 0, "created": 0, "dispatched": 0}
    assert dispatcher.calls == []


def test_duplicate_scheduler_ticks_dispatch_one_active_profile_job() -> None:
    repository = InMemoryRepository()
    profile = SearchProfile(
        name="Duplicate-safe search",
        query="warehouse liveset",
        source="fixture",
        operation="search",
        parameters={"term": "warehouse liveset"},
        schedule_cron="0 6 * * *",
        created_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    repository.profiles[profile.id] = profile
    dispatcher = _SchedulerDispatcher(repository)
    registry = ProviderRegistry.build((_descriptor(),))
    settings = Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="live",
    )
    now = datetime(2026, 8, 1, 8, 15, tzinfo=UTC)

    first = schedule_due_profiles(repository, dispatcher, registry, settings, now=now)
    second = schedule_due_profiles(repository, dispatcher, registry, settings, now=now)

    assert first == {"due": 1, "created": 1, "dispatched": 1}
    assert second == {"due": 0, "created": 0, "dispatched": 0}
    assert len(dispatcher.calls) == 1


def test_duplicate_scheduler_processes_cannot_overlap_profile_jobs() -> None:
    barrier = Barrier(2)

    class ConcurrentSchedulerRepository(InMemoryRepository):
        def list_profiles(self):
            profiles = super().list_profiles()
            barrier.wait()
            return profiles

    repository = ConcurrentSchedulerRepository()
    profile = SearchProfile(
        name="Concurrent scheduler search",
        query="warehouse liveset",
        source="fixture",
        operation="search",
        parameters={"term": "warehouse liveset"},
        schedule_cron="0 6 * * *",
        created_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    repository.profiles[profile.id] = profile
    dispatcher = _SchedulerDispatcher(repository)
    registry = ProviderRegistry.build((_descriptor(),))
    settings = Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="live",
    )

    def schedule_once():
        return schedule_due_profiles(
            repository,
            dispatcher,
            registry,
            settings,
            now=datetime(2026, 8, 1, 8, 15, tzinfo=UTC),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: schedule_once(), range(2)))

    assert sum(result["created"] for result in results) == 1
    assert sum(result["dispatched"] for result in results) == 1
    assert len(dispatcher.calls) == 1


def test_profile_api_rejects_unknown_schedule_timezone() -> None:
    repository = InMemoryRepository()
    client = TestClient(
        create_app(
            repository,
            settings=Settings(environment="fixture", repository_mode="memory"),
            dispatcher=RecordingDispatcher(),
        )
    )

    response = client.post(
        "/search-profiles",
        json={
            "name": "Invalid timezone",
            "query": "warehouse liveset",
            "schedule_timezone": "local",
        },
    )

    assert response.status_code == 422
    assert repository.list_profiles() == []


def test_scheduler_skips_disabled_provider_without_advancing_profile() -> None:
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Disabled search",
            query="warehouse liveset",
            source="fixture",
            operation="search",
            parameters={"term": "warehouse liveset"},
            schedule_cron="0 6 * * *",
        )
    )
    dispatcher = _SchedulerDispatcher(repository)

    result = schedule_due_profiles(
        repository,
        dispatcher,
        ProviderRegistry.build((_descriptor(enabled_by_default=False),)),
        Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
        now=datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    )

    assert result == {"due": 1, "created": 0, "dispatched": 0}
    assert dispatcher.calls == []
    updated = repository.get_profile(profile.id)
    assert updated is not None
    assert updated.last_scheduled_at is None


def test_profile_validation_rejects_unknown_descriptor_operation() -> None:
    repository = InMemoryRepository()
    registry = ProviderRegistry.build((_descriptor(),))
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Invalid search",
            query="warehouse liveset",
            source="fixture",
            operation="trending",
            parameters={"term": "warehouse liveset"},
        )
    )

    result = schedule_due_profiles(
        repository,
        _SchedulerDispatcher(repository),
        registry,
        Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
        now=datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    )

    assert result == {"due": 1, "created": 0, "dispatched": 0}
    assert repository.get_profile(profile.id).last_scheduled_at is None


def test_profile_api_rejects_unregistered_provider() -> None:
    client = TestClient(
        create_app(
            InMemoryRepository(),
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
            ),
            dispatcher=RecordingDispatcher(),
        )
    )

    response = client.post(
        "/search-profiles",
        json={
            "name": "Unknown provider",
            "query": "warehouse liveset",
            "source": "missing-provider",
            "operation": "search",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "provider_not_registered"


def test_profile_api_rejects_invalid_cron_before_persistence() -> None:
    repository = InMemoryRepository()
    client = TestClient(
        create_app(
            repository,
            settings=Settings(environment="fixture", repository_mode="memory"),
            dispatcher=RecordingDispatcher(),
        )
    )

    response = client.post(
        "/search-profiles",
        json={
            "name": "Invalid cron",
            "query": "warehouse liveset",
            "schedule_cron": "61 25 * * *",
        },
    )

    assert response.status_code == 422
    assert repository.list_profiles() == []


def test_profile_manual_run_rejects_effectively_disabled_provider() -> None:
    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Unconfigured YouTube",
            query="warehouse liveset",
        )
    )
    client = TestClient(
        create_app(
            repository,
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="live",
                youtube_api_key="",
            ),
            dispatcher=RecordingDispatcher(),
        )
    )

    response = client.post(f"/search-profiles/{profile.id}/run")

    assert response.status_code == 409
    assert response.json()["detail"] == "provider_configuration_missing"


def test_scheduler_migration_adds_timezone_and_safe_weekly_ftm_profile() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260801150000_scheduler_hardening.sql"
    ).read_text().casefold()

    assert "schedule_timezone text not null default 'utc'" in migration
    assert "'weekly ftm metadata crawl'" in migration
    assert "'ftm'" in migration
    assert "'crawl'" in migration
    assert "https://freeteknomusic.org/sets/23hz" in migration
    assert "'0 4 * * 1'" in migration
    assert "download" not in migration
    assert "audio" not in migration
