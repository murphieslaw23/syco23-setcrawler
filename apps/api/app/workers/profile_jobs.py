from uuid import UUID

from app.repositories.base import Repository
from app.schemas.import_job import ImportJob, JobStatus, JobType
from app.workers.recovery import claim_or_reschedule


PROFILE_COUNT_KEYS = (
    "result_count",
    "discard_count",
    "duplicate_count",
)
TERMINAL_JOB_STATUSES = {
    JobStatus.completed,
    JobStatus.failed,
    JobStatus.dead_letter,
    JobStatus.blocked,
}


class ProfileOwnershipLost(RuntimeError):
    pass


def profile_job_counts(job: ImportJob) -> dict[str, int]:
    return {
        key: int(job.details.get(key, 0))
        for key in PROFILE_COUNT_KEYS
    }


def claim_profile_job(
    task,
    repository: Repository,
    job_id: UUID,
    *,
    claim_ttl_seconds: int,
) -> ImportJob | None:
    current = repository.get_job(job_id)
    if current is None:
        raise KeyError(f"Import job {job_id} not found")
    if current.job_type is not JobType.search_profile:
        raise ValueError(f"Import job {job_id} is not a search profile job")
    if current.profile_id is None:
        raise ValueError(f"Import job {job_id} has no search profile")
    if current.status in TERMINAL_JOB_STATUSES:
        return None
    claimed = claim_or_reschedule(
        task,
        repository,
        job_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claimed is None:
        current = repository.get_job(job_id)
        if current is None:
            raise KeyError(f"Import job {job_id} not found")
        if current.job_type is not JobType.search_profile:
            raise ValueError(f"Import job {job_id} is not a search profile job")
        if current.profile_id is None:
            raise ValueError(f"Import job {job_id} has no search profile")
        if current.status in TERMINAL_JOB_STATUSES:
            return None
        if current.status is JobStatus.processing:
            return None
        raise ValueError(f"Import job {job_id} is not available for processing")
    return claimed


def finalize_profile_failure(
    repository: Repository,
    job: ImportJob,
    *,
    error_code: str,
    status: JobStatus = JobStatus.failed,
) -> None:
    if job.profile_id is None:
        raise ValueError(f"Import job {job.id} has no search profile")
    if job.started_at is None:
        raise ValueError(f"Import job {job.id} has no ownership token")
    profile = repository.get_profile(job.profile_id)
    repository.finalize_profile_job(
        job.id,
        job.started_at,
        status=status,
        next_page_token=(
            profile.next_page_token if profile is not None else None
        ),
        result_count=0,
        discard_count=0,
        duplicate_count=0,
        error_code=error_code,
        error_message=error_code,
    )
