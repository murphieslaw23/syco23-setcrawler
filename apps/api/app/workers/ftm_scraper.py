import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import get_settings
from app.repositories.base import Repository
from app.schemas.import_job import ImportJobPatch, JobStatus
from app.services.ftm import FTMAdapter
from app.services.provider import (
    ProviderBlockedError,
    ProviderError,
    ProviderTemporaryError,
)
from app.workers.celery_app import celery_app
from app.workers.normalize_worker import _record_retry, get_worker_repository
from app.workers.process_dispatch import dispatch_process_payload
from app.workers.recovery import claim_or_reschedule


def get_ftm_adapter() -> FTMAdapter:
    return FTMAdapter()


def _transition_terminal(
    repository: Repository,
    job_id: UUID,
    *,
    claim_started_at: datetime,
    status: JobStatus,
    error_code: str,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=status,
            finished_at=datetime.now(UTC),
            error_code=error_code,
            error_message=error_code,
        ),
    )


def _blocked_code(error: ProviderBlockedError) -> str:
    if str(error) == "provider_mode_fixture":
        return "provider_mode_fixture"
    if str(error) == "ftm_robots_denied":
        return "robots_denied"
    if str(error) == "ftm_disabled":
        return "provider_disabled"
    return "provider_blocked"


@celery_app.task(
    bind=True,
    name="app.workers.ftm_scraper.import_url",
)
def import_ftm(self, job_id: str) -> str | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    job = repository.get_job(parsed_job_id)
    if job is None:
        raise KeyError(f"Import job {parsed_job_id} not found")
    if job.url is None:
        raise ValueError(f"Import job {parsed_job_id} has no URL")
    if job.status is JobStatus.completed:
        return str(job.result_set_id) if job.result_set_id else None

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
    try:
        if settings.provider_mode != "live":
            raise ProviderBlockedError("provider_mode_fixture")
        if not settings.ftm_scraper_enabled:
            raise ProviderBlockedError("ftm_disabled")
        payload = asyncio.run(get_ftm_adapter().fetch(claimed.url))
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
        raise self.retry(exc=error, countdown=delay, max_retries=3)
    except ProviderBlockedError as error:
        _transition_terminal(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            status=JobStatus.blocked,
            error_code=_blocked_code(error),
        )
        return None
    except ProviderError as error:
        _transition_terminal(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            status=JobStatus.failed,
            error_code=str(error),
        )
        raise
    except Exception:
        _transition_terminal(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            status=JobStatus.failed,
            error_code="ftm_worker_error",
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
        raise self.retry(exc=error, countdown=delay, max_retries=3)
    return None
