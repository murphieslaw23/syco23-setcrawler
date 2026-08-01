from datetime import UTC, datetime
import re

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
