from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.services.cron_schedule import validate_cron, validate_timezone


def utc_now() -> datetime:
    return datetime.now(UTC)


class SearchProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    query: str = Field(min_length=2, max_length=160)
    source: str = Field(default="youtube", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    operation: str = Field(default="search", pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    schedule_cron: str = "0 6 * * *"
    schedule_timezone: str = "UTC"
    enabled: bool = True

    @field_validator("schedule_cron")
    @classmethod
    def validate_schedule(cls, value: str) -> str:
        return validate_cron(value)

    @field_validator("schedule_timezone")
    @classmethod
    def validate_schedule_timezone(cls, value: str) -> str:
        return validate_timezone(value)

    @model_validator(mode="after")
    def default_query_parameter(self):
        if not self.parameters and self.operation == "search":
            self.parameters = {"query": self.query}
        return self


class SearchProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=80)
    query: str | None = Field(default=None, min_length=2, max_length=160)
    source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    operation: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parameters: dict[str, JsonValue] | None = None
    schedule_cron: str | None = None
    schedule_timezone: str | None = None
    enabled: bool | None = None

    @field_validator("schedule_cron")
    @classmethod
    def validate_schedule(cls, value: str | None) -> str | None:
        return validate_cron(value) if value is not None else None

    @field_validator("schedule_timezone")
    @classmethod
    def validate_schedule_timezone(cls, value: str | None) -> str | None:
        return validate_timezone(value) if value is not None else None


class SearchProfile(SearchProfileCreate):
    model_config = ConfigDict(extra="ignore")

    id: UUID = Field(default_factory=uuid4)
    last_scheduled_at: datetime | None = None
    next_scheduled_at: datetime | None = None
    last_run_at: datetime | None = None
    next_page_token: str | None = None
    last_result_count: int | None = None
    last_error_code: str | None = None
    latest_job_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
