from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


MIN_MULTIPART_PART_SIZE_BYTES = 5 * 1024 * 1024
MAX_MULTIPART_PARTS = 10_000


def utc_now() -> datetime:
    return datetime.now(UTC)


class CreatorUploadManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    part_size_bytes: int = Field(ge=MIN_MULTIPART_PART_SIZE_BYTES)
    expected_part_count: int = Field(ge=1, le=MAX_MULTIPART_PARTS)
    created_at: datetime = Field(default_factory=utc_now)


class CreatorUploadPartRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    part_number: int = Field(ge=1, le=MAX_MULTIPART_PARTS)
    etag: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=1)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)
