from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class AudioInputKind(StrEnum):
    provider_acquisition = "provider_acquisition"
    creator_upload = "creator_upload"


class AudioInputStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    retry = "retry"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    dead_letter = "dead_letter"


class AudioAssetState(StrEnum):
    quarantine = "quarantine"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class AudioBucket(StrEnum):
    quarantine = "audio-quarantine"
    originals = "audio-originals"
    derivatives = "audio-derivatives"


class AudioInputJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    rights_review_id: UUID
    input_kind: AudioInputKind
    provider_key: str | None = Field(default=None, max_length=64)
    provider_item_external_id: str | None = Field(default=None, max_length=512)
    candidate_external_id: str = Field(min_length=1, max_length=512)
    source_url: str | None = Field(default=None, max_length=4096)
    expected_sha256: str | None = None
    status: AudioInputStatus = AudioInputStatus.queued
    attempt_count: int = Field(default=0, ge=0)
    claim_started_at: datetime | None = None
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    audio_asset_id: UUID | None = None
    created_by: str = Field(min_length=1, max_length=300)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("expected_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")
        return value

    @model_validator(mode="after")
    def validate_kind_and_terminal_state(self) -> "AudioInputJob":
        if self.input_kind is AudioInputKind.provider_acquisition:
            if not self.provider_key or not self.provider_item_external_id or not self.source_url:
                raise ValueError("provider acquisition jobs require provider identity and source URL")
        elif self.provider_key is not None or self.source_url is not None:
            raise ValueError("creator uploads cannot carry provider network identity")

        if self.status is AudioInputStatus.completed:
            if self.audio_asset_id is None or self.finished_at is None:
                raise ValueError("completed audio jobs require an asset and finish time")
        elif self.audio_asset_id is not None:
            raise ValueError("only completed audio jobs may reference an asset")
        return self


class AudioAssetRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    rights_review_id: UUID
    state: AudioAssetState = AudioAssetState.quarantine
    bucket_name: AudioBucket = AudioBucket.quarantine
    object_key: str = Field(
        pattern=r"^objects/[0-9a-f]{2}/[0-9a-f]{32}$",
        max_length=80,
    )
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=5_368_709_120)
    content_type: str | None = Field(default=None, min_length=3, max_length=100)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_bucket_state(self) -> "AudioAssetRecord":
        if self.state is AudioAssetState.approved:
            if self.bucket_name is not AudioBucket.originals:
                raise ValueError("approved audio assets must be stored in originals")
        elif self.bucket_name is not AudioBucket.quarantine:
            raise ValueError("unapproved audio assets must remain in quarantine")
        if self.state is AudioAssetState.quarantine and self.expires_at is None:
            raise ValueError("quarantine assets require an expiry")
        return self
