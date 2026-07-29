from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class SearchProfileCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    query: str = Field(min_length=2, max_length=160)
    schedule_cron: str = "0 6 * * *"
    enabled: bool = True


class SearchProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    query: str | None = Field(default=None, min_length=2, max_length=160)
    schedule_cron: str | None = None
    enabled: bool | None = None


class SearchProfile(SearchProfileCreate):
    id: UUID = Field(default_factory=uuid4)
    source: str = "youtube"
    last_run_at: datetime | None = None
    next_page_token: str | None = None
    last_result_count: int | None = None
    last_error_code: str | None = None
    latest_job_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
