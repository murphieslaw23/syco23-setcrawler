import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.repository import InMemoryRepository
from app.schemas import JobStatus, JobType, ReviewStatus, SetSource
from app.services.ftm import FTMAdapter
from app.services.import_pipeline import process_payload
from app.services.soundcloud import SoundCloudAdapter
from app.services.youtube import YouTubeAdapter


FIXTURES = Path(__file__).parent / "fixtures"


class _FixtureProcess:
    def __init__(self, payload: bytes) -> None:
        self.stdout = _FixtureStream(payload)
        self.stderr = _FixtureStream(b"")

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        return None


class _FixtureStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, _: int) -> bytes:
        payload, self.payload = self.payload, b""
        return payload


class FixtureDispatcher:
    """Offline provider fixture runner that only passes normalized metadata onward."""

    def __init__(self, repository: InMemoryRepository) -> None:
        self.repository = repository

    def run_source(self, source: SetSource):
        payload = self._payload_for(source)
        job = self.repository.create_job(
            url=payload.canonical_url,
            source=source,
            job_type=JobType.url_import,
            details={"fixture": True},
        )
        process_payload(self.repository, job.id, payload)
        return job

    def _payload_for(self, source: SetSource):
        if source is SetSource.youtube:
            videos = json.loads((FIXTURES / "youtube_videos.json").read_text())
            return YouTubeAdapter._normalize_video(videos["items"][0])
        if source is SetSource.soundcloud:
            raw = (FIXTURES / "soundcloud.json").read_bytes()

            async def runner(*_args, **_kwargs):
                return _FixtureProcess(raw)

            return asyncio.run(
                SoundCloudAdapter(process_runner=runner).fetch(
                    "https://soundcloud.com/syco23/fixture-set"
                )
            )
        html = (FIXTURES / "ftm_set.html").read_text()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=("User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else html),
            )
        )
        return asyncio.run(
            FTMAdapter(enabled=True, transport=transport).fetch(
                "https://freeteknomusic.org/sets/fixture-set"
            )
        )


@pytest.fixture
def repository() -> InMemoryRepository:
    return InMemoryRepository.seeded()


@pytest.fixture
def fixture_dispatcher(repository: InMemoryRepository) -> FixtureDispatcher:
    return FixtureDispatcher(repository)


@pytest.mark.parametrize(
    "source",
    [SetSource.youtube, SetSource.soundcloud, SetSource.freeteknomusic],
)
def test_fixture_provider_reaches_review_inbox(
    source: SetSource,
    fixture_dispatcher: FixtureDispatcher,
    repository: InMemoryRepository,
) -> None:
    """Every provider fixture normalizes offline and remains editorially gated."""
    job = fixture_dispatcher.run_source(source)

    completed = repository.get_job(job.id)

    assert completed is not None
    assert completed.status is JobStatus.completed
    assert completed.result_set_id is not None
    created = repository.get_set(completed.result_set_id)
    assert created is not None
    assert created.review_status is ReviewStatus.inbox
