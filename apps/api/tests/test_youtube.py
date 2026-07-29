import asyncio
from copy import deepcopy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from celery.exceptions import Retry

from app.repositories.memory import InMemoryRepository
from app.core.config import Settings
from app.schemas.import_job import ImportJobPatch, JobStatus, JobType
from app.schemas.profile import SearchProfileCreate
from app.schemas.set import SetSource
from app.services.normalizer import RawSetPayload
from app.schemas.profile import SearchProfile
from app.services.provider import (
    ProviderPayloadError,
    ProviderQuotaError,
    ProviderTemporaryError,
)
from app.services.youtube import YouTubeAdapter, YouTubeSearchBatch


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _live_youtube_worker_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker exercises opt into live mode without changing the safe default."""
    from app.workers import youtube_poller

    monkeypatch.setattr(
        youtube_poller,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
    )


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@contextmanager
def _task_request(task, retries: int) -> Iterator[None]:
    task.push_request(
        retries=retries,
        called_directly=False,
        is_eager=True,
    )
    try:
        yield
    finally:
        task.pop_request()


def test_search_uses_video_long_filter_and_normalizes_details() -> None:
    """Wrong search filters, duration parsing, or image order corrupt discovery."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=_fixture("youtube_search.json"),
            )
        return httpx.Response(
            200,
            json=_fixture("youtube_videos.json"),
        )

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )
    batch = asyncio.run(
        adapter.search(
            SearchProfile(
                name="Long sets",
                query="tribe liveset",
                next_page_token="CURRENT_PAGE",
            )
        )
    )

    search_request, videos_request = requests
    assert search_request.url.params["part"] == "snippet"
    assert search_request.url.params["type"] == "video"
    assert search_request.url.params["videoDuration"] == "long"
    assert search_request.url.params["maxResults"] == "50"
    assert search_request.url.params["q"] == "tribe liveset"
    assert search_request.url.params["pageToken"] == "CURRENT_PAGE"
    assert search_request.url.params["key"] == "server-key"
    assert videos_request.url.params["part"] == "snippet,contentDetails,status"
    assert len(videos_request.url.params["id"].split(",")) <= 50
    assert batch.next_page_token == "NEXT_PAGE"
    assert batch.payloads[0].duration_seconds == 5062
    assert batch.payloads[0].primary_image_url.endswith("maxres.jpg")
    assert set(batch.payloads[0].raw_payload["thumbnails"]) == {
        "default",
        "medium",
        "high",
        "standard",
        "maxres",
    }


def test_search_omits_page_token_when_profile_has_no_cursor() -> None:
    """An empty pageToken must not be sent as an invalid API cursor."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        fixture = (
            "youtube_search.json"
            if request.url.path.endswith("/search")
            else "youtube_videos.json"
        )
        return httpx.Response(200, json=_fixture(fixture))

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )
    asyncio.run(
        adapter.search(
            SearchProfile(name="Long sets", query="tribe liveset")
        )
    )

    assert "pageToken" not in requests[0].url.params


def test_quota_error_has_safe_stable_code() -> None:
    """Leaking Google's response text or retrying quota exhaustion is unsafe."""

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Project 123 secret quota details",
                    "errors": [
                        {
                            "message": "Project 123 secret quota details",
                            "domain": "youtube.quota",
                            "reason": "quotaExceeded",
                        }
                    ],
                    "status": "PERMISSION_DENIED",
                }
            },
        )

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        ProviderQuotaError,
        match="^youtube_quota_exceeded$",
    ):
        asyncio.run(
            adapter.search(
                SearchProfile(name="Long sets", query="tribe liveset")
            )
        )


