from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.candidate import Candidate
from app.schemas.image import SetImage


class SetSource(StrEnum):
    youtube = "youtube"
    soundcloud = "soundcloud"
    freeteknomusic = "freeteknomusic"


class ReviewStatus(StrEnum):
    inbox = "inbox"
    reviewing = "reviewing"
    accepted = "accepted"
    rejected = "rejected"
    published = "published"


class SetSummary(BaseModel):
    id: UUID
    source: SetSource
    source_id: str
    canonical_url: str
    title: str
    duration_seconds: int | None = None
    published_at: datetime | None = None
    set_score: float = Field(ge=0, le=1)
    review_status: ReviewStatus
    artist_names: list[str] = []
    event_name: str | None = None
    city: str | None = None
    primary_image_url: str | None = None
    score_reasons: list[str] = []
    import_job_id: UUID | None = None
    duplicate_of_id: UUID | None = None


class SetDetail(SetSummary):
    description: str | None = None
    venue: str | None = None
    year: int | None = None
    raw_payload: dict[str, Any] = {}
    candidates: list[Candidate] = []
    images: list[SetImage] = []
    created_at: datetime
    updated_at: datetime


class SetPage(BaseModel):
    items: list[SetSummary]
    total: int
    limit: int
    offset: int


class SetPatch(BaseModel):
    review_status: ReviewStatus | None = None
    title: str | None = Field(default=None, min_length=1)
    event_name: str | None = None
    venue: str | None = None
    city: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
