import asyncio
from pathlib import Path

import httpx
import pytest
from celery.exceptions import Retry

from app.core.config import Settings
from app.repository import InMemoryRepository
from app.schemas.import_job import JobStatus, JobType
from app.schemas.set import SetSource
from app.services.provider import (
    ProviderBlockedError,
    ProviderTemporaryError,
    ProviderValidationError,
)


BASE = "https://freeteknomusic.org"
FTM_URL = f"{BASE}/sets/23hz"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _live_ftm_worker_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker exercises opt into live mode without changing the safe default."""
    from app.workers import ftm_scraper

    monkeypatch.setattr(
        ftm_scraper,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            ftm_scraper_enabled=True,
        ),
    )


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, str]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, path: str, body: str, status: int = 200) -> None:
        self.responses[path] = (status, body)

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.get(request.url.path, (404, ""))
        return httpx.Response(status, text=body, request=request)


@pytest.fixture
def recording_transport() -> RecordingTransport:
    return RecordingTransport()


def _html() -> str:
    return (FIXTURES / "ftm_set.html").read_text()


def enabled_adapter(
    transport: RecordingTransport,
    *,
    sleep=None,
    max_pages: int = 25,
):
    from app.services.ftm import FTMAdapter

    async def no_sleep(_seconds: float) -> None:
        return None

    return FTMAdapter(
        enabled=True,
        transport=transport,
        scraper_user_agent="syco23-test/1.0",
        scraper_request_delay_ms=5_000,
        ftm_max_pages_per_run=max_pages,
        sleep=no_sleep if sleep is None else sleep,
    )


def test_disabled_ftm_never_makes_http_request(
    recording_transport: RecordingTransport,
) -> None:
    """Disabled providers must not probe robots or page URLs."""
    from app.services.ftm import FTMAdapter

    adapter = FTMAdapter(enabled=False, transport=recording_transport)

    with pytest.raises(ProviderBlockedError, match="disabled"):
        asyncio.run(adapter.fetch(FTM_URL))

    assert recording_transport.requests == []


def test_robots_denial_blocks_page_fetch(
    recording_transport: RecordingTransport,
) -> None:
    """A denied robots rule must stop before content is requested."""
    recording_transport.add("/robots.txt", "User-agent: *\nDisallow: /sets/")

    with pytest.raises(ProviderBlockedError, match="robots"):
        asyncio.run(enabled_adapter(recording_transport).fetch(FTM_URL))

    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt"
    ]


def test_unavailable_robots_blocks_page_fetch(
    recording_transport: RecordingTransport,
) -> None:
    """An unavailable robots policy must be treated as a stop, not permission."""
    recording_transport.add("/robots.txt", "", status=503)

    with pytest.raises(ProviderBlockedError, match="robots"):
        asyncio.run(enabled_adapter(recording_transport).fetch(FTM_URL))

    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt"
    ]


def test_robots_transport_failure_blocks_page_fetch() -> None:
    """A network failure while obtaining robots must never imply permission."""
    from app.services.ftm import FTMAdapter

    async def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(ProviderBlockedError, match="robots"):
        asyncio.run(
            FTMAdapter(
                enabled=True,
                transport=httpx.MockTransport(fail),
                scraper_user_agent="syco23-test/1.0",
            ).fetch(FTM_URL)
        )


def test_api_rejects_non_https_ftm_url(client_as_editor) -> None:
    """The import boundary must reject an FTM URL before a job is queued."""
    response = client_as_editor.post(
        "/imports/url",
        json={"url": "http://freeteknomusic.org/sets/23hz"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid FreeTeknoMusic URL"


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/download/23hz.mp3",
        f"{BASE}/DOWNLOAD/23HZ.MP3?token=one#fragment",
        f"{BASE}/download-file/23hz",
        f"{BASE}/sets/23hz%2Fdownload.mp3",
        f"{BASE}/media/23hz.webm",
        f"{BASE}/sets/23hz.mp3;metadata",
        f"{BASE}/api/tracks/1/play",
        f"{BASE}/sets/23hz/extra",
        f"{BASE}/sets/23hz%3Bmetadata",
        f"{BASE}/sets/23hz%2Fextra",
        f"{BASE}/sets/23hz?next=https://evil.example/",
    ],
)
def test_adapter_rejects_media_and_download_urls_without_http_request(
    recording_transport: RecordingTransport,
    url: str,
) -> None:
    """Direct imports must not reach an audio/video or download endpoint."""
    with pytest.raises(ProviderValidationError, match="ftm_invalid_url"):
        asyncio.run(enabled_adapter(recording_transport).fetch(url))

    assert recording_transport.requests == []


def test_api_rejects_ftm_download_url(client_as_editor) -> None:
    """The import API must reject a download endpoint before job creation."""
    response = client_as_editor.post(
        "/imports/url",
        json={"url": f"{BASE}/download/23hz.mp3"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid FreeTeknoMusic URL"


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/sets/23hz.mp3;metadata",
        f"{BASE}/api/tracks/1/play",
        f"{BASE}/sets/23hz/extra",
        f"{BASE}/sets/23hz%3Bmetadata",
        f"{BASE}/sets/23hz%2Fextra",
    ],
)
def test_api_rejects_non_set_page_routes(client_as_editor, url: str) -> None:
    """Only one safe FTM set-page route may create an import job."""
    response = client_as_editor.post("/imports/url", json={"url": url})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid FreeTeknoMusic URL"


def test_api_accepts_the_supported_ftm_set_page_route(client_as_editor) -> None:
    """The positive allowlist must retain ordinary editorial FTM imports."""
    response = client_as_editor.post("/imports/url", json={"url": FTM_URL})

    assert response.status_code == 202
    assert response.json()["url"] == FTM_URL


def test_fetch_extracts_metadata_and_preserves_raw_html(
    recording_transport: RecordingTransport,
) -> None:
    """The FTM adapter must normalize metadata without touching media links."""
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add("/sets/23hz", _html())

    payload = asyncio.run(enabled_adapter(recording_transport).fetch(FTM_URL))

    assert payload.source_id == "sets-23hz"
    assert payload.canonical_url == FTM_URL
    assert payload.title == "23Hz — RITUAL TEKNO SET"
    assert payload.description == "Recorded live at the abandoned hangar."
    assert payload.duration_seconds == 5_400
    assert payload.primary_image_url == "https://cdn.example.invalid/23hz-flyer.jpg"
    assert payload.raw_payload["raw_html"] == _html()
    assert len(payload.raw_payload["content_hash"]) == 64
    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt",
        "/sets/23hz",
    ]


def test_fetch_rejects_an_unsafe_canonical_route(
    recording_transport: RecordingTransport,
) -> None:
    """Provider-supplied canonical URLs cannot change the supported route."""
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add(
        "/sets/23hz",
        _html().replace(
            f"{BASE}/sets/23hz",
            f"{BASE}/api/tracks/1/play",
        ),
    )

    with pytest.raises(ProviderValidationError, match="ftm_invalid_url"):
        asyncio.run(enabled_adapter(recording_transport).fetch(FTM_URL))

    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt",
        "/sets/23hz",
    ]


def test_configured_delay_occurs_between_page_requests(
    recording_transport: RecordingTransport,
) -> None:
    """Removing crawl pacing would overload the upstream provider."""
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add("/sets/23hz", _html())
    recording_transport.add("/sets/second", _html().replace("23hz", "second"))
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    payloads = asyncio.run(
        enabled_adapter(recording_transport, sleep=sleep).crawl(FTM_URL, 2)
    )

    assert len(payloads) == 2
    assert delays == [5.0, 5.0, 5.0]
    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt",
        "/sets/23hz",
        "/robots.txt",
        "/sets/second",
    ]


@pytest.mark.parametrize(
    "second_robots_status,second_robots_body",
    [
        (200, "User-agent: *\nDisallow: /sets/second"),
        (503, ""),
    ],
)
def test_crawl_rechecks_robots_before_second_page_and_fails_closed(
    second_robots_status: int,
    second_robots_body: str,
) -> None:
    """A changed or unavailable policy must block before page two is fetched."""
    requests: list[str] = []
    robots_calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal robots_calls
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            robots_calls += 1
            if robots_calls == 1:
                return httpx.Response(
                    200,
                    text="User-agent: *\nAllow: /",
                    request=request,
                )
            return httpx.Response(
                second_robots_status,
                text=second_robots_body,
                request=request,
            )
        if request.url.path == "/sets/23hz":
            return httpx.Response(200, text=_html(), request=request)
        raise AssertionError("second page content was fetched")

    from app.services.ftm import FTMAdapter

    adapter = FTMAdapter(
        enabled=True,
        transport=httpx.MockTransport(respond),
        scraper_user_agent="syco23-test/1.0",
        scraper_request_delay_ms=0,
    )

    with pytest.raises(ProviderBlockedError, match="robots"):
        asyncio.run(adapter.crawl(FTM_URL, 2))

    assert requests == [
        "/robots.txt",
        "/sets/23hz",
        "/robots.txt",
    ]


def test_crawl_caps_pages_deduplicates_urls_and_identical_content(
    recording_transport: RecordingTransport,
) -> None:
    """Crawler bounds and dedupe prevent repeated or unbounded provider load."""
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add("/sets/23hz", _html())
    recording_transport.add("/sets/second", _html())

    payloads = asyncio.run(
        enabled_adapter(recording_transport, max_pages=1).crawl(FTM_URL, 99)
    )

    assert len(payloads) == 1
    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt",
        "/sets/23hz",
    ]

    recording_transport = RecordingTransport()
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add("/sets/23hz", _html())
    recording_transport.add("/sets/second", _html())
    payloads = asyncio.run(enabled_adapter(recording_transport).crawl(FTM_URL, 3))

    assert len(payloads) == 1
    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt",
        "/sets/23hz",
        "/robots.txt",
        "/sets/second",
    ]


def test_crawl_never_uses_robots_policy_for_another_host(
    recording_transport: RecordingTransport,
) -> None:
    """A robots file is host-specific, even for the allowed www alias."""
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add(
        "/sets/23hz",
        _html().replace(
            "/sets/second",
            "https://www.freeteknomusic.org/sets/second",
        ),
    )

    payloads = asyncio.run(enabled_adapter(recording_transport).crawl(FTM_URL, 2))

    assert len(payloads) == 1
    assert {request.url.host for request in recording_transport.requests} == {
        "freeteknomusic.org"
    }


def test_crawl_ignores_discovered_routes_outside_set_allowlist(
    recording_transport: RecordingTransport,
) -> None:
    """A page link outside the positive set route must never enter the queue."""
    recording_transport.add("/robots.txt", "User-agent: *\nAllow: /")
    recording_transport.add(
        "/sets/23hz",
        _html().replace("/sets/second", "/api/tracks/1/play"),
    )

    payloads = asyncio.run(enabled_adapter(recording_transport).crawl(FTM_URL, 2))

    assert len(payloads) == 1
    assert [request.url.path for request in recording_transport.requests] == [
        "/robots.txt",
        "/sets/23hz",
    ]


def test_disabled_worker_is_blocked_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled FTM provider must end in a safe blocked terminal state."""
    from app.workers import ftm_scraper

    repository = InMemoryRepository()
    job = repository.create_job(
        url=FTM_URL,
        source=SetSource.freeteknomusic,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, _: str):
            raise AssertionError("disabled worker fetched provider")

    monkeypatch.setattr(ftm_scraper, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(ftm_scraper, "get_ftm_adapter", lambda: Adapter())
    monkeypatch.setattr(
        ftm_scraper,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            ftm_scraper_enabled=False,
        ),
    )

    result = ftm_scraper.import_ftm.run(str(job.id))

    assert result is None
    blocked = repository.get_job(job.id)
    assert blocked.status is JobStatus.blocked
    assert blocked.error_code == "provider_disabled"


