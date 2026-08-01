import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.observability import StructuredJsonFormatter, log_context
from app.main import create_app
from app.repositories.memory import InMemoryRepository
from app.schemas import ImportJobPatch, JobStatus, JobType, SetSource
from app.services.operational_health import (
    OperationalHealthProbe,
    enabled_worker_queues,
)
from app.services.provider import build_provider_registry
from conftest import RecordingDispatcher


def _fixture_settings(**overrides: object) -> Settings:
    return Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="live",
        **overrides,
    )


def _production_settings(**overrides: object) -> Settings:
    return Settings(
        environment="production",
        repository_mode="postgres",
        auth_mode="supabase",
        supabase_url="https://fixture.supabase.co",
        supabase_anon_key="fixture-anon-key",
        provider_mode="live",
        youtube_api_key="fixture-youtube-key",
        **overrides,
    )


def test_structured_formatter_correlates_and_redacts_secrets() -> None:
    formatter = StructuredJsonFormatter(
        service="worker-provider-api",
        environment="production",
        secret_values=("literal-provider-secret",),
    )
    record = logging.LogRecord(
        name="app.workers.youtube_poller",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "provider failed Authorization: Bearer header.payload.signature "
            "at postgresql://operator:database-password@db.example/syco23 "
            "using literal-provider-secret"
        ),
        args=(),
        exc_info=None,
    )
    record.event = "provider_failed"
    record.api_key = "field-level-secret"

    with log_context(
        request_id="request-123",
        job_id="00000000-0000-4000-8000-000000000023",
        provider="youtube",
    ):
        payload = json.loads(formatter.format(record))

    assert payload["event"] == "provider_failed"
    assert payload["request_id"] == "request-123"
    assert payload["job_id"] == "00000000-0000-4000-8000-000000000023"
    assert payload["provider"] == "youtube"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["service"] == "worker-provider-api"
    assert payload["environment"] == "production"
    serialized = json.dumps(payload)
    for secret in (
        "header.payload.signature",
        "database-password",
        "literal-provider-secret",
        "field-level-secret",
    ):
        assert secret not in serialized


def test_api_request_has_correlation_header_and_log(
    caplog,
) -> None:
    app = create_app(
        InMemoryRepository.seeded(),
        settings=_fixture_settings(youtube_api_key="configured"),
        dispatcher=RecordingDispatcher(),
    )

    with caplog.at_level(logging.INFO, logger="app.http"):
        response = TestClient(app).get(
            "/health",
            headers={"X-Request-ID": "release-check-23"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "release-check-23"
    completion = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "http_request_completed"
    )
    assert completion.request_id == "release-check-23"
    assert completion.method == "GET"
    assert completion.path == "/health"
    assert completion.status_code == 200
    assert completion.duration_ms >= 0


def test_api_dispatch_log_links_request_to_import_job(caplog) -> None:
    app = create_app(
        InMemoryRepository(),
        settings=_fixture_settings(youtube_api_key="configured"),
        dispatcher=RecordingDispatcher(),
    )

    with caplog.at_level(logging.INFO, logger="app.jobs"):
        response = TestClient(app).post(
            "/imports/url",
            headers={"X-Request-ID": "dispatch-request-23"},
            json={"url": "https://www.youtube.com/watch?v=observability"},
        )

    assert response.status_code == 202
    dispatched = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "job_dispatched"
    )
    assert dispatched.job_id == response.json()["id"]
    assert dispatched.provider == "youtube"
    assert dispatched.job_type == "url_import"


def test_worker_queue_readiness_is_derived_from_enabled_descriptors() -> None:
    without_youtube = _fixture_settings(youtube_api_key="")
    registry = build_provider_registry(without_youtube)

    assert enabled_worker_queues(registry, without_youtube) == {
        "process",
        "provider-scrape",
    }

    with_youtube = _fixture_settings(youtube_api_key="configured")
    assert enabled_worker_queues(
        build_provider_registry(with_youtube),
        with_youtube,
    ) == {"process", "provider-api", "provider-scrape"}


class _FakeRedis:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


class _MetricsRepository:
    def __init__(self) -> None:
        self.calls = 0

    def operational_metrics(self, *, claim_ttl_seconds: int) -> dict[str, int]:
        self.calls += 1
        assert claim_ttl_seconds == 300
        return {
            "dead_letter_jobs": 2,
            "stuck_processing_jobs": 1,
            "provider_quota_failures": 3,
            "provider_robots_failures": 4,
        }


def test_production_probe_covers_dependencies_and_alert_conditions() -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    redis = _FakeRedis(
        {
            "syco23:observability:beat:last_success_at": now.isoformat().encode(),
            "syco23:observability:redrive:publish_failures": b"2",
        }
    )
    inspector = SimpleNamespace(
        active_queues=lambda: {
            "api@worker": [{"name": "provider-api"}],
            "scrape@worker": [{"name": "provider-scrape"}],
            "process@worker": [{"name": "process"}],
        }
    )
    settings = _production_settings()
    registry = build_provider_registry(settings)
    probe = OperationalHealthProbe(
        _MetricsRepository(),
        settings,
        redis_factory=lambda _url: redis,
        inspector_factory=lambda _timeout: inspector,
        clock=lambda: now,
    )

    snapshot = probe(registry)

    assert snapshot["ready"] is True
    assert snapshot["dependencies"]["postgres"]["ready"] is True
    assert snapshot["dependencies"]["redis"]["ready"] is True
    assert snapshot["dependencies"]["worker_queues"] == {
        "ready": True,
        "status": "available",
        "expected": ["process", "provider-api", "provider-scrape"],
        "observed": ["process", "provider-api", "provider-scrape"],
        "missing": [],
    }
    assert snapshot["dependencies"]["beat"]["ready"] is True
    active = {
        alert["code"]: alert["value"]
        for alert in snapshot["alerts"]
        if alert["active"]
    }
    assert active == {
        "dead_letter_growth": 2,
        "provider_quota_failures": 3,
        "provider_robots_failures": 4,
        "redrive_publish_failures": 2,
        "stuck_processing_jobs": 1,
    }


