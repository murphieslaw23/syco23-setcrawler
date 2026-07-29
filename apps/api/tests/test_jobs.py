from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry

from app.core.config import Settings
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.repository import InMemoryRepository
from app.schemas.import_job import (
    ImportJob,
    ImportJobPatch,
    JobStatus,
    JobType,
    validate_job_transition,
)
from app.schemas.set import ReviewStatus, SetSource
from app.services.import_pipeline import process_payload, retry_delay
from app.services.normalizer import RawSetPayload


PAYLOAD = RawSetPayload(
    source=SetSource.soundcloud,
    source_id="syco23-ritual-session",
    canonical_url="https://soundcloud.com/syco23/ritual-session",
    title="SYCO23 LIVESET @ RITUAL FLOOR",
    description="Recorded at Hangar 23, Berlin.",
    duration_seconds=5_400,
    raw_payload={"provider": "soundcloud"},
)


@contextmanager
def task_request(task, retries: int) -> Iterator[None]:
    task.push_request(
        retries=retries,
        called_directly=False,
        is_eager=True,
    )
    try:
        yield
    finally:
        task.pop_request()


def test_job_contract_supports_operational_states() -> None:
    """A provider-disabled URL import must retain its blocked outcome details."""
    job = ImportJob(
        url="https://soundcloud.com/syco23/ritual",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
        status=JobStatus.blocked,
        attempt_count=1,
        error_code="provider_disabled",
        error_message="Provider is disabled",
    )

    assert job.status is JobStatus.blocked
    assert job.result_set_id is None


def test_job_transition_rejects_completed_to_processing() -> None:
    """A completed import is terminal and cannot be returned to a worker."""
    with pytest.raises(ValueError, match="completed"):
        validate_job_transition(JobStatus.completed, JobStatus.processing)


def test_job_transition_allows_queued_to_processing() -> None:
    """A queued import may be claimed by a provider worker."""
    validate_job_transition(JobStatus.queued, JobStatus.processing)


def test_processing_same_payload_twice_returns_same_set(repository) -> None:
    """Removing duplicate detection would persist two sets for one recording."""
    first_job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    second_job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )

    first = process_payload(repository, first_job.id, PAYLOAD)
    second = process_payload(repository, second_job.id, PAYLOAD)

    assert first == second
    assert repository.get_job(second_job.id).details["duplicate"] is True


def test_completed_job_redelivery_returns_existing_outcome(repository) -> None:
    """Late acknowledgement redelivery must not reprocess a completed job."""
    persisted_job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    persisted_set_id = process_payload(
        repository,
        persisted_job.id,
        PAYLOAD,
    )
    discarded_payload = PAYLOAD.model_copy(
        update={
            "source_id": "short",
            "canonical_url": "https://soundcloud.com/syco23/short",
            "title": "Short clip",
            "duration_seconds": 30,
        }
    )
    discarded_job = repository.create_job(
        url=discarded_payload.canonical_url,
        source=discarded_payload.source,
        job_type=JobType.url_import,
    )
    assert process_payload(
        repository,
        discarded_job.id,
        discarded_payload,
    ) is None
    set_count = len(repository.sets)

    persisted_redelivery = process_payload(
        repository,
        persisted_job.id,
        PAYLOAD,
    )
    discarded_redelivery = process_payload(
        repository,
        discarded_job.id,
        discarded_payload,
    )

    assert persisted_redelivery == persisted_set_id
    assert discarded_redelivery is None
    assert len(repository.sets) == set_count


def test_processing_delivery_exits_without_taking_over(repository) -> None:
    """A competing delivery must not process work owned by another worker."""
    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    claimed = repository.transition_job(
        job.id,
        ImportJobPatch(
            status=JobStatus.processing,
            attempt_count=1,
        ),
    )
    assert claimed is not None

    result = process_payload(repository, job.id, PAYLOAD)

    current = repository.get_job(job.id)
    assert result is None
    assert current.status is JobStatus.processing
    assert current.attempt_count == 1
    assert len(repository.sets) == 6


def test_active_processing_claim_is_not_reclaimed(repository) -> None:
    """A delivery inside the claim lease must retain exclusive ownership."""
    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    active = repository.claim_job(job.id)
    assert active is not None

    competing = repository.claim_job(
        job.id,
        claim_ttl_seconds=300,
    )

    current = repository.get_job(job.id)
    assert competing is None
    assert current.status is JobStatus.processing
    assert current.attempt_count == 1
    assert "reclaim_count" not in current.details


