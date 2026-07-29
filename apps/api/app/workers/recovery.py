from datetime import UTC, datetime
from math import ceil
from uuid import UUID

from celery import Task

from app.repositories.base import Repository
from app.schemas.import_job import ImportJob, JobStatus


_TERMINAL_STATUSES = {
    JobStatus.completed,
    JobStatus.failed,
    JobStatus.blocked,
    JobStatus.dead_letter,
}


def claim_or_reschedule(
    task: Task,
    repository: Repository,
    job_id: UUID,
    *,
    claim_ttl_seconds: int,
) -> ImportJob | None:
    """Claim durable work or publish one replacement after its live lease."""
    claimed = repository.claim_job(
        job_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claimed is not None:
        return claimed
    current = repository.get_job(job_id)
    if current is None:
        raise KeyError(f"Import job {job_id} not found")
    if current.status in _TERMINAL_STATUSES:
        return None
    if (
        current.status is JobStatus.retry
        and current.next_retry_at is not None
        and current.next_retry_at > datetime.now(UTC)
    ):
        remaining = max(
            1,
            ceil(
                current.next_retry_at.timestamp()
                - datetime.now(UTC).timestamp()
            ),
        )
        task.apply_async(
            args=(str(job_id),),
            countdown=remaining,
        )
        return None
    if (
        current.status is JobStatus.processing
        and current.started_at is not None
    ):
        lease_expires = current.started_at.timestamp() + claim_ttl_seconds
        remaining = max(
            1,
            ceil(lease_expires - datetime.now(UTC).timestamp()),
        )
        task.apply_async(
            args=(str(job_id),),
            countdown=remaining,
        )
        return None
    raise ValueError(f"Import job {job_id} is not available for processing")
