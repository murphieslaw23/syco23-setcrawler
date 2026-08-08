from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.audio import utc_now


class AudioProcessingJobStatus(StrEnum):
    queued = "queued"
    claimed = "claimed"
    retry = "retry"
    completed = "completed"
    failed = "failed"


class AudioProcessingJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    audio_asset_id: UUID
    status: AudioProcessingJobStatus = AudioProcessingJobStatus.queued
    claim_token: UUID | None = None
    claim_started_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    derivative_object_key: str | None = Field(
        default=None,
        pattern=r"^objects/[0-9a-f]{2}/[0-9a-f]{32}$",
        max_length=80,
    )
    next_retry_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=2000)
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_status_fields(self) -> "AudioProcessingJob":
        if self.status is AudioProcessingJobStatus.claimed:
            if self.claim_token is None or self.claim_started_at is None:
                raise ValueError("claimed processing jobs require claim identity")
        elif self.claim_token is not None or self.claim_started_at is not None:
            raise ValueError("only claimed processing jobs may carry claim identity")

        if self.status is AudioProcessingJobStatus.retry:
            if self.next_retry_at is None:
                raise ValueError("retry processing jobs require next_retry_at")
        elif self.next_retry_at is not None:
            raise ValueError("only retry processing jobs may carry next_retry_at")

        if self.status is AudioProcessingJobStatus.completed:
            if self.completed_at is None:
                raise ValueError("completed processing jobs require completed_at")
        elif self.completed_at is not None:
            raise ValueError("only completed processing jobs may carry completed_at")
        return self