def test_expired_processing_claim_is_reclaimed_with_metadata(
    repository,
) -> None:
    """A worker lost beyond the TTL must not strand durable work."""
    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
        details={"origin": "lease-test"},
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None
    expired_at = datetime.now(UTC) - timedelta(seconds=301)
    repository.jobs[job.id] = claimed.model_copy(
        update={
            "started_at": expired_at,
            "next_retry_at": datetime.now(UTC) + timedelta(hours=1),
        }
    )

    not_expired_for_custom_ttl = repository.claim_job(
        job.id,
        claim_ttl_seconds=600,
    )
    reclaimed = repository.claim_job(job.id)

    assert (
        Settings(job_claim_ttl_seconds=600).job_claim_ttl_seconds
        == 600
    )
    assert not_expired_for_custom_ttl is None
    assert reclaimed is not None
    assert reclaimed.status is JobStatus.processing
    assert reclaimed.attempt_count == 2
    assert reclaimed.started_at > expired_at
    assert reclaimed.next_retry_at is None
    assert reclaimed.details["origin"] == "lease-test"
    assert reclaimed.details["reclaim_count"] == 1
    assert reclaimed.details["reclaimed_started_at"] == (
        expired_at.isoformat()
    )
    assert reclaimed.details["last_reclaimed_at"]


def test_lost_worker_redelivery_reaches_one_terminal_result(
    repository,
) -> None:
    """A stale redelivery must finish once without duplicating the set."""
    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None
    repository.jobs[job.id] = claimed.model_copy(
        update={
            "started_at": datetime.now(UTC)
            - timedelta(seconds=301),
        }
    )
    initial_set_count = len(repository.sets)

    recovered = process_payload(repository, job.id, PAYLOAD)
    late_redelivery = process_payload(
        repository,
        job.id,
        PAYLOAD,
    )

    terminal = repository.get_job(job.id)
    assert recovered is not None
    assert late_redelivery == recovered
    assert terminal.status is JobStatus.completed
    assert terminal.result_set_id == recovered
    assert terminal.attempt_count == 2
    assert terminal.details["reclaim_count"] == 1
    assert len(repository.sets) == initial_set_count + 1


def test_losing_atomic_claim_exits_without_invalid_transition() -> None:
    """A worker losing the queued-to-processing race must exit cleanly."""

    class LosingClaimRepository(InMemoryRepository):
        def claim_job(
            self,
            job_id: UUID,
            *,
            claim_ttl_seconds: int = 300,
        ) -> ImportJob | None:
            current = self.get_job(job_id)
            if current and current.status is JobStatus.queued:
                self.transition_job(
                    job_id,
                    ImportJobPatch(
                        status=JobStatus.processing,
                        attempt_count=1,
                    ),
                )
            return None

    repository = LosingClaimRepository()
    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )

    result = process_payload(repository, job.id, PAYLOAD)

    assert result is None
    assert repository.get_job(job.id).status is JobStatus.processing
    assert repository.sets == {}


def test_high_score_processing_stays_in_inbox_with_audit_metadata(
    repository,
) -> None:
    """A high heuristic score must not bypass explicit editorial publishing."""
    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )

    set_id = process_payload(repository, job.id, PAYLOAD)

    assert set_id is not None
    detail = repository.get_set(set_id)
    assert detail.review_status is ReviewStatus.inbox
    assert detail.raw_payload["duplicate_fingerprint"]
    assert detail.raw_payload["score_reasons"]
    assert repository.get_job(job.id).status is JobStatus.completed


@pytest.mark.parametrize("attempt,delay", [(1, 5), (2, 30), (3, 120)])
def test_retry_delay(attempt: int, delay: int) -> None:
    """Changing retry backoff must not overload a failing provider."""
    assert retry_delay(attempt) == delay


class RecordingDispatcher:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.calls: list[tuple[str, object]] = []

    def _record(self, action: str, job: ImportJob) -> None:
        assert self.repository.get_job(job.id) is not None
        self.calls.append((action, job))

    def dispatch_url(self, job: ImportJob) -> None:
        self._record("url", job)

    def dispatch_profile(self, job: ImportJob) -> None:
        self._record("profile", job)

    def retry(self, job: ImportJob) -> None:
        self._record("retry", job)


def _live_settings() -> Settings:
    return Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="live",
    )


def test_url_dispatch_observes_durable_job_before_enqueue(repository) -> None:
    """Dispatching before persistence could lose accepted API work."""
    dispatcher = RecordingDispatcher(repository)
    client = TestClient(
        create_app(repository, settings=_live_settings(), dispatcher=dispatcher)
    )

    response = client.post(
        "/imports/url",
        json={"url": PAYLOAD.canonical_url},
    )

    assert response.status_code == 202
    assert dispatcher.calls == [
        ("url", repository.get_job(UUID(response.json()["id"])))
    ]


