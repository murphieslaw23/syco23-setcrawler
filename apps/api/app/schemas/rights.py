from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RightsEvidenceType(StrEnum):
    creator_attestation = "creator_attestation"
    provider_permission = "provider_permission"
    permissive_license = "permissive_license"
    contract = "contract"


class RightsReviewStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class RightsDecisionAction(StrEnum):
    approve = "approve"
    reject = "reject"
    expire = "expire"


class RightsEvidenceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_type: RightsEvidenceType
    reference_url: str = Field(min_length=8, max_length=4096)
    assertions: dict[str, Any] = Field(default_factory=dict)

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
            raise ValueError("rights evidence reference must be HTTPS")
        return value


class RightsEvidence(RightsEvidenceInput):
    id: UUID = Field(default_factory=uuid4)
    rights_review_id: UUID
    submitted_by: str = Field(min_length=1, max_length=300)
    created_at: datetime = Field(default_factory=utc_now)


class RightsReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set_id: UUID
    provider_key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
    )
    provider_external_id: str = Field(min_length=1, max_length=512)
    requested_stream: bool
    requested_download: bool
    evidence: list[RightsEvidenceInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_requested_permissions(self) -> "RightsReviewCreate":
        if not self.requested_stream and not self.requested_download:
            raise ValueError("at least one permission must be requested")
        return self


class RightsReview(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    set_id: UUID
    provider_key: str
    provider_external_id: str
    requested_stream: bool
    requested_download: bool
    allow_stream: bool = False
    allow_download: bool = False
    status: RightsReviewStatus = RightsReviewStatus.pending
    evidence: list[RightsEvidence] = Field(default_factory=list)
    submitted_by: str
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RightsReviewPage(BaseModel):
    items: list[RightsReview]
    total: int
    limit: int
    offset: int


class RightsReviewApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_stream: bool
    allow_download: bool
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_grant(self) -> "RightsReviewApproval":
        if not self.allow_stream and not self.allow_download:
            raise ValueError("at least one permission must be granted")
        return self


class RightsReviewResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class RightsDecisionEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    rights_review_id: UUID
    action: RightsDecisionAction
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2000)
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