@pytest.mark.parametrize(
    ("url", "expected_id"),
    [
        ("https://www.youtube.com/watch?v=video-alpha", "video-alpha"),
        ("https://youtu.be/video-alpha", "video-alpha"),
    ],
)
def test_fetch_accepts_only_supported_video_urls(
    url: str,
    expected_id: str,
) -> None:
    """Direct imports must extract the canonical ID and use video details."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_fixture("youtube_videos.json"),
        )

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    payload = asyncio.run(adapter.fetch(url))

    assert requests[0].url.params["id"] == expected_id
    assert payload.source_id == expected_id
    assert payload.canonical_url == (
        f"https://www.youtube.com/watch?v={expected_id}"
    )


def test_fetch_rejects_non_video_youtube_url_without_http() -> None:
    """Channel and playlist URLs must never be mistaken for direct videos."""
    called = False

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"items": []})

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^youtube_video_unavailable$",
    ):
        asyncio.run(
            adapter.fetch(
                "https://www.youtube.com/playlist?list=private-list"
            )
        )
    assert called is False


def test_fetch_maps_missing_or_private_video_to_safe_error() -> None:
    """A hidden video must produce a stable payload error without API leakage."""

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "kind": "youtube#videoListResponse",
                "etag": "empty",
                "items": [],
                "pageInfo": {"totalResults": 0, "resultsPerPage": 0},
            },
        )

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^youtube_video_unavailable$",
    ):
        asyncio.run(
            adapter.fetch(
                "https://www.youtube.com/watch?v=private-video"
            )
        )


def test_fetch_rejects_video_with_non_public_status() -> None:
    """A returned private item must not enter the import pipeline."""
    private_video = _fixture("youtube_videos.json")
    private_video["items"] = [private_video["items"][0]]
    private_video["items"][0]["status"]["privacyStatus"] = "private"

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=private_video)

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^youtube_video_unavailable$",
    ):
        asyncio.run(
            adapter.fetch(
                "https://www.youtube.com/watch?v=video-alpha"
            )
        )


def test_search_preserves_search_order_when_details_are_reversed() -> None:
    """videos.list ordering must not change the discovery order."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = _fixture("youtube_search.json")
        if request.url.path.endswith("/videos"):
            payload = _fixture("youtube_videos.json")
            payload["items"].reverse()
        return httpx.Response(200, json=payload)

    batch = asyncio.run(
        YouTubeAdapter(
            api_key="server-key",
            transport=httpx.MockTransport(respond),
        ).search(SearchProfile(name="Order", query="ordered liveset"))
    )

    assert [item.source_id for item in batch.payloads] == [
        "video-alpha",
        "video-beta",
    ]


def test_search_omits_missing_partial_detail_without_reordering() -> None:
    """One unavailable detail must not misassociate another video's metadata."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = _fixture("youtube_search.json")
        if request.url.path.endswith("/videos"):
            payload = _fixture("youtube_videos.json")
            payload["items"] = [payload["items"][1]]
        return httpx.Response(200, json=payload)

    batch = asyncio.run(
        YouTubeAdapter(
            api_key="server-key",
            transport=httpx.MockTransport(respond),
        ).search(SearchProfile(name="Partial", query="partial liveset"))
    )

    assert [item.source_id for item in batch.payloads] == ["video-beta"]


def test_video_details_batches_at_fifty_id_boundary() -> None:
    """A 51st ID must use a second videos.list call."""
    sizes: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"kind": "youtube#video", "videoId": f"v{index}"}}
                        for index in range(51)
                    ]
                },
            )
        ids = request.url.params["id"].split(",")
        sizes.append(len(ids))
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": video_id,
                        "snippet": {
                            "publishedAt": "2026-07-10T18:30:00Z",
                            "title": f"Video {video_id}",
                            "description": "",
                            "thumbnails": {},
                        },
                        "contentDetails": {"duration": "PT30M"},
                        "status": {"privacyStatus": "public"},
                    }
                    for video_id in ids
                ]
            },
        )

    batch = asyncio.run(
        YouTubeAdapter(
            api_key="server-key",
            transport=httpx.MockTransport(respond),
        ).search(SearchProfile(name="Boundary", query="boundary liveset"))
    )

    assert sizes == [50, 1]
    assert len(batch.payloads) == 51


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_http_transient_statuses_are_classified_for_retry(
    status_code: int,
) -> None:
    """HTTP 429 and 5xx must enter the worker's bounded retry path."""

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {}})

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        ProviderTemporaryError,
        match="^youtube_temporary_error$",
    ):
        asyncio.run(
            adapter.search(
                SearchProfile(name="Transient", query="retry liveset")
            )
        )


