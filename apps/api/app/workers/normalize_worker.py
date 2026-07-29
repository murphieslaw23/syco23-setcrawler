from datetime import UTC, datetime, timedelta
from functools import lru_cache
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
from app.workers.celery_app import celery_app


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
    if retries < 3:
        delay = retry_delay(retries + 1)
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
            error_code="retry_exhausted",
            error_message=str(error),
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
        error_code="retry_exhausted",
        error_message=str(error),
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
