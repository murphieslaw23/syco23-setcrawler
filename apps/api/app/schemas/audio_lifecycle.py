from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class AudioLifecycleAction(StrEnum):
    approve = "approve"
    reject = "reject"
    expire = "expire"


class AudioLifecycleJobStatus(StrEnum):
    queued = "queued"
    claimed = "claimed"
    retry = "retry"
    completed = "completed"
    failed = "failed"


class AudioStorageOutcome(StrEnum):
    promoted = "promoted"
    deleted = "deleted"


class AudioLifecycleJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    audio_asset_id: UUID
    action: AudioLifecycleAction
    status: AudioLifecycleJobStatus = AudioLifecycleJobStatus.queued
    claim_token: UUID | None = None
    claim_started_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2000)
    last_error: str | None = Field(default=None, max_length=2000)
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_claim_and_terminal_state(self) -> "AudioLifecycleJob":
        if self.status is AudioLifecycleJobStatus.claimed:
            if self.claim_token is None or self.claim_started_at is None:
                raise ValueError("claimed lifecycle jobs require a claim fence")
        elif self.claim_token is not None or self.claim_started_at is not None:
            raise ValueError("only claimed lifecycle jobs may carry a claim fence")

        if self.status is AudioLifecycleJobStatus.completed:
            if self.completed_at is None:
                raise ValueError("completed lifecycle jobs require completion time")
        elif self.completed_at is not None:
            raise ValueError("only completed lifecycle jobs may carry completion time")
        return self


class AudioLifecycleTombstone(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    lifecycle_job_id: UUID
    audio_asset_id: UUID
    action: AudioLifecycleAction
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2000)
    storage_outcome: AudioStorageOutcome
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=5_368_709_120)
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
