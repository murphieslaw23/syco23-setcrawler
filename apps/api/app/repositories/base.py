from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.schemas.auth import UserRole
from app.schemas.candidate import Candidate, CandidateCreate
from app.schemas.import_job import ImportJob, ImportJobPage, ImportJobPatch, JobStatus, JobType
from app.schemas.profile import SearchProfile, SearchProfileCreate, SearchProfileUpdate
from app.schemas.set import ReviewStatus, SetDetail, SetPage, SetPatch, SetSource
from app.services.heuristic import HeuristicConfig, ScoreResult
from app.services.normalizer import RawSetPayload


class ActiveProfileJobsError(RuntimeError):
    """Raised when deleting a profile would detach active durable work."""


class Repository(Protocol):
    def create_job(
        self,
        *,
        url: str | None,
        source: SetSource,
        job_type: JobType,
        profile_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> ImportJob: ...

    def get_job(self, job_id: UUID) -> ImportJob | None: ...

    def create_retry_job(self, job_id: UUID) -> tuple[ImportJob, bool] | None: ...

    def claim_job(
        self,
        job_id: UUID,
        *,
        claim_ttl_seconds: int = 300,
    ) -> ImportJob | None: ...

    def list_recoverable_jobs(
        self,
        *,
        claim_ttl_seconds: int,
        limit: int,
    ) -> list[ImportJob]: ...

    def list_jobs(
        self, *, source: SetSource | None, status: JobStatus | None, limit: int, offset: int
    ) -> ImportJobPage: ...

    def transition_job(self, job_id: UUID, patch: ImportJobPatch) -> ImportJob | None: ...

    def transition_claimed_job(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        patch: ImportJobPatch,
    ) -> ImportJob | None: ...

    def complete_duplicate_job(
        self,
        job_id: UUID,
        duplicate_set_id: UUID,
        *,
        claim_started_at: datetime,
    ) -> ImportJob | None: ...

    def complete_discarded_job(
        self,
        job_id: UUID,
        score: ScoreResult,
        *,
        claim_started_at: datetime,
    ) -> ImportJob | None: ...

    def find_duplicate(self, payload: RawSetPayload, fingerprint: str) -> UUID | None: ...

    def persist_processed_set(
        self,
        *,
        payload: RawSetPayload,
        score: ScoreResult,
        candidates: list[CandidateCreate],
        job_id: UUID,
        fingerprint: str,
        claim_started_at: datetime,
    ) -> UUID | None: ...

    def get_heuristic_config(self) -> HeuristicConfig: ...

    def get_user_role(self, user_id: UUID) -> UserRole | None: ...

    def get_profile(self, profile_id: UUID) -> SearchProfile | None: ...

    def checkpoint_profile_page(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        *,
        input_page_token: str | None,
        next_page_token: str | None,
        payloads: list[RawSetPayload],
    ) -> ImportJob | None: ...

    def get_or_create_child_job(
        self,
        parent_job_id: UUID,
        claim_started_at: datetime,
        payload: RawSetPayload,
    ) -> ImportJob | None: ...

    def finalize_profile_job(
        self,
        job_id: UUID,
        claim_started_at: datetime,
        *,
        status: JobStatus,
        next_page_token: str | None,
        result_count: int,
        discard_count: int,
        duplicate_count: int,
        error_code: str | None,
        error_message: str | None,
    ) -> ImportJob | None: ...

    def list_sets(
        self,
        *,
        source: SetSource | None,
        status: ReviewStatus | None,
        min_score: float | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> SetPage: ...

    def get_set(self, set_id: UUID) -> SetDetail | None: ...

    def update_set(self, set_id: UUID, patch: SetPatch, actor: str = "local-editor") -> SetDetail | None: ...

    def decide_candidate(self, set_id: UUID, candidate_id: UUID, accepted: bool) -> Candidate | None: ...

    def list_profiles(self) -> list[SearchProfile]: ...

    def create_profile(self, payload: SearchProfileCreate) -> SearchProfile: ...

    def update_profile(self, profile_id: UUID, payload: SearchProfileUpdate) -> SearchProfile | None: ...

    def delete_profile(self, profile_id: UUID) -> bool: ...

    def queue_profile(self, profile_id: UUID) -> ImportJob | None: ...

    def queue_profile_with_creation(
        self,
        profile_id: UUID,
    ) -> tuple[ImportJob, bool] | None: ...

    def mark_profile_scheduled(
        self,
        profile_id: UUID,
        *,
        scheduled_at: datetime,
        next_scheduled_at: datetime,
    ) -> SearchProfile | None: ...

    def stats(self) -> dict[str, Any]: ...
