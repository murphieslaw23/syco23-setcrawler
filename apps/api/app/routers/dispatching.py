from datetime import UTC, datetime
from collections.abc import Callable

from fastapi import HTTPException, status

from app.repositories.base import Repository
from app.schemas.import_job import ImportJob, ImportJobPatch, JobStatus


def dispatch_or_terminalize(
    repository: Repository,
    job: ImportJob,
    dispatch: Callable[[ImportJob], None],
) -> None:
    try:
        dispatch(job)
    except Exception as error:
        terminal = repository.transition_job(
            job.id,
            ImportJobPatch(
                status=JobStatus.failed,
                finished_at=datetime.now(UTC),
                error_code="broker_dispatch_failed",
                error_message="Background queue dispatch failed",
            ),
        )
        if terminal is None or terminal.status is not JobStatus.failed:
            raise RuntimeError(
                "Dispatch failed and durable job terminalization failed"
            ) from error
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Import dispatch is temporarily unavailable",
        ) from error