def test_http_timeout_is_classified_for_retry() -> None:
    """A concrete HTTP timeout must not strand a processing job."""

    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timed out", request=request)

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        ProviderTemporaryError,
        match="^youtube_temporary_error$",
    ):
        asyncio.run(
            adapter.search(
                SearchProfile(name="Timeout", query="timeout liveset")
            )
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"items": {}}),
    ],
)
def test_malformed_search_response_is_safe_payload_error(
    response: httpx.Response,
) -> None:
    """Malformed successful responses must not leak parser exceptions."""
    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(lambda _: response),
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^youtube_invalid_response$",
    ):
        asyncio.run(
            adapter.search(
                SearchProfile(name="Malformed", query="bad liveset")
            )
        )


def test_non_json_403_is_safe_payload_error() -> None:
    """A proxy-generated 403 body must not leak JSON decoding details."""
    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(403, content=b"<html>denied</html>")
        ),
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^youtube_provider_error$",
    ):
        asyncio.run(
            adapter.search(
                SearchProfile(name="Denied", query="denied liveset")
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snippet", None),
        ("contentDetails", None),
        ("publishedAt", "not-a-timestamp"),
        ("duration", None),
        ("privacyStatus", None),
    ],
)
def test_malformed_video_detail_is_safe_payload_error(
    field: str,
    value: object,
) -> None:
    """Malformed detail fields must be classified at the provider boundary."""
    videos = _fixture("youtube_videos.json")
    videos["items"] = [deepcopy(videos["items"][0])]
    if field in {"snippet", "contentDetails"}:
        videos["items"][0][field] = value
    elif field == "publishedAt":
        videos["items"][0]["snippet"][field] = value
    elif field == "privacyStatus":
        videos["items"][0]["status"][field] = value
    else:
        videos["items"][0]["contentDetails"][field] = value

    adapter = YouTubeAdapter(
        api_key="server-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=videos)
        ),
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^youtube_invalid_response$",
    ):
        asyncio.run(
            adapter.fetch(
                "https://www.youtube.com/watch?v=video-alpha"
            )
        )


def _payload(
    source_id: str,
    title: str,
    duration_seconds: int,
) -> RawSetPayload:
    return RawSetPayload(
        source=SetSource.youtube,
        source_id=source_id,
        canonical_url=(
            f"https://www.youtube.com/watch?v={source_id}"
        ),
        title=title,
        duration_seconds=duration_seconds,
        raw_payload={"id": source_id, "thumbnails": {}},
    )


def _use_eager_process_worker(
    monkeypatch: pytest.MonkeyPatch,
    repository: InMemoryRepository,
) -> None:
    from app.workers import normalize_worker

    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )


def test_profile_worker_processes_payloads_and_persists_cursor(
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """Losing counts or the page token causes repeated or invisible imports."""
    from app.workers import youtube_poller

    repository = InMemoryRepository.seeded()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Worker profile",
            query="freetekno liveset",
        )
    )
    repository.profiles[profile.id] = profile.model_copy(
        update={"next_page_token": "CURRENT_PAGE"}
    )
    job = repository.queue_profile(profile.id)
    assert job is not None

    class Adapter:
        async def search(
            self,
            current: SearchProfile,
        ) -> YouTubeSearchBatch:
            assert current.next_page_token == "CURRENT_PAGE"
            return YouTubeSearchBatch(
                payloads=[
                    _payload(
                        "new-accepted",
                        "SYCO23 LIVESET @ TEKNIVAL",
                        5_400,
                    ),
                    _payload("new-discarded", "Short clip", 60),
                    _payload(
                        "yt-murph-2026",
                        "MURPH @ SOUTH SIDE TEKNIVAL 2026",
                        5_062,
                    ),
                ],
                next_page_token="NEXT_PAGE",
            )

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    _use_eager_process_worker(monkeypatch, repository)

    result = youtube_poller.run_youtube_profile.run(str(job.id))

    completed = repository.get_job(job.id)
    updated_profile = repository.get_profile(profile.id)
    assert result is None
    assert completed is not None
    assert completed.status is JobStatus.completed
    assert completed.details["result_count"] == 1
    assert completed.details["discard_count"] == 1
    assert completed.details["duplicate_count"] == 1
    assert updated_profile is not None
    assert updated_profile.next_page_token == "NEXT_PAGE"
    assert updated_profile.last_run_at is not None
    assert updated_profile.last_result_count == 1
    assert updated_profile.last_error_code is None
    assert updated_profile.latest_job_id == job.id