def test_stale_beat_and_missing_descriptor_queue_fail_readiness() -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    redis = _FakeRedis(
        {
            "syco23:observability:beat:last_success_at": (
                now - timedelta(minutes=10)
            ).isoformat().encode(),
            "syco23:observability:redrive:publish_failures": b"0",
        }
    )
    settings = _production_settings(beat_stale_after_seconds=180)
    probe = OperationalHealthProbe(
        _MetricsRepository(),
        settings,
        redis_factory=lambda _url: redis,
        inspector_factory=lambda _timeout: SimpleNamespace(
            active_queues=lambda: {
                "api@worker": [{"name": "provider-api"}],
                "process@worker": [{"name": "process"}],
            }
        ),
        clock=lambda: now,
    )

    snapshot = probe(build_provider_registry(settings))

    assert snapshot["ready"] is False
    assert snapshot["dependencies"]["beat"] == {
        "ready": False,
        "status": "stale",
        "age_seconds": 600,
        "stale_after_seconds": 180,
    }
    assert snapshot["dependencies"]["worker_queues"]["missing"] == [
        "provider-scrape"
    ]


def test_production_probe_caches_bounded_dependency_work() -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    monotonic_values = iter((100.0, 105.0, 111.0))
    redis = _FakeRedis(
        {
            "syco23:observability:beat:last_success_at": now.isoformat().encode(),
            "syco23:observability:redrive:publish_failures": b"0",
        }
    )
    repository = _MetricsRepository()
    inspection_calls = 0

    def inspector_factory(_timeout):
        nonlocal inspection_calls
        inspection_calls += 1
        return SimpleNamespace(
            active_queues=lambda: {
                "all@worker": [
                    {"name": "process"},
                    {"name": "provider-api"},
                    {"name": "provider-scrape"},
                ]
            }
        )

    settings = _production_settings(health_probe_cache_seconds=10)
    probe = OperationalHealthProbe(
        repository,
        settings,
        redis_factory=lambda _url: redis,
        inspector_factory=inspector_factory,
        clock=lambda: now,
        monotonic_clock=lambda: next(monotonic_values),
    )
    registry = build_provider_registry(settings)

    first = probe(registry)
    cached = probe(registry)
    refreshed = probe(registry)

    assert first == cached == refreshed
    assert repository.calls == 2
    assert inspection_calls == 2


def test_repository_operational_metrics_are_aggregate_and_secret_free() -> None:
    repository = InMemoryRepository()
    dead_letter = repository.create_job(
        url="https://soundcloud.com/syco23/dead",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    stale = repository.create_job(
        url="https://soundcloud.com/syco23/stale",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    quota = repository.create_job(
        url="https://youtube.com/watch?v=quota",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    robots = repository.create_job(
        url="https://freeteknomusic.org/robots",
        source=SetSource.freeteknomusic,
        job_type=JobType.crawl,
    )
    repository.jobs[dead_letter.id] = dead_letter.model_copy(
        update={"status": JobStatus.dead_letter, "error_message": "secret"}
    )
    repository.jobs[stale.id] = stale.model_copy(
        update={
            "status": JobStatus.processing,
            "started_at": datetime.now(UTC) - timedelta(minutes=10),
        }
    )
    repository.jobs[quota.id] = quota.model_copy(
        update={"status": JobStatus.failed, "error_code": "youtube_quota_exceeded"}
    )
    repository.jobs[robots.id] = robots.model_copy(
        update={"status": JobStatus.blocked, "error_code": "robots_denied"}
    )

    metrics = repository.operational_metrics(claim_ttl_seconds=300)

    assert metrics == {
        "dead_letter_jobs": 1,
        "stuck_processing_jobs": 1,
        "provider_quota_failures": 1,
        "provider_robots_failures": 1,
    }
    assert "secret" not in json.dumps(metrics)


def test_health_preserves_public_contract_and_combines_operational_readiness() -> None:
    operational = {
        "ready": False,
        "dependencies": {
            "postgres": {"ready": False, "status": "unavailable"},
        },
        "alerts": [
            {
                "code": "dead_letter_growth",
                "severity": "critical",
                "active": True,
                "value": 2,
                "threshold": 1,
            }
        ],
    }
    app = create_app(
        InMemoryRepository.seeded(),
        settings=_fixture_settings(youtube_api_key="configured"),
        dispatcher=RecordingDispatcher(),
        operational_probe=lambda _registry: operational,
    )

    body = TestClient(app).get("/health").json()

    assert body["status"] == "ok"
    assert body["service"] == "syco23-setcrawler-api"
    assert body["ready"] is False
    assert set(body["providers"]) == {"youtube", "soundcloud", "ftm"}
    assert body["dependencies"] == operational["dependencies"]
    assert body["alerts"] == operational["alerts"]
