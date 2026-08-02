from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_CREATOR_UPLOAD_BYTES = 5_368_709_120
ALLOWED_CREATOR_AUDIO_TYPES = frozenset(
    {
        "application/ogg",
        "audio/aac",
        "audio/flac",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/x-aac",
        "audio/x-flac",
        "audio/x-wav",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class CreatorUploadStatus(StrEnum):
    initiated = "initiated"
    uploading = "uploading"
    awaiting_attestation = "awaiting_attestation"
    completed = "completed"
    aborted = "aborted"
    expired = "expired"


class CreatorUploadStart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_size_bytes: int = Field(ge=1, le=MAX_CREATOR_UPLOAD_BYTES)
    content_type: str = Field(min_length=3, max_length=100)
    expected_sha256: str | None = None

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str) -> str:
        normalized = value.casefold().strip()
        if normalized not in ALLOWED_CREATOR_AUDIO_TYPES:
            raise ValueError("creator upload content type is not allowed")
        return normalized

    @field_validator("expected_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")
        return value


class CreatorUploadSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    audio_input_job_id: UUID
    rights_review_id: UUID
    expected_size_bytes: int = Field(ge=1, le=MAX_CREATOR_UPLOAD_BYTES)
    received_size_bytes: int = Field(default=0, ge=0)
    content_type: str = Field(min_length=3, max_length=100)
    expected_sha256: str | None = None
    status: CreatorUploadStatus = CreatorUploadStatus.initiated
    attestation_evidence_id: UUID | None = None
    attested_by: str | None = Field(default=None, max_length=300)
    attested_at: datetime | None = None
    expires_at: datetime
    created_by: str = Field(min_length=1, max_length=300)
    version: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_progress(self) -> "CreatorUploadSession":
        if self.received_size_bytes > self.expected_size_bytes:
            raise ValueError("received upload bytes exceed the declared size")
        if self.status in {
            CreatorUploadStatus.awaiting_attestation,
            CreatorUploadStatus.completed,
        } and self.received_size_bytes != self.expected_size_bytes:
            raise ValueError("attestation requires a complete upload")
        if self.status is CreatorUploadStatus.completed and (
            self.attestation_evidence_id is None
            or self.attested_by is None
            or self.attested_at is None
        ):
            raise ValueError("completed creator uploads require attestation")
        return self