def test_robots_denied_worker_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Robots denial must be terminal and distinct from a failed parse."""
    from app.workers import ftm_scraper
    from app.services.provider import ProviderBlockedError

    repository = InMemoryRepository()
    job = repository.create_job(
        url=FTM_URL,
        source=SetSource.freeteknomusic,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, _: str):
            raise ProviderBlockedError("ftm_robots_denied")

    monkeypatch.setattr(ftm_scraper, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(ftm_scraper, "get_ftm_adapter", lambda: Adapter())
    monkeypatch.setattr(
        ftm_scraper,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            ftm_scraper_enabled=True,
        ),
    )

    result = ftm_scraper.import_ftm.run(str(job.id))

    assert result is None
    blocked = repository.get_job(job.id)
    assert blocked.status is JobStatus.blocked
    assert blocked.error_code == "robots_denied"


def test_temporary_ftm_error_retries_with_common_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate limits and timeouts must retain the durable retry policy."""
    from app.workers import ftm_scraper

    repository = InMemoryRepository()
    job = repository.create_job(
        url=FTM_URL,
        source=SetSource.freeteknomusic,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, _: str):
            raise ProviderTemporaryError("ftm_temporary_error")

    monkeypatch.setattr(ftm_scraper, "get_worker_repository", lambda: repository)
    monkeypatch.setattr(ftm_scraper, "get_ftm_adapter", lambda: Adapter())
    monkeypatch.setattr(
        ftm_scraper,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            ftm_scraper_enabled=True,
        ),
    )

    ftm_scraper.import_ftm.push_request(
        retries=0,
        called_directly=False,
        is_eager=True,
    )
    try:
        with pytest.raises(Retry) as retry:
            ftm_scraper.import_ftm.run(str(job.id))
    finally:
        ftm_scraper.import_ftm.pop_request()

    assert retry.value.when == 5
    assert repository.get_job(job.id).status is JobStatus.retry