def test_profile_worker_fails_quota_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota exhaustion must be terminal and must not expose provider details."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Worker profile",
            query="freetekno liveset",
        )
    )
    job = repository.queue_profile(profile.id)
    assert job is not None

    class Adapter:
        async def search(
            self,
            current: SearchProfile,
        ) -> YouTubeSearchBatch:
            raise ProviderQuotaError("youtube_quota_exceeded")

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )

    with pytest.raises(
        ProviderQuotaError,
        match="^youtube_quota_exceeded$",
    ):
        youtube_poller.run_youtube_profile.run(str(job.id))

    failed = repository.get_job(job.id)
    updated_profile = repository.get_profile(profile.id)
    assert failed is not None
    assert failed.status is JobStatus.failed
    assert failed.error_code == "youtube_quota_exceeded"
    assert failed.next_retry_at is None
    assert updated_profile is not None
    assert updated_profile.last_error_code == (
        "youtube_quota_exceeded"
    )


@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.completed, JobStatus.failed, JobStatus.dead_letter],
)
def test_profile_worker_ignores_terminal_late_deliveries(
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: JobStatus,
) -> None:
    """Late acknowledgements must not turn durable terminal jobs into failures."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Terminal delivery", query="terminal")
    )
    job = repository.queue_profile(profile.id)
    assert job is not None
    if terminal_status in {JobStatus.completed, JobStatus.dead_letter}:
        repository.transition_job(job.id, ImportJobPatch(status=JobStatus.processing))
    if terminal_status is JobStatus.dead_letter:
        repository.transition_job(job.id, ImportJobPatch(status=JobStatus.retry))
        repository.transition_job(
            job.id,
            ImportJobPatch(status=JobStatus.dead_letter),
        )
    else:
        repository.transition_job(job.id, ImportJobPatch(status=terminal_status))
    searched = False

    class Adapter:
        async def search(self, _: SearchProfile) -> YouTubeSearchBatch:
            nonlocal searched
            searched = True
            raise AssertionError("terminal job searched provider")

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )

    result = youtube_poller.run_youtube_profile.run(str(job.id))

    assert searched is False
    assert repository.get_job(job.id).status is terminal_status
    if terminal_status is JobStatus.completed:
        assert result == {
            "result_count": 0,
            "discard_count": 0,
            "duplicate_count": 0,
        }
    else:
        assert result is None


def test_profile_worker_retries_temporary_failures_on_shared_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing provider retry timing can overload YouTube or strand jobs."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(
            name="Worker profile",
            query="freetekno liveset",
        )
    )
    job = repository.queue_profile(profile.id)
    assert job is not None

    class Adapter:
        async def search(
            self,
            current: SearchProfile,
        ) -> YouTubeSearchBatch:
            raise ProviderTemporaryError("youtube_temporary_error")

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )

    for retries, delay in ((0, 5), (1, 30), (2, 120)):
        if retries:
            current = repository.get_job(job.id)
            repository.jobs[job.id] = current.model_copy(
                update={
                    "next_retry_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ),
                }
            )
        with _task_request(
            youtube_poller.run_youtube_profile,
            retries,
        ):
            with pytest.raises(Retry) as retry:
                youtube_poller.run_youtube_profile.run(str(job.id))
        current = repository.get_job(job.id)
        assert retry.value.when == delay
        assert current is not None
        assert current.status is JobStatus.retry
        assert current.next_retry_at is not None

    current = repository.get_job(job.id)
    repository.jobs[job.id] = current.model_copy(
        update={
            "next_retry_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    with _task_request(youtube_poller.run_youtube_profile, 3):
        with pytest.raises(
            ProviderTemporaryError,
            match="^youtube_temporary_error$",
        ):
            youtube_poller.run_youtube_profile.run(str(job.id))

    exhausted = repository.get_job(job.id)
    assert exhausted is not None
    assert exhausted.status is JobStatus.dead_letter
    assert exhausted.error_code == "retry_exhausted"


def test_stale_direct_retry_exhaustion_cannot_dead_letter_new_claim() -> None:
    """The retry-to-dead-letter hop must keep the original fencing token."""
    from app.workers.normalize_worker import _record_retry

    class ReclaimAfterRetryRepository(InMemoryRepository):
        replacement_started_at = None

        def transition_claimed_job(
            self,
            job_id,
            claim_started_at,
            patch,
        ):
            transitioned = super().transition_claimed_job(
                job_id,
                claim_started_at,
                patch,
            )
            if (
                transitioned is not None
                and patch.status is JobStatus.retry
                and self.replacement_started_at is None
            ):
                replacement = self.claim_job(job_id)
                assert replacement is not None
                self.replacement_started_at = replacement.started_at
            return transitioned

    repository = ReclaimAfterRetryRepository()
    job = repository.create_job(
        url="https://www.youtube.com/watch?v=retry-fence",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None and claimed.started_at is not None

    _record_retry(
        repository,
        job.id,
        ProviderTemporaryError("youtube_temporary_error"),
        3,
        claim_started_at=claimed.started_at,
    )

    current = repository.get_job(job.id)
    assert current is not None
    assert current.status is JobStatus.processing
    assert current.started_at == repository.replacement_started_at


def _stale_parent(
    repository: InMemoryRepository,
    job_id,
) -> None:
    repository.jobs[job_id] = repository.jobs[job_id].model_copy(
        update={
            "started_at": datetime.now(UTC) - timedelta(hours=1)
        }
    )


class _LostWorker(BaseException):
    pass


def test_profile_worker_recovers_loss_before_first_child(
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """A lost lease before child creation must replay the same parent page."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Before child", query="before child liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None
    payload = _payload(
        "before-child-video",
        "BEFORE CHILD LIVESET",
        5_400,
    )

    class Adapter:
        async def search(self, _: SearchProfile) -> YouTubeSearchBatch:
            return YouTubeSearchBatch(
                payloads=[payload],
                next_page_token="NEXT",
            )

    original = repository.get_or_create_child_job
    lost = True

    def lose_once(parent_job_id, claim_started_at, current_payload):
        nonlocal lost
        if lost:
            lost = False
            raise _LostWorker()
        return original(
            parent_job_id,
            claim_started_at,
            current_payload,
        )

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        repository,
        "get_or_create_child_job",
        lose_once,
    )
    _use_eager_process_worker(monkeypatch, repository)

    with pytest.raises(_LostWorker):
        youtube_poller.run_youtube_profile.run(str(parent.id))
    assert repository.get_job(parent.id).status is JobStatus.processing

    _stale_parent(repository, parent.id)
    result = youtube_poller.run_youtube_profile.run(str(parent.id))

    assert result is None
    completed = repository.get_job(parent.id)
    assert completed.status is JobStatus.completed
    assert completed.details["result_count"] == 1
    assert len(
        [
            job
            for job in repository.jobs.values()
            if job.details.get("profile_job_id") == str(parent.id)
        ]
    ) == 1


