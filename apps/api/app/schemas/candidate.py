from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class CandidateCreate(BaseModel):
    field_name: str
    candidate_value: str
    confidence: float = Field(ge=0, le=1)
    source: str


class Candidate(CandidateCreate):
    id: UUID = Field(default_factory=uuid4)
    set_id: UUID | None = None
    accepted: bool | None = None
    created_at: datetime = Field(default_factory=utc_now)
