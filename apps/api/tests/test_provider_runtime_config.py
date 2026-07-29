import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.main import create_app
from app.repository import InMemoryRepository
from app.schemas.import_job import JobStatus, JobType
from app.schemas.profile import SearchProfileCreate
from app.schemas.set import SetSource
from app.services.ftm import FTMAdapter
from app.services.soundcloud import SoundCloudAdapter
from app.services.youtube import YouTubeAdapter
from conftest import RecordingDispatcher


def test_provider_settings_read_environment_and_enforce_safe_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROVIDER_MODE", "fixture")
    monkeypatch.setenv("PROVIDER_REQUEST_TIMEOUT_SECONDS", "11")
    monkeypatch.setenv("PROVIDER_OUTPUT_LIMIT_BYTES", "512")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    assert settings.provider_mode == "fixture"
    assert settings.provider_request_timeout_seconds == 11
    assert settings.provider_output_limit_bytes == 512
    with pytest.raises(ValidationError):
        Settings(provider_request_timeout_seconds=121)
    with pytest.raises(ValidationError):
        Settings(provider_output_limit_bytes=1_048_577)


@pytest.mark.parametrize("delay", ["4999", "10001"])
def test_environment_rejects_unsafe_ftm_delay(
    monkeypatch: pytest.MonkeyPatch,
    delay: str,
) -> None:
    monkeypatch.setenv("SCRAPER_REQUEST_DELAY_MS", delay)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("delay", [5_000, 10_000])
def test_runtime_ftm_delay_accepts_only_the_documented_range(delay: int) -> None:
    assert Settings(scraper_request_delay_ms=delay).scraper_request_delay_ms == delay


@pytest.mark.parametrize("delay", [4_999, 10_001])
def test_runtime_ftm_delay_rejects_unsafe_values(delay: int) -> None:
    with pytest.raises(ValidationError):
        Settings(scraper_request_delay_ms=delay)


def test_adapters_consume_runtime_http_and_output_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.ftm as ftm
    import app.services.soundcloud as soundcloud
    import app.services.youtube as youtube

    settings = Settings(
        provider_request_timeout_seconds=11,
        provider_output_limit_bytes=512,
    )
    monkeypatch.setattr(youtube, "get_settings", lambda: settings)
    monkeypatch.setattr(ftm, "get_settings", lambda: settings)
    monkeypatch.setattr(soundcloud, "get_settings", lambda: settings)

    assert YouTubeAdapter().timeout == 11
    assert FTMAdapter().timeout == 11
    adapter = SoundCloudAdapter()
    assert adapter.output_limit_bytes == 512
    assert adapter.process_timeout_seconds == 30


def test_fixture_mode_blocks_dispatch_and_is_visible_in_provider_health() -> None:
    repository = InMemoryRepository.seeded()
    dispatcher = RecordingDispatcher()
    client = TestClient(
        create_app(
            repository,
            settings=Settings(
                environment="fixture",
                repository_mode="memory",
                provider_mode="fixture",
                youtube_api_key="fixture-key",
                yt_dlp_bin="yt-dlp",
                scraper_user_agent="syco23-test/1.0",
            ),
            dispatcher=dispatcher,
        )
    )

    providers = client.get("/providers")
    response = client.post(
        "/imports/url",
        json={"url": "https://soundcloud.com/syco23/ritual-session"},
    )

    assert providers.status_code == 200
    assert providers.json()["soundcloud"]["runtime_mode"] == "fixture"
    assert providers.json()["soundcloud"]["enabled"] is False
    assert response.status_code == 409
    assert response.json()["detail"] == "Provider imports are disabled in fixture mode"
    assert dispatcher.calls == []


def test_fixture_mode_youtube_direct_worker_blocks_before_adapter_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    job = repository.create_job(
        url="https://www.youtube.com/watch?v=fixture-mode",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    fetched = False

    class Adapter:
        async def fetch(self, _: str):
            nonlocal fetched
            fetched = True
            raise AssertionError("fixture worker fetched YouTube")

    monkeypatch.setattr(youtube_poller, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(youtube_poller, "get_youtube_adapter", lambda: Adapter())
    monkeypatch.setattr(
        youtube_poller,
        "get_settings",
        lambda: Settings(environment="fixture", repository_mode="memory"),
    )

    assert youtube_poller.import_url.run(str(job.id)) is None
    blocked = repository.get_job(job.id)
    assert fetched is False
    assert blocked is not None
    assert blocked.status is JobStatus.blocked
    assert blocked.error_code == "provider_mode_fixture"


def test_fixture_mode_youtube_profile_worker_blocks_before_adapter_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Fixture", query="fixture liveset")
    )
    job = repository.queue_profile(profile.id)
    assert job is not None
    searched = False

    class Adapter:
        async def search(self, _: object):
            nonlocal searched
            searched = True
            raise AssertionError("fixture worker searched YouTube")

    monkeypatch.setattr(youtube_poller, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(youtube_poller, "get_youtube_adapter", lambda: Adapter())
    monkeypatch.setattr(
        youtube_poller,
        "get_settings",
        lambda: Settings(environment="fixture", repository_mode="memory"),
    )

    assert youtube_poller.run_youtube_profile.run(str(job.id)) is None
    blocked = repository.get_job(job.id)
    assert searched is False
    assert blocked is not None
    assert blocked.status is JobStatus.blocked
    assert blocked.error_code == "provider_mode_fixture"
    assert youtube_poller.run_youtube_profile.run(str(job.id)) is None
    redelivered = repository.get_job(job.id)
    assert searched is False
    assert redelivered == blocked


def test_fixture_mode_soundcloud_worker_blocks_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import soundcloud_importer

    repository = InMemoryRepository()
    job = repository.create_job(
        url="https://soundcloud.com/syco23/fixture-mode",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    fetched = False

    class Adapter:
        async def fetch(self, _: str):
            nonlocal fetched
            fetched = True
            raise AssertionError("fixture worker ran yt-dlp")

    monkeypatch.setattr(soundcloud_importer, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(soundcloud_importer, "get_soundcloud_adapter", lambda: Adapter())
    monkeypatch.setattr(
        soundcloud_importer,
        "get_settings",
        lambda: Settings(environment="fixture", repository_mode="memory"),
    )

    assert soundcloud_importer.import_soundcloud.run(str(job.id)) is None
    blocked = repository.get_job(job.id)
    assert fetched is False
    assert blocked is not None
    assert blocked.status is JobStatus.blocked
    assert blocked.error_code == "provider_mode_fixture"


def test_fixture_mode_ftm_worker_blocks_before_network_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import ftm_scraper

    repository = InMemoryRepository()
    job = repository.create_job(
        url="https://freeteknomusic.org/sets/fixture-mode",
        source=SetSource.freeteknomusic,
        job_type=JobType.url_import,
    )
    fetched = False

    class Adapter:
        async def fetch(self, _: str):
            nonlocal fetched
            fetched = True
            raise AssertionError("fixture worker fetched FTM")

    monkeypatch.setattr(ftm_scraper, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(ftm_scraper, "get_ftm_adapter", lambda: Adapter())
    monkeypatch.setattr(
        ftm_scraper,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            ftm_scraper_enabled=True,
        ),
    )

    assert ftm_scraper.import_ftm.run(str(job.id)) is None
    blocked = repository.get_job(job.id)
    assert fetched is False
    assert blocked is not None
    assert blocked.status is JobStatus.blocked
    assert blocked.error_code == "provider_mode_fixture"