def test_profile_recovery_reuses_checkpoint_without_refetching_mutable_page(
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """A reclaimed parent must process its original page, not today's page."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Drift", query="mutable page liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None
    original_payload = _payload(
        "original-page-video",
        "ORIGINAL PAGE LIVESET",
        5_400,
    )
    adapter_calls = 0

    class Adapter:
        async def search(self, _: SearchProfile) -> YouTubeSearchBatch:
            nonlocal adapter_calls
            adapter_calls += 1
            if adapter_calls > 1:
                raise AssertionError("checkpointed page was refetched")
            return YouTubeSearchBatch(
                payloads=[original_payload],
                next_page_token="ORIGINAL_NEXT",
            )

    original_get_or_create = repository.get_or_create_child_job
    lost = True

    def lose_after_checkpoint(*args):
        nonlocal lost
        if lost:
            lost = False
            raise _LostWorker()
        return original_get_or_create(*args)

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        repository,
        "get_or_create_child_job",
        lose_after_checkpoint,
    )
    _use_eager_process_worker(monkeypatch, repository)

    with pytest.raises(_LostWorker):
        youtube_poller.run_youtube_profile.run(str(parent.id))
    checkpoint = repository.get_job(parent.id).details.get(
        "youtube_page_checkpoint"
    )
    assert checkpoint is not None
    assert checkpoint["input_page_token"] is None
    assert checkpoint["next_page_token"] == "ORIGINAL_NEXT"
    assert checkpoint["source_ids"] == ["original-page-video"]

    _stale_parent(repository, parent.id)
    result = youtube_poller.run_youtube_profile.run(str(parent.id))

    assert adapter_calls == 1
    assert result is None
    assert repository.get_job(parent.id).details["result_count"] == 1
    assert repository.get_profile(profile.id).next_page_token == (
        "ORIGINAL_NEXT"
    )


def test_profile_worker_recovers_loss_mid_page_with_stable_counts(
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """A mid-page replay must reuse the completed child and count it once."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Mid page", query="mid page liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None
    payloads = [
        _payload("mid-accepted", "MID PAGE LIVESET", 5_400),
        _payload("mid-discarded", "Short clip", 60),
    ]

    class Adapter:
        async def search(self, _: SearchProfile) -> YouTubeSearchBatch:
            return YouTubeSearchBatch(
                payloads=payloads,
                next_page_token="NEXT",
            )

    original = repository.get_or_create_child_job
    calls = 0

    def lose_on_second(
        parent_job_id,
        claim_started_at,
        current_payload,
    ):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise _LostWorker()
        return original(
            parent_job_id,
            claim_started_at,
            current_payload,
        )

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        repository,
        "get_or_create_child_job",
        lose_on_second,
    )
    _use_eager_process_worker(monkeypatch, repository)

    with pytest.raises(_LostWorker):
        youtube_poller.run_youtube_profile.run(str(parent.id))

    _stale_parent(repository, parent.id)
    result = youtube_poller.run_youtube_profile.run(str(parent.id))

    assert result is None
    assert {
        key: repository.get_job(parent.id).details[key]
        for key in (
            "result_count",
            "discard_count",
            "duplicate_count",
        )
    } == {
        "result_count": 1,
        "discard_count": 1,
        "duplicate_count": 0,
    }
    children = [
        job
        for job in repository.jobs.values()
        if job.details.get("profile_job_id") == str(parent.id)
    ]
    assert len(children) == 2
    assert all(job.status is JobStatus.completed for job in children)


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_profile_worker_recovers_loss_around_atomic_finalization(
    monkeypatch: pytest.MonkeyPatch,
    crash_after_commit: bool,
    eager_celery,
) -> None:
    """Recovery immediately around commit must return one stable outcome."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Finalize", query="finalize liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None

    class Adapter:
        async def search(self, _: SearchProfile) -> YouTubeSearchBatch:
            return YouTubeSearchBatch(
                payloads=[
                    _payload(
                        "finalize-video",
                        "FINALIZE LIVESET",
                        5_400,
                    )
                ],
                next_page_token="NEXT",
            )

    original = repository.finalize_profile_job
    lost = True

    def lose_around_commit(*args, **kwargs):
        nonlocal lost
        if not lost:
            return original(*args, **kwargs)
        lost = False
        if crash_after_commit:
            original(*args, **kwargs)
        raise _LostWorker()

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        repository,
        "finalize_profile_job",
        lose_around_commit,
    )
    _use_eager_process_worker(monkeypatch, repository)

    with pytest.raises(_LostWorker):
        youtube_poller.run_youtube_profile.run(str(parent.id))
    if not crash_after_commit:
        _stale_parent(repository, parent.id)

    result = youtube_poller.run_youtube_profile.run(str(parent.id))

    if crash_after_commit:
        assert result == {
            "result_count": 1,
            "discard_count": 0,
            "duplicate_count": 0,
        }
    else:
        assert result is None
    assert {
        key: repository.get_job(parent.id).details[key]
        for key in (
            "result_count",
            "discard_count",
            "duplicate_count",
        )
    } == {
        "result_count": 1,
        "discard_count": 0,
        "duplicate_count": 0,
    }
    assert repository.get_profile(profile.id).next_page_token == "NEXT"
    assert len(
        [
            job
            for job in repository.jobs.values()
            if job.details.get("profile_job_id") == str(parent.id)
        ]
    ) == 1


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (
            ProviderPayloadError("youtube_invalid_response"),
            "youtube_invalid_response",
        ),
        (RuntimeError("secret database details"), "youtube_worker_error"),
    ],
)
def test_profile_worker_terminalizes_permanent_and_unknown_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    error_code: str,
) -> None:
    """A claimed parent must never remain processing after an exception."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Terminal", query="terminal liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None

    class Adapter:
        async def search(self, _: SearchProfile) -> YouTubeSearchBatch:
            raise error

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )

    with pytest.raises(type(error)):
        youtube_poller.run_youtube_profile.run(str(parent.id))

    failed = repository.get_job(parent.id)
    assert failed.status is JobStatus.failed
    assert failed.error_code == error_code
    assert failed.error_message == error_code


