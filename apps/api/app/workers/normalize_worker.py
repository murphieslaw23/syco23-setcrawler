from datetime import UTC, datetime, timedelta
from functools import lru_cache
import logging
from uuid import UUID

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.database import create_pool
from app.repositories.base import Repository
from app.repositories.memory import InMemoryRepository
from app.repositories.postgres import PostgresRepository
from app.schemas.import_job import ImportJobPatch, JobStatus
from app.services.import_pipeline import process_payload
from app.services.import_pipeline import retry_delay
from app.services.normalizer import RawSetPayload
from app.services.operational_health import record_periodic_task_success
from app.workers.celery_app import celery_app
from app.workers.dispatch import JobDispatcher


logger = logging.getLogger(__name__)


@lru_cache
def get_worker_repository() -> Repository:
    settings = get_settings()
    if settings.repository_mode == "memory":
        return InMemoryRepository.seeded()
    pool = create_pool(settings.database_url)
    pool.open()
    pool.wait()
    return PostgresRepository(pool)


def _fail_invalid_payload(
    repository: Repository,
    job_id: UUID,
    claim_started_at: datetime,
    error: ValidationError,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=JobStatus.failed,
            finished_at=datetime.now(UTC),
            error_code="invalid_payload",
            error_message=str(error),
        ),
    )


def _record_retry(
    repository: Repository,
    job_id: UUID,
    error: Exception,
    retries: int,
    *,
    claim_started_at: datetime | None = None,
    error_code: str = "processing_error",
    error_message: str | None = None,
) -> int | None:
    current = repository.get_job(job_id)
    if current is None:
        raise KeyError(f"Import job {job_id} not found")
    if current.status is JobStatus.completed:
        return None
    if current.status is not JobStatus.processing:
        raise ValueError(
            f"Import job {job_id} cannot record a processing failure"
        )
    retry_count = max(
        retries,
        int(current.details.get("retry_count", 0)),
    ) + 1
    details = {
        **current.details,
        "retry_count": retry_count,
    }
    if retry_count <= 3:
        delay = retry_delay(retry_count)
        patch = ImportJobPatch(
            status=JobStatus.retry,
            next_retry_at=datetime.now(UTC)
            + timedelta(seconds=delay),
            error_code=error_code,
            error_message=(
                str(error)
                if error_message is None
                else error_message
            ),
            details=details,
        )
        if claim_started_at is None:
            transitioned = repository.transition_job(job_id, patch)
        else:
            transitioned = repository.transition_claimed_job(
                job_id,
                claim_started_at,
                patch,
            )
        if transitioned is None:
            return None
        return delay
    exhausted = ImportJobPatch(
            status=JobStatus.retry,
            next_retry_at=None,
            error_code="retry_exhausted",
            error_message=str(error),
            details=details,
    )
    if claim_started_at is None:
        transitioned = repository.transition_job(job_id, exhausted)
    else:
        transitioned = repository.transition_claimed_job(
            job_id,
            claim_started_at,
            exhausted,
        )
    if transitioned is None:
        return None
    terminal_patch = ImportJobPatch(
        status=JobStatus.dead_letter,
        finished_at=datetime.now(UTC),
        next_retry_at=None,
        error_code="retry_exhausted",
        error_message=str(error),
        details=details,
    )
    if claim_started_at is None:
        repository.transition_job(job_id, terminal_patch)
    else:
        repository.transition_claimed_job(
            job_id,
            claim_started_at,
            terminal_patch,
        )
    return None


@celery_app.task(
    name="app.workers.normalize_worker.redrive_import_jobs",
)
def redrive_import_jobs() -> int:
    """Republish durable work that is absent from or expired in Redis."""
    repository = get_worker_repository()
    settings = get_settings()
    dispatcher = JobDispatcher()
    jobs = repository.list_recoverable_jobs(
        claim_ttl_seconds=settings.job_claim_ttl_seconds,
        limit=settings.job_redrive_batch_size,
    )
    dispatched = 0
    publish_failures = 0
    for job in jobs:
        try:
            dispatcher.retry(job)
        except Exception:
            publish_failures += 1
            logger.exception(
                "Failed to redrive import job",
                extra={
                    "event": "redrive_publish_failed",
                    "job_id": str(job.id),
                },
            )
            continue
        dispatched += 1
    record_periodic_task_success(
        settings,
        task_name="redrive_import_jobs",
        redrive_publish_failures=publish_failures,
    )
    return dispatched


@celery_app.task(
    bind=True,
    name="app.workers.normalize_worker.process_raw_payload",
)
def process_raw_payload(
    self,
    job_id: str,
    payload: dict,
    claim_started_at: str,
) -> str | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    owner_token = datetime.fromisoformat(claim_started_at)
    if owner_token.tzinfo is None:
        raise ValueError("Process ownership token must be timezone-aware")
    current = repository.get_job(parsed_job_id)
    if current is None:
        raise KeyError(f"Import job {parsed_job_id} not found")
    if current.status is JobStatus.completed:
        return (
            str(current.result_set_id)
            if current.result_set_id is not None
            else None
        )
    if current.status is JobStatus.retry:
        claimed = repository.claim_job(
            parsed_job_id,
            claim_ttl_seconds=get_settings().job_claim_ttl_seconds,
        )
        if claimed is None or claimed.started_at is None:
            return None
        owner_token = claimed.started_at
    elif (
        current.status is not JobStatus.processing
        or current.started_at != owner_token
    ):
        return None
    try:
        normalized = RawSetPayload.model_validate(payload)
    except ValidationError as error:
        _fail_invalid_payload(
            repository,
            parsed_job_id,
            owner_token,
            error,
        )
        raise
    try:
        set_id = process_payload(
            repository,
            parsed_job_id,
            normalized,
            claim_started_at=owner_token,
        )
    except Exception as error:
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=owner_token,
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    return str(set_id) if set_id is not None else None
