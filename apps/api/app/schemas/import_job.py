from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, BaseModel, Field

from app.schemas.set import SetSource


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    retry = "retry"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    dead_letter = "dead_letter"


class JobType(StrEnum):
    url_import = "url_import"
    search_profile = "search_profile"
    crawl = "crawl"


ALLOWED_JOB_TRANSITIONS = {
    JobStatus.queued: {JobStatus.processing, JobStatus.failed, JobStatus.blocked},
    JobStatus.processing: {JobStatus.completed, JobStatus.retry, JobStatus.failed, JobStatus.blocked},
    JobStatus.retry: {JobStatus.processing, JobStatus.dead_letter},
    JobStatus.completed: set(),
    JobStatus.failed: set(),
    JobStatus.blocked: set(),
    JobStatus.dead_letter: set(),
}


def validate_job_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise ValueError(f"Cannot transition job from {current} to {target}")


class ImportRequest(BaseModel):
    url: AnyHttpUrl
    source: SetSource | None = None


class ImportJob(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    url: str | None = None
    source: SetSource
    job_type: JobType = JobType.url_import
    profile_id: UUID | None = None
    status: JobStatus = JobStatus.queued
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_retry_at: datetime | None = None
    result_set_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ImportJobPatch(BaseModel):
    status: JobStatus | None = None
    attempt_count: int | None = Field(default=None, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_retry_at: datetime | None = None
    result_set_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] | None = None


class ImportJobPage(BaseModel):
    items: list[ImportJob]
    total: int
    limit: int
    offset: int