def test_profile_worker_terminalizes_when_profile_was_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active profile job must prevent deletion and retain its association."""
    from app.repositories.base import ActiveProfileJobsError

    repository = InMemoryRepository()
    profile = repository.create_profile(
        SearchProfileCreate(name="Deleted", query="deleted liveset")
    )
    parent = repository.queue_profile(profile.id)
    assert parent is not None

    with pytest.raises(
        ActiveProfileJobsError,
        match="active import job",
    ):
        repository.delete_profile(profile.id)

    active = repository.get_job(parent.id)
    assert active.status is JobStatus.queued
    assert active.profile_id == profile.id
    assert repository.get_profile(profile.id) is not None


def test_direct_url_task_processes_metadata_with_configured_lease(
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """Direct metadata imports must use the common pipeline and lease setting."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    payload = _payload("direct-video", "DIRECT LIVESET", 5_400)
    job = repository.create_job(
        url=payload.canonical_url,
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    claims: list[int] = []
    original_claim = repository.claim_job

    def record_claim(job_id, *, claim_ttl_seconds=300):
        claims.append(claim_ttl_seconds)
        return original_claim(
            job_id,
            claim_ttl_seconds=claim_ttl_seconds,
        )

    class Adapter:
        async def fetch(self, url: str) -> RawSetPayload:
            assert url == payload.canonical_url
            current = repository.get_job(job.id)
            assert current.status is JobStatus.processing
            assert current.started_at is not None
            return payload

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            job_claim_ttl_seconds=17,
        ),
    )
    monkeypatch.setattr(repository, "claim_job", record_claim)
    _use_eager_process_worker(monkeypatch, repository)

    result = youtube_poller.import_url.run(str(job.id))

    assert result is None
    assert claims == [17]
    assert repository.get_job(job.id).status is JobStatus.completed


