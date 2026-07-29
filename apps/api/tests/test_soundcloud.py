import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from celery.exceptions import Retry

from app.core.config import Settings
from app.repository import InMemoryRepository
from app.schemas.import_job import JobStatus, JobType
from app.schemas.set import SetSource
from app.services.provider import (
    ProviderPayloadError,
    ProviderTemporaryError,
    ProviderValidationError,
)


VALID_URL = "https://soundcloud.com/syco23/ritual-session"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _live_soundcloud_worker_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker exercises opt into live mode without changing the safe default."""
    from app.workers import soundcloud_importer

    monkeypatch.setattr(
        soundcloud_importer,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
        ),
    )


class FakeStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, _: int = -1) -> bytes:
        payload, self.payload = self.payload, b""
        return payload


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = returncode
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


class FakeProcessRunner:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.argv: tuple[str, ...] = ()
        self.shell = False
        self.stdout = None
        self.stderr = None

    async def __call__(self, *argv: str, stdout, stderr) -> FakeProcess:
        self.argv = argv
        self.stdout = stdout
        self.stderr = stderr
        return self.process


def _fixture_bytes() -> bytes:
    return (FIXTURES / "soundcloud.json").read_bytes()


@pytest.mark.parametrize(
    "url",
    [
        "http://soundcloud.com/crew/set",
        "https://evil.example/?next=https://soundcloud.com/crew/set",
        "https://soundcloud.com/",
        "https://soundcloud.com/crew/sets/playlist",
        "https://api.soundcloud.com/crew/set",
        "https://user:secret@soundcloud.com/crew/set",
        "https://soundcloud.com:443/crew/set",
        "https://soundcloud.com/crew/likes",
        "https://soundcloud.com/crew/reposts",
        "https://soundcloud.com/crew%2Fother/set",
        "https://soundcloud.com/crew/set?next=https%3A%2F%2Fevil.example",
        "https://soundcloud.com/crew/set?next=%2F%2F%5B",
    ],
)
def test_rejects_unsafe_soundcloud_urls(url: str) -> None:
    """Relaxing the track-only URL boundary could admit redirects or playlists."""
    from app.services.soundcloud import validate_soundcloud_url

    with pytest.raises(ProviderValidationError):
        validate_soundcloud_url(url)


def test_valid_url_is_normalized_without_fragment() -> None:
    """Persisting fragments would give one track multiple import identities."""
    from app.services.soundcloud import validate_soundcloud_url

    normalized = validate_soundcloud_url(
        "https://www.soundcloud.com/crew/set?si=share-token#comments"
    )

    assert normalized == (
        "https://www.soundcloud.com/crew/set?si=share-token"
    )


def test_yt_dlp_never_uses_shell_or_download_flags() -> None:
    """A shell, playlist, or output argument could execute or write untrusted data."""
    from app.services.soundcloud import SoundCloudAdapter

    fake_process = FakeProcessRunner(
        FakeProcess(stdout=_fixture_bytes())
    )

    asyncio.run(
        SoundCloudAdapter(
            process_runner=fake_process,
            yt_dlp_bin="/usr/local/bin/yt-dlp",
        ).fetch(VALID_URL)
    )

    assert fake_process.shell is False
    assert fake_process.argv == (
        "/usr/local/bin/yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        VALID_URL,
    )
    assert "-o" not in fake_process.argv
    assert fake_process.stdout is asyncio.subprocess.PIPE
    assert fake_process.stderr is asyncio.subprocess.PIPE


def test_timeout_after_thirty_seconds_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged extractor must not retain a worker or child process indefinitely."""
    from app.services import soundcloud

    process = FakeProcess(stdout=_fixture_bytes())
    runner = FakeProcessRunner(process)
    requested_timeouts: list[float] = []

    class ImmediateTimeout:
        async def __aenter__(self):
            raise TimeoutError

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    def timeout(seconds: float) -> ImmediateTimeout:
        requested_timeouts.append(seconds)
        return ImmediateTimeout()

    monkeypatch.setattr(soundcloud.asyncio, "timeout", timeout)

    with pytest.raises(
        ProviderTemporaryError,
        match="^soundcloud_timeout$",
    ):
        asyncio.run(
            soundcloud.SoundCloudAdapter(
                process_runner=runner
            ).fetch(VALID_URL)
        )

    assert requested_timeouts == [30]
    assert process.killed is True
    assert process.waited is True


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_rejects_output_larger_than_one_mebibyte(
    stream_name: str,
) -> None:
    """Either unbounded child stream could exhaust worker memory."""
    from app.services.soundcloud import SoundCloudAdapter

    oversized = b"x" * (1024 * 1024 + 1)
    process = FakeProcess(
        stdout=(
            oversized
            if stream_name == "stdout"
            else _fixture_bytes()
        ),
        stderr=oversized if stream_name == "stderr" else b"",
    )

    with pytest.raises(
        ProviderPayloadError,
        match="^soundcloud_output_too_large$",
    ):
        asyncio.run(
            SoundCloudAdapter(
                process_runner=FakeProcessRunner(process)
            ).fetch(VALID_URL)
        )

    assert process.killed is True
    assert process.waited is True


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json",
        b"[]",
        b'{"id": "first"} {"id": "second"}',
        b'{"id": "track", "duration": 1e999}',
    ],
)
def test_malformed_output_is_a_safe_payload_failure(
    stdout: bytes,
) -> None:
    """Malformed extractor output must fail permanently without leaking parser errors."""
    from app.services.soundcloud import SoundCloudAdapter

    with pytest.raises(
        ProviderPayloadError,
        match="^soundcloud_invalid_response$",
    ):
        asyncio.run(
            SoundCloudAdapter(
                process_runner=FakeProcessRunner(
                    FakeProcess(stdout=stdout)
                )
            ).fetch(VALID_URL)
        )


