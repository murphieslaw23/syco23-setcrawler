from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class MergeCandidateStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    restored = "restored"


class MergeDecisionAction(StrEnum):
    approve = "approve"
    reject = "reject"
    restore = "restore"


class MergeComponentScores(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title_artist: float = Field(ge=0, le=1)
    event: float = Field(ge=0, le=1)
    date_year: float = Field(ge=0, le=1)
    duration: float = Field(ge=0, le=1)
    aliases: float = Field(ge=0, le=1)


class MergeScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float = Field(ge=0, le=1)
    components: MergeComponentScores
    reasons: list[str] = Field(default_factory=list)


class SetProviderSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_key: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=512)
    canonical_url: str = Field(min_length=8, max_length=4096)
    embed_url: str | None = Field(default=None, max_length=4096)
    raw_metadata: dict[str, Any] = Field(default_factory=dict, exclude=True)
    is_primary: bool = False


class MergeCandidate(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_set_id: UUID
    target_set_id: UUID
    score: float = Field(ge=0, le=1)
    component_scores: MergeComponentScores
    reasons: list[str] = Field(default_factory=list)
    status: MergeCandidateStatus = MergeCandidateStatus.pending
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MergeCandidatePage(BaseModel):
    items: list[MergeCandidate]
    total: int
    limit: int
    offset: int


class MergeDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    merge_candidate_id: UUID
    action: MergeDecisionAction
    actor: str = Field(min_length=1, max_length=300)
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