def test_profile_run_dispatches_its_durable_job(repository) -> None:
    """A manual profile run must enqueue the row returned by the repository."""
    dispatcher = RecordingDispatcher(repository)
    client = TestClient(
        create_app(repository, settings=_live_settings(), dispatcher=dispatcher)
    )
    profile = repository.list_profiles()[0]

    response = client.post(f"/search-profiles/{profile.id}/run")

    assert response.status_code == 202
    assert dispatcher.calls == [
        ("profile", repository.get_job(UUID(response.json()["id"])))
    ]


def test_production_dispatcher_delays_provider_and_profile_tasks(
    repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing a task name or omitting the durable job id would orphan work."""
    from app.workers.dispatch import JobDispatcher, celery_app

    delayed: list[tuple[str, str]] = []

    class Signature:
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name

        def delay(self, job_id: str) -> None:
            delayed.append((self.task_name, job_id))

    monkeypatch.setattr(
        celery_app,
        "signature",
        lambda task_name: Signature(task_name),
    )
    url_job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    profile = repository.list_profiles()[0]
    profile_job = repository.queue_profile(profile.id)
    assert profile_job is not None

    dispatcher = JobDispatcher()
    dispatcher.dispatch_url(url_job)
    dispatcher.dispatch_profile(profile_job)
    dispatcher.retry(url_job)

    assert delayed == [
        (
            "app.workers.soundcloud_importer.import_url",
            str(url_job.id),
        ),
        (
            "app.workers.youtube_poller.poll_profile",
            str(profile_job.id),
        ),
        (
            "app.workers.soundcloud_importer.import_url",
            str(url_job.id),
        ),
    ]


def test_admin_retry_creates_new_durable_job_and_rejects_active_job(
    repository,
) -> None:
    """Manual retries must preserve the terminal attempt and reject active work."""
    dispatcher = RecordingDispatcher(repository)
    client = TestClient(
        create_app(repository, settings=_live_settings(), dispatcher=dispatcher)
    )
    failed = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    repository.transition_job(
        failed.id,
        ImportJobPatch(status=JobStatus.failed),
    )
    active = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )

    retried = client.post(f"/imports/queue/{failed.id}/retry")
    rejected = client.post(f"/imports/queue/{active.id}/retry")

    assert retried.status_code == 202
    assert retried.json()["id"] != str(failed.id)
    assert retried.json()["details"]["retry_of_job_id"] == str(failed.id)
    assert dispatcher.calls == [
        ("retry", repository.get_job(UUID(retried.json()["id"])))
    ]
    assert rejected.status_code == 409


def test_dead_letter_retry_and_retry_errors_are_authorized(repository) -> None:
    """Dead-letter retries are admin-only and missing jobs remain distinguishable."""
    dispatcher = RecordingDispatcher(repository)
    client = TestClient(
        create_app(repository, settings=_live_settings(), dispatcher=dispatcher)
    )
    dead = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    repository.transition_job(
        dead.id,
        ImportJobPatch(status=JobStatus.processing),
    )
    repository.transition_job(
        dead.id,
        ImportJobPatch(status=JobStatus.retry),
    )
    repository.transition_job(
        dead.id,
        ImportJobPatch(status=JobStatus.dead_letter),
    )

    forbidden = client.post(
        f"/imports/queue/{dead.id}/retry",
        headers={"X-Local-Role": "viewer"},
    )
    missing = client.post(f"/imports/queue/{uuid4()}/retry")
    retried = client.post(f"/imports/queue/{dead.id}/retry")

    assert forbidden.status_code == 403
    assert missing.status_code == 404
    assert retried.status_code == 202
    assert retried.json()["details"]["retry_of_job_id"] == str(dead.id)
    assert dispatcher.calls == [
        ("retry", repository.get_job(UUID(retried.json()["id"])))
    ]


def test_terminal_retry_reuses_existing_active_retry_job(repository) -> None:
    """Repeated admin retry requests must not enqueue duplicate active work."""
    dispatcher = RecordingDispatcher(repository)
    client = TestClient(
        create_app(repository, settings=_live_settings(), dispatcher=dispatcher)
    )
    failed = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    repository.transition_job(failed.id, ImportJobPatch(status=JobStatus.failed))

    first = client.post(f"/imports/queue/{failed.id}/retry")
    second = client.post(f"/imports/queue/{failed.id}/retry")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert len(dispatcher.calls) == 1


def test_repository_retry_creation_is_atomic_under_competing_requests(
    repository,
) -> None:
    """Removing the retry lock would create two queued children for one parent."""
    failed = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    repository.transition_job(failed.id, ImportJobPatch(status=JobStatus.failed))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: repository.create_retry_job(failed.id),
                range(2),
            )
        )

    assert all(outcome is not None for outcome in outcomes)
    jobs = [outcome[0] for outcome in outcomes if outcome is not None]
    created = [outcome[1] for outcome in outcomes if outcome is not None]
    assert len({job.id for job in jobs}) == 1
    assert created.count(True) == 1
    assert len(repository.jobs) == 2


def test_celery_is_json_only_and_routes_normalization_work() -> None:
    """Unsafe serializers or a wrong queue would break worker isolation."""
    from app.workers.celery_app import celery_app

    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_routes == {
        "app.workers.youtube_poller.*": {"queue": "youtube"},
        "app.workers.soundcloud_importer.*": {
            "queue": "soundcloud"
        },
        "app.workers.ftm_scraper.*": {"queue": "ftm"},
        "app.workers.normalize_worker.*": {"queue": "process"},
    }


def test_normalize_task_runs_eager_without_provider_traffic(
    repository,
    monkeypatch: pytest.MonkeyPatch,
    eager_celery,
) -> None:
    """The normalization task must consume supplied metadata without provider I/O."""
    from app.workers import normalize_worker
    from app.workers.dispatch import JobDispatcher

    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        JobDispatcher,
        "dispatch_url",
        lambda *args: pytest.fail("provider URL task invoked"),
    )
    monkeypatch.setattr(
        JobDispatcher,
        "dispatch_profile",
        lambda *args: pytest.fail("provider profile task invoked"),
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None and claimed.started_at is not None

    result = normalize_worker.process_raw_payload.delay(
        str(job.id),
        PAYLOAD.model_dump(mode="json"),
        claimed.started_at.isoformat(),
    )

    assert result.get() == str(repository.get_job(job.id).result_set_id)


def test_retryable_worker_failures_persist_backoff_and_dead_letter(
    repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing retry transitions would strand a job in processing."""
    from app.services import import_pipeline
    from app.workers import normalize_worker

    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None and claimed.started_at is not None
    owner_token = claimed.started_at.isoformat()

    def unavailable_score(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        import_pipeline,
        "calculate_set_score",
        unavailable_score,
    )
    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )

    for retries, delay, attempt_count in (
        (0, 5, 1),
        (1, 30, 2),
        (2, 120, 3),
    ):
        if retries:
            current = repository.get_job(job.id)
            repository.jobs[job.id] = current.model_copy(
                update={
                    "next_retry_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ),
                }
            )
        with task_request(
            normalize_worker.process_raw_payload,
            retries,
        ):
            with pytest.raises(Retry) as retry:
                normalize_worker.process_raw_payload.run(
                    str(job.id),
                    PAYLOAD.model_dump(mode="json"),
                    owner_token,
                )
        current = repository.get_job(job.id)
        assert retry.value.when == delay
        assert current.status is JobStatus.retry
        assert current.attempt_count == attempt_count
        assert current.error_code == "processing_error"
        assert current.error_message == "database unavailable"
        assert current.next_retry_at is not None

    current = repository.get_job(job.id)
    repository.jobs[job.id] = current.model_copy(
        update={
            "next_retry_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    with task_request(normalize_worker.process_raw_payload, 3):
        with pytest.raises(RuntimeError, match="database unavailable"):
            normalize_worker.process_raw_payload.run(
                str(job.id),
                PAYLOAD.model_dump(mode="json"),
                owner_token,
            )

    exhausted = repository.get_job(job.id)
    assert exhausted.status is JobStatus.dead_letter
    assert exhausted.attempt_count == 4
    assert exhausted.error_code == "retry_exhausted"
    assert exhausted.error_message == "database unavailable"
    assert exhausted.next_retry_at is None


def test_invalid_payload_fails_permanently_without_retry(
    repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed metadata must fail durably instead of entering retry."""
    from app.workers import normalize_worker

    job = repository.create_job(
        url=PAYLOAD.canonical_url,
        source=PAYLOAD.source,
        job_type=JobType.url_import,
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None and claimed.started_at is not None
    invalid_payload = PAYLOAD.model_dump(mode="json")
    del invalid_payload["raw_payload"]
    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )

    with task_request(normalize_worker.process_raw_payload, 0):
        with pytest.raises(ValidationError):
            normalize_worker.process_raw_payload.run(
                str(job.id),
                invalid_payload,
                claimed.started_at.isoformat(),
            )

    failed = repository.get_job(job.id)
    assert failed.status is JobStatus.failed
    assert failed.attempt_count == 1
    assert failed.error_code == "invalid_payload"
    assert failed.error_message
    assert failed.next_retry_at is None
