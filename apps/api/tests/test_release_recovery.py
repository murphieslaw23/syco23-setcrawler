from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.repositories.memory import InMemoryRepository
from app.schemas import ImportJobPatch, JobStatus, JobType, SetSource
from app.workers import normalize_worker
from app.workers.normalize_worker import _record_retry
from app.workers.recovery import claim_or_reschedule


def _processing_job(
    repository: InMemoryRepository,
    *,
    suffix: str = "recovery",
):
    job = repository.create_job(
        url=f"https://soundcloud.com/syco23/{suffix}",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    claimed = repository.claim_job(job.id)
    assert claimed is not None
    assert claimed.started_at is not None
    return job, claimed


def _retry_job(
    repository: InMemoryRepository,
    *,
    next_retry_at: datetime,
    suffix: str = "retry",
):
    job, claimed = _processing_job(repository, suffix=suffix)
    retried = repository.transition_claimed_job(
        job.id,
        claimed.started_at,
        ImportJobPatch(
            status=JobStatus.retry,
            next_retry_at=next_retry_at,
            error_code="temporary",
            error_message="temporary",
        ),
    )
    assert retried is not None
    return retried


def test_future_retry_cannot_be_claimed_or_redriven() -> None:
    repository = InMemoryRepository()
    job = _retry_job(
        repository,
        next_retry_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert repository.claim_job(job.id) is None
    assert (
        repository.list_recoverable_jobs(
            claim_ttl_seconds=300,
            limit=50,
        )
        == []
    )


def test_due_retry_and_stale_processing_are_recoverable() -> None:
    repository = InMemoryRepository()
    due = _retry_job(
        repository,
        next_retry_at=datetime.now(UTC) - timedelta(seconds=1),
        suffix="due",
    )
    stale_job, stale_claim = _processing_job(repository, suffix="stale")
    repository.jobs[stale_job.id] = stale_claim.model_copy(
        update={
            "started_at": datetime.now(UTC) - timedelta(seconds=301),
        }
    )
    future = _retry_job(
        repository,
        next_retry_at=datetime.now(UTC) + timedelta(minutes=5),
        suffix="future",
    )

    recoverable = repository.list_recoverable_jobs(
        claim_ttl_seconds=300,
        limit=50,
    )

    assert {job.id for job in recoverable} == {due.id, stale_job.id}
    assert future.id not in {job.id for job in recoverable}


def test_future_retry_redelivery_reschedules_for_durable_due_time() -> None:
    repository = InMemoryRepository()
    job = _retry_job(
        repository,
        next_retry_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    scheduled: list[tuple[tuple[str], int]] = []
    task = SimpleNamespace(
        apply_async=lambda *, args, countdown: scheduled.append(
            (args, countdown)
        )
    )

    claimed = claim_or_reschedule(
        task,
        repository,
        job.id,
        claim_ttl_seconds=300,
    )

    assert claimed is None
    assert scheduled
    assert scheduled[0][0] == (str(job.id),)
    assert 1 <= scheduled[0][1] <= 30
    assert repository.get_job(job.id).status is JobStatus.retry


def test_durable_retry_count_survives_celery_retry_reset() -> None:
    repository = InMemoryRepository()
    job, first_claim = _processing_job(repository)

    first_delay = _record_retry(
        repository,
        job.id,
        RuntimeError("one"),
        0,
        claim_started_at=first_claim.started_at,
    )
    assert first_delay == 5
    first_retry = repository.get_job(job.id)
    repository.jobs[job.id] = first_retry.model_copy(
        update={
            "next_retry_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    second_claim = repository.claim_job(job.id)
    assert second_claim is not None
    assert second_claim.started_at is not None

    second_delay = _record_retry(
        repository,
        job.id,
        RuntimeError("two"),
        0,
        claim_started_at=second_claim.started_at,
    )

    assert second_delay == 30
    current = repository.get_job(job.id)
    assert current is not None
    assert current.details["retry_count"] == 2


def test_durable_fourth_failure_moves_job_to_dead_letter() -> None:
    repository = InMemoryRepository()
    job, claim = _processing_job(repository, suffix="dead-letter")

    for expected_delay in (5, 30, 120):
        delay = _record_retry(
            repository,
            job.id,
            RuntimeError("temporary"),
            0,
            claim_started_at=claim.started_at,
        )
        assert delay == expected_delay
        current = repository.get_job(job.id)
        repository.jobs[job.id] = current.model_copy(
            update={
                "next_retry_at": datetime.now(UTC) - timedelta(seconds=1),
            }
        )
        claim = repository.claim_job(job.id)
        assert claim is not None
        assert claim.started_at is not None

    delay = _record_retry(
        repository,
        job.id,
        RuntimeError("permanent"),
        0,
        claim_started_at=claim.started_at,
    )

    assert delay is None
    current = repository.get_job(job.id)
    assert current is not None
    assert current.status is JobStatus.dead_letter
    assert current.details["retry_count"] == 4


def test_redriver_dispatches_each_recoverable_job_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryRepository()
    queued = repository.create_job(
        url="https://soundcloud.com/syco23/redrive",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    _retry_job(
        repository,
        next_retry_at=datetime.now(UTC) + timedelta(minutes=5),
        suffix="not-due",
    )
    calls = []
    monkeypatch.setattr(
        normalize_worker,
        "get_worker_repository",
        lambda: repository,
    )
    monkeypatch.setattr(
        normalize_worker,
        "get_settings",
        lambda: Settings(
            environment="fixture",
            repository_mode="memory",
            job_redrive_batch_size=50,
        ),
    )
    monkeypatch.setattr(
        normalize_worker,
        "JobDispatcher",
        lambda: SimpleNamespace(retry=lambda job: calls.append(job.id)),
    )

    dispatched = normalize_worker.redrive_import_jobs.run()

    assert dispatched == 1
    assert calls == [queued.id]