def test_temporary_process_failure_is_retryable() -> None:
    """A transient extractor exit must enter the retry schedule."""
    from app.services.soundcloud import SoundCloudAdapter

    with pytest.raises(
        ProviderTemporaryError,
        match="^soundcloud_process_error$",
    ):
        asyncio.run(
            SoundCloudAdapter(
                process_runner=FakeProcessRunner(
                    FakeProcess(
                        stdout=b"",
                        stderr=b"temporary upstream error",
                        returncode=1,
                    )
                )
            ).fetch(VALID_URL)
        )


def test_soundcloud_payload_is_normalized() -> None:
    """Dropping provider field mappings would corrupt common-pipeline metadata."""
    from app.services.soundcloud import SoundCloudAdapter

    payload = asyncio.run(
        SoundCloudAdapter(
            process_runner=FakeProcessRunner(
                FakeProcess(stdout=_fixture_bytes())
            )
        ).fetch(VALID_URL)
    )

    assert payload.source is SetSource.soundcloud
    assert payload.source_id == "1876543210"
    assert payload.canonical_url == VALID_URL
    assert payload.title == "SYCO23 LIVESET @ RITUAL FLOOR"
    assert payload.description == "Recorded at Hangar 23, Berlin."
    assert payload.duration_seconds == 5400
    assert payload.published_at == datetime(2025, 1, 1, tzinfo=UTC)
    assert payload.primary_image_url == (
        "https://i1.sndcdn.com/artworks-ritual-t500x500.jpg"
    )
    assert payload.raw_payload["extractor_key"] == "Soundcloud"


@contextmanager
def task_request(task, retries: int):
    task.push_request(
        retries=retries,
        called_directly=False,
        is_eager=True,
    )
    try:
        yield
    finally:
        task.pop_request()


