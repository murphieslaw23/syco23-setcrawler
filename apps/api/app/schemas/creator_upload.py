from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
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
_REQUIRED_ATTESTATION_ASSERTIONS = (
    "rights_holder",
    "allows_distribution",
    "allows_derivatives",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_content_type(value: str) -> str:
    normalized = value.casefold().strip()
    if normalized not in ALLOWED_CREATOR_AUDIO_TYPES:
        raise ValueError("creator upload content type is not allowed")
    return normalized


def _validate_sha256(value: str | None) -> str | None:
    if value is not None and (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("expected_sha256 must be lowercase hexadecimal")
    return value


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

    _content_type = field_validator("content_type")(_normalize_content_type)
    _checksum = field_validator("expected_sha256")(_validate_sha256)


class CreatorUploadAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_url: str = Field(min_length=8, max_length=4096)
    assertions: dict[str, Any]
    expected_version: int = Field(ge=0)

    @field_validator("reference_url")
    @classmethod
    def validate_reference_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise ValueError("creator attestation reference must be HTTPS")
        return value

    @model_validator(mode="after")
    def validate_assertions(self) -> "CreatorUploadAttestation":
        if any(
            self.assertions.get(assertion) is not True
            for assertion in _REQUIRED_ATTESTATION_ASSERTIONS
        ):
            raise ValueError(
                "creator attestation must affirm ownership, distribution, and derivatives"
            )
        return self


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

    _content_type = field_validator("content_type")(_normalize_content_type)
    _checksum = field_validator("expected_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def validate_progress(self) -> "CreatorUploadSession":
        if self.received_size_bytes > self.expected_size_bytes:
            raise ValueError("received upload bytes exceed the declared size")
        if self.expires_at <= self.created_at:
            raise ValueError("creator upload expiry must follow creation")
        if self.status in {
            CreatorUploadStatus.awaiting_attestation,
            CreatorUploadStatus.completed,
        } and self.received_size_bytes != self.expected_size_bytes:
            raise ValueError("attestation requires a complete upload")
        attestation_values = (
            self.attestation_evidence_id,
            self.attested_by,
            self.attested_at,
        )
        if self.status is CreatorUploadStatus.completed:
            if any(value is None for value in attestation_values):
                raise ValueError("completed creator uploads require attestation")
        elif any(value is not None for value in attestation_values):
            raise ValueError("only completed creator uploads may carry attestation")
        return self
