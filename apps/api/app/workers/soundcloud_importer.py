import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import get_settings
from app.repositories.base import Repository
from app.schemas.import_job import ImportJobPatch, JobStatus
from app.services.provider import (
    ProviderError,
    ProviderTemporaryError,
    get_provider_registry,
)
from app.workers.celery_app import celery_app
from app.workers.normalize_worker import (
    _record_retry,
    get_worker_repository,
)
from app.workers.process_dispatch import dispatch_process_payload
from app.workers.recovery import claim_or_reschedule


def get_soundcloud_adapter() -> object:
    return get_provider_registry().adapter("soundcloud")


def _fail_job(
    repository: Repository,
    job_id: UUID,
    *,
    claim_started_at: datetime,
    error_code: str,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=JobStatus.failed,
            finished_at=datetime.now(UTC),
            error_code=error_code,
            error_message=error_code,
        ),
    )


def _block_job(
    repository: Repository,
    job_id: UUID,
    *,
    claim_started_at: datetime,
    error_code: str,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=JobStatus.blocked,
            finished_at=datetime.now(UTC),
            error_code=error_code,
            error_message=error_code,
        ),
    )


@celery_app.task(
    bind=True,
    name="app.workers.soundcloud_importer.import_url",
)
def import_soundcloud(self, job_id: str) -> str | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    job = repository.get_job(parsed_job_id)
    if job is None:
        raise KeyError(f"Import job {parsed_job_id} not found")
    if job.url is None:
        raise ValueError(f"Import job {parsed_job_id} has no URL")
    if job.status is JobStatus.completed:
        return (
            str(job.result_set_id)
            if job.result_set_id is not None
            else None
        )

    settings = get_settings()
    claim_ttl_seconds = settings.job_claim_ttl_seconds
    claimed = claim_or_reschedule(
        self,
        repository,
        parsed_job_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claimed is None or claimed.started_at is None:
        return None
    if settings.provider_mode != "live":
        _block_job(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            error_code="provider_mode_fixture",
        )
        return None
    try:
        payload = asyncio.run(
            get_soundcloud_adapter().fetch(claimed.url)
        )
    except ProviderTemporaryError as error:
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=claimed.started_at,
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    except ProviderError as error:
        _fail_job(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            error_code=str(error),
        )
        raise
    except Exception:
        _fail_job(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            error_code="soundcloud_worker_error",
        )
        raise
    try:
        dispatch_process_payload(
            parsed_job_id,
            payload,
            claimed.started_at,
        )
    except Exception as error:
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=claimed.started_at,
            error_code="process_dispatch_error",
            error_message="Process queue dispatch failed",
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    return None