def test_duplicate_direct_delivery_exits_before_provider_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-owner delivery must not fetch or mutate another worker's job."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    job = repository.create_job(
        url="https://www.youtube.com/watch?v=owned-direct",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )
    owner = repository.claim_job(job.id)
    assert owner is not None
    fetched = False

    class Adapter:
        async def fetch(self, _: str) -> RawSetPayload:
            nonlocal fetched
            fetched = True
            raise AssertionError("duplicate delivery fetched provider")

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )
    scheduled: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        youtube_poller.import_url,
        "apply_async",
        lambda args, countdown: scheduled.append(tuple(args)),
    )

    result = youtube_poller.import_url.run(str(job.id))

    current = repository.get_job(job.id)
    assert result is None
    assert fetched is False
    assert current.status is JobStatus.processing
    assert current.started_at == owner.started_at
    assert scheduled == [(str(job.id),)]


def test_direct_url_task_terminalizes_unknown_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected direct-task failures must not strand a claimed job."""
    from app.workers import youtube_poller

    repository = InMemoryRepository()
    job = repository.create_job(
        url="https://www.youtube.com/watch?v=direct-error",
        source=SetSource.youtube,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, _: str) -> RawSetPayload:
            raise RuntimeError("secret provider response")

    monkeypatch.setattr(
        youtube_poller,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        youtube_poller,
        "get_youtube_adapter",
        lambda: Adapter(),
    )

    with pytest.raises(RuntimeError, match="secret provider response"):
        youtube_poller.import_url.run(str(job.id))

    failed = repository.get_job(job.id)
    assert failed.status is JobStatus.failed
    assert failed.error_code == "youtube_worker_error"
    assert failed.error_message == "youtube_worker_error"
