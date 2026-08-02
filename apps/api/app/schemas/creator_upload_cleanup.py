from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class CreatorUploadCleanupReason(StrEnum):
    user_abort = "user_abort"
    admin_abort = "admin_abort"
    expired = "expired"
    rights_denied = "rights_denied"
    verification_failed = "verification_failed"


class CreatorUploadCleanupStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    retry = "retry"
    completed = "completed"
    dead_letter = "dead_letter"


class CreatorUploadCleanupOutcome(StrEnum):
    retry = "retry"
    completed = "completed"
    dead_letter = "dead_letter"


class CreatorUploadCleanupJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    reason: CreatorUploadCleanupReason
    status: CreatorUploadCleanupStatus = CreatorUploadCleanupStatus.queued
    object_key: str | None = Field(
        default=None,
        pattern=r"^objects/[0-9a-f]{2}/[0-9a-f]{32}$",
        max_length=80,
    )
    storage_upload_id: str | None = Field(default=None, min_length=1, max_length=2048)
    requested_by: str = Field(min_length=1, max_length=300)
    attempt_count: int = Field(default=0, ge=0, le=1000)
    claim_started_at: datetime | None = None
    next_retry_at: datetime | None = None
    last_error_code: str | None = Field(default=None, min_length=1, max_length=120)
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self) -> "CreatorUploadCleanupJob":
        private_values = (self.object_key, self.storage_upload_id)
        if any(value is None for value in private_values) and any(
            value is not None for value in private_values
        ):
            raise ValueError("cleanup private storage identity must be complete")
        if self.status is CreatorUploadCleanupStatus.processing:
            if self.claim_started_at is None:
                raise ValueError("processing cleanup requires a claim timestamp")
        elif self.claim_started_at is not None:
            raise ValueError("only processing cleanup may carry a claim timestamp")
        if self.status is CreatorUploadCleanupStatus.retry:
            if self.next_retry_at is None or self.last_error_code is None:
                raise ValueError("retry cleanup requires retry time and error code")
        elif self.next_retry_at is not None:
            raise ValueError("only retry cleanup may carry a retry timestamp")
        if self.status in {
            CreatorUploadCleanupStatus.completed,
            CreatorUploadCleanupStatus.dead_letter,
        }:
            if self.completed_at is None:
                raise ValueError("terminal cleanup requires completion time")
        elif self.completed_at is not None:
            raise ValueError("non-terminal cleanup cannot be completed")
        return self


class CreatorUploadCleanupTombstone(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    cleanup_job_id: UUID
    session_id: UUID
    reason: CreatorUploadCleanupReason
    outcome: CreatorUploadCleanupOutcome
    attempt_number: int = Field(ge=1, le=1000)
    multipart_aborted: bool
    object_deleted: bool
    ledger_deleted: bool
    error_code: str | None = Field(default=None, min_length=1, max_length=120)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_outcome(self) -> "CreatorUploadCleanupTombstone":
        if self.outcome is CreatorUploadCleanupOutcome.completed:
            if self.error_code is not None or not self.ledger_deleted:
                raise ValueError("completed cleanup requires deleted ledger and no error")
        elif self.error_code is None:
            raise ValueError("retry and dead-letter tombstones require an error code")
        return self


class CreatorUploadCleanupReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cleanup_job_id: UUID
    session_id: UUID
    reason: CreatorUploadCleanupReason
    status: CreatorUploadCleanupStatus
    attempt_count: int
    outcome: CreatorUploadCleanupOutcome
    multipart_aborted: bool
    object_deleted: bool
    ledger_deleted: bool
    created_at: datetime

    @classmethod
    def from_records(
        cls,
        job: CreatorUploadCleanupJob,
        tombstone: CreatorUploadCleanupTombstone,
    ) -> "CreatorUploadCleanupReceipt":
        if job.id != tombstone.cleanup_job_id or job.session_id != tombstone.session_id:
            raise ValueError("cleanup receipt records do not match")
        return cls(
            cleanup_job_id=job.id,
            session_id=job.session_id,
            reason=job.reason,
            status=job.status,
            attempt_count=job.attempt_count,
            outcome=tombstone.outcome,
            multipart_aborted=tombstone.multipart_aborted,
            object_deleted=tombstone.object_deleted,
            ledger_deleted=tombstone.ledger_deleted,
            created_at=tombstone.created_at,
        )