def test_worker_claims_before_fetch_and_uses_common_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """Fetching before ownership or bypassing the pipeline could duplicate imports."""
    from app.workers import soundcloud_importer
    from app.workers import normalize_worker

    repository = InMemoryRepository()
    raw = json.loads(_fixture_bytes())
    expected = None
    fetched = False
    job = repository.create_job(
        url=VALID_URL,
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, url: str):
            nonlocal fetched, expected
            fetched = True
            current = repository.get_job(job.id)
            assert current.status is JobStatus.processing
            assert current.started_at is not None
            from app.services.normalizer import normalize_raw_payload

            expected = normalize_raw_payload("soundcloud", raw)
            return expected

    monkeypatch.setattr(
        soundcloud_importer,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        soundcloud_importer,
        "get_soundcloud_adapter",
        lambda: Adapter(),
    )
    monkeypatch.setattr(
        soundcloud_importer,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            provider_mode="live",
            job_claim_ttl_seconds=17,
        ),
    )

    result = soundcloud_importer.import_soundcloud.run(str(job.id))

    assert fetched is True
    assert expected is not None
    assert result is None
    assert repository.get_job(job.id).status is JobStatus.completed


def test_non_owner_worker_does_not_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competing delivery must not fetch or mutate another worker's job."""
    from app.workers import soundcloud_importer

    repository = InMemoryRepository()
    job = repository.create_job(
        url=VALID_URL,
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    owner = repository.claim_job(job.id)
    assert owner is not None

    class Adapter:
        async def fetch(self, _: str):
            raise AssertionError("non-owner fetched provider")

    monkeypatch.setattr(
        soundcloud_importer,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        soundcloud_importer,
        "get_soundcloud_adapter",
        lambda: Adapter(),
    )
    scheduled: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        soundcloud_importer.import_soundcloud,
        "apply_async",
        lambda args, countdown: scheduled.append(tuple(args)),
    )

    assert soundcloud_importer.import_soundcloud.run(str(job.id)) is None
    current = repository.get_job(job.id)
    assert current.status is JobStatus.processing
    assert current.started_at == owner.started_at
    assert scheduled == [(str(job.id),)]


def test_invalid_url_fails_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent validation failures must not consume transient retry capacity."""
    from app.workers import soundcloud_importer

    repository = InMemoryRepository()
    job = repository.create_job(
        url="https://soundcloud.com/crew/sets/playlist",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    monkeypatch.setattr(
        soundcloud_importer,
        "get_worker_repository",
        lambda: repository,
    )

    with pytest.raises(ProviderValidationError):
        soundcloud_importer.import_soundcloud.run(str(job.id))

    failed = repository.get_job(job.id)
    assert failed.status is JobStatus.failed
    assert failed.error_code == "soundcloud_invalid_url"
    assert failed.next_retry_at is None


def test_timeout_retries_then_dead_letters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporary extractor failures must retain the common retry/dead-letter policy."""
    from app.workers import soundcloud_importer

    repository = InMemoryRepository()
    job = repository.create_job(
        url=VALID_URL,
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )

    class Adapter:
        async def fetch(self, _: str):
            raise ProviderTemporaryError("soundcloud_timeout")

    monkeypatch.setattr(
        soundcloud_importer,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        soundcloud_importer,
        "get_soundcloud_adapter",
        lambda: Adapter(),
    )

    for retries, delay in ((0, 5), (1, 30), (2, 120)):
        with task_request(soundcloud_importer.import_soundcloud, retries):
            with pytest.raises(Retry) as retry:
                soundcloud_importer.import_soundcloud.run(str(job.id))
        assert retry.value.when == delay
        assert repository.get_job(job.id).status is JobStatus.retry

    with task_request(soundcloud_importer.import_soundcloud, 3):
        with pytest.raises(
            ProviderTemporaryError,
            match="soundcloud_timeout",
        ):
            soundcloud_importer.import_soundcloud.run(str(job.id))

    exhausted = repository.get_job(job.id)
    assert exhausted.status is JobStatus.dead_letter
    assert exhausted.error_code == "retry_exhausted"
