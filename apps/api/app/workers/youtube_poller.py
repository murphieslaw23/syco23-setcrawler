import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import get_settings
from app.repositories.base import Repository
from app.schemas.import_job import (
    ImportJob,
    ImportJobPatch,
    JobStatus,
    JobType,
)
from app.services.normalizer import RawSetPayload
from app.services.provider import (
    ProviderError,
    ProviderPayloadError,
    ProviderQuotaError,
    ProviderTemporaryError,
    get_provider_registry,
)
from app.workers.celery_app import celery_app
from app.workers.normalize_worker import (
    _record_retry,
    get_worker_repository,
)
from app.workers.process_dispatch import dispatch_process_payload
from app.workers.profile_jobs import (
    ProfileOwnershipLost,
    claim_profile_job,
    finalize_profile_failure,
    profile_job_counts,
)
from app.workers.recovery import claim_or_reschedule


def get_youtube_adapter() -> object:
    return get_provider_registry().adapter("youtube")


def _load_profile_checkpoint(
    repository: Repository,
    profile_id: UUID,
    parent_job_id: UUID,
    *,
    claim_started_at: datetime,
) -> tuple[list[RawSetPayload], str | None]:
    profile = repository.get_profile(profile_id)
    if profile is None:
        raise KeyError(f"Search profile {profile_id} not found")
    current = repository.get_job(parent_job_id)
    if current is None:
        raise KeyError(f"Import job {parent_job_id} not found")
    checkpoint = current.details.get("youtube_page_checkpoint")
    if checkpoint is None:
        input_page_token = profile.next_page_token
        fetched = asyncio.run(get_youtube_adapter().search(profile))
        checkpointed = repository.checkpoint_profile_page(
            parent_job_id,
            claim_started_at,
            input_page_token=input_page_token,
            next_page_token=fetched.next_page_token,
            payloads=fetched.payloads,
        )
        if checkpointed is None:
            raise ProfileOwnershipLost()
        checkpoint = checkpointed.details.get(
            "youtube_page_checkpoint"
        )
    if not isinstance(checkpoint, dict):
        raise ProviderPayloadError("youtube_checkpoint_invalid")
    payload_values = checkpoint.get("payloads")
    source_ids = checkpoint.get("source_ids")
    next_page_token = checkpoint.get("next_page_token")
    if (
        not isinstance(payload_values, list)
        or not isinstance(source_ids, list)
        or (
            next_page_token is not None
            and not isinstance(next_page_token, str)
        )
    ):
        raise ProviderPayloadError("youtube_checkpoint_invalid")
    try:
        payloads = [
            RawSetPayload.model_validate(value)
            for value in payload_values
        ]
    except Exception as error:
        raise ProviderPayloadError(
            "youtube_checkpoint_invalid"
        ) from error
    if [payload.source_id for payload in payloads] != source_ids:
        raise ProviderPayloadError("youtube_checkpoint_invalid")

    return payloads, next_page_token


def _process_profile(
    repository: Repository,
    profile_id: UUID,
    parent_job_id: UUID,
    *,
    claim_started_at: datetime,
    claim_ttl_seconds: int,
) -> None:
    payloads, _next_page_token = _load_profile_checkpoint(
        repository,
        profile_id,
        parent_job_id,
        claim_started_at=claim_started_at,
    )
    for payload in payloads:
        child = repository.get_or_create_child_job(
            parent_job_id,
            claim_started_at,
            payload,
        )
        if child is None:
            raise ProfileOwnershipLost()
        if child.status is JobStatus.completed:
            continue
        if child.status in {
            JobStatus.failed,
            JobStatus.blocked,
            JobStatus.dead_letter,
        }:
            raise ProviderPayloadError("youtube_child_failed")
        child_claim = repository.claim_job(
            child.id,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        if child_claim is None:
            current_child = repository.get_job(child.id)
            if (
                current_child is not None
                and current_child.status is JobStatus.processing
            ):
                continue
            raise ProviderTemporaryError("youtube_child_unavailable")
        if child_claim.started_at is None:
            raise ProviderTemporaryError("youtube_child_claim_invalid")
        try:
            dispatch_process_payload(
                child.id,
                payload,
                child_claim.started_at,
            )
        except Exception as error:
            _record_retry(
                repository,
                child.id,
                error,
                0,
                claim_started_at=child_claim.started_at,
                error_code="process_dispatch_error",
                error_message="Process queue dispatch failed",
            )
            raise ProviderTemporaryError(
                "youtube_process_dispatch_failed"
            ) from error
    try:
        finalize_youtube_profile.apply_async(
            args=(
                str(parent_job_id),
                claim_started_at.isoformat(),
            ),
        )
    except Exception as error:
        raise ProviderTemporaryError(
            "youtube_finalize_dispatch_failed"
        ) from error


@celery_app.task(
    bind=True,
    name="app.workers.youtube_poller.finalize_profile",
)
def finalize_youtube_profile(
    self,
    job_id: str,
    claim_started_at: str,
) -> dict[str, int] | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    owner_token = datetime.fromisoformat(claim_started_at)
    if owner_token.tzinfo is None:
        raise ValueError("Profile ownership token must be timezone-aware")
    current = repository.get_job(parsed_job_id)
    if current is None:
        raise KeyError(f"Import job {parsed_job_id} not found")
    if current.status is JobStatus.completed:
        return profile_job_counts(current)
    if (
        current.status is not JobStatus.processing
        or current.started_at != owner_token
    ):
        return None
    if current.profile_id is None:
        raise ValueError(
            f"Import job {parsed_job_id} has no search profile"
        )
    payloads, next_page_token = _load_profile_checkpoint(
        repository,
        current.profile_id,
        parsed_job_id,
        claim_started_at=owner_token,
    )
    children: list[ImportJob] = []
    for payload in payloads:
        child = repository.get_or_create_child_job(
            parsed_job_id,
            owner_token,
            payload,
        )
        if child is None:
            return None
        children.append(child)
    if any(
        child.status
        in {
            JobStatus.queued,
            JobStatus.processing,
            JobStatus.retry,
        }
        for child in children
    ):
        raise self.retry(countdown=1, max_retries=None)
    if any(
        child.status
        in {
            JobStatus.failed,
            JobStatus.blocked,
            JobStatus.dead_letter,
        }
        for child in children
    ):
        finalized = repository.finalize_profile_job(
            parsed_job_id,
            owner_token,
            status=JobStatus.failed,
            next_page_token=next_page_token,
            result_count=0,
            discard_count=0,
            duplicate_count=0,
            error_code="youtube_child_failed",
            error_message="youtube_child_failed",
        )
        return None if finalized is not None else None
    counts = {
        "result_count": 0,
        "discard_count": 0,
        "duplicate_count": 0,
    }
    for child in children:
        outcome = child.details.get("outcome")
        if outcome == "discarded":
            counts["discard_count"] += 1
        elif outcome == "duplicate":
            counts["duplicate_count"] += 1
        elif outcome == "persisted":
            counts["result_count"] += 1
        else:
            raise ProviderPayloadError(
                "youtube_child_outcome_missing"
            )
    finalized = repository.finalize_profile_job(
        parsed_job_id,
        owner_token,
        status=JobStatus.completed,
        next_page_token=next_page_token,
        result_count=counts["result_count"],
        discard_count=counts["discard_count"],
        duplicate_count=counts["duplicate_count"],
        error_code=None,
        error_message=None,
    )
    return counts if finalized is not None else None


@celery_app.task(
    bind=True,
    name="app.workers.youtube_poller.poll_profile",
)
def run_youtube_profile(
    self,
    job_id: str,
) -> dict[str, int] | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    settings = get_settings()
    claim_ttl_seconds = settings.job_claim_ttl_seconds
    claimed = claim_profile_job(
        self,
        repository,
        parsed_job_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claimed is None:
        current = repository.get_job(parsed_job_id)
        if current is not None and current.status is JobStatus.completed:
            return profile_job_counts(current)
        return None
    if settings.provider_mode != "live":
        finalize_profile_failure(
            repository,
            claimed,
            error_code="provider_mode_fixture",
            status=JobStatus.blocked,
        )
        return None
    try:
        _process_profile(
            repository,
            claimed.profile_id,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        return None
    except ProfileOwnershipLost:
        return None
    except ProviderQuotaError as error:
        finalize_profile_failure(
            repository,
            claimed,
            error_code="youtube_quota_exceeded",
        )
        raise
    except ProviderTemporaryError as error:
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=claimed.started_at,
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    except ProviderError as error:
        finalize_profile_failure(
            repository,
            claimed,
            error_code=str(error),
        )
        raise
    except Exception:
        finalize_profile_failure(
            repository,
            claimed,
            error_code="youtube_worker_error",
        )
        raise


def _fail_direct_job(
    repository: Repository,
    job_id: UUID,
    *,
    claim_started_at: datetime,
    error_code: str,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=JobStatus.failed,
            finished_at=datetime.now(UTC),
            error_code=error_code,
            error_message=error_code,
        ),
    )


def _block_direct_job(
    repository: Repository,
    job_id: UUID,
    *,
    claim_started_at: datetime,
    error_code: str,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=JobStatus.blocked,
            finished_at=datetime.now(UTC),
            error_code=error_code,
            error_message=error_code,
        ),
    )


@celery_app.task(
    bind=True,
    name="app.workers.youtube_poller.import_url",
)
def import_url(self, job_id: str) -> str | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    job = repository.get_job(parsed_job_id)
    if job is None:
        raise KeyError(f"Import job {parsed_job_id} not found")
    if job.url is None:
        raise ValueError(f"Import job {parsed_job_id} has no URL")
    settings = get_settings()
    claim_ttl_seconds = settings.job_claim_ttl_seconds
    if job.status is JobStatus.completed:
        return (
            str(job.result_set_id)
            if job.result_set_id is not None
            else None
        )
    claimed = claim_or_reschedule(
        self,
        repository,
        parsed_job_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claimed is None or claimed.started_at is None:
        return None
    if settings.provider_mode != "live":
        _block_direct_job(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            error_code="provider_mode_fixture",
        )
        return None
    try:
        payload = asyncio.run(get_youtube_adapter().fetch(claimed.url))
    except ProviderTemporaryError as error:
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=claimed.started_at,
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    except ProviderError as error:
        _fail_direct_job(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            error_code=str(error),
        )
        raise
    except Exception:
        _fail_direct_job(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            error_code="youtube_worker_error",
        )
        raise
    try:
        dispatch_process_payload(
            parsed_job_id,
            payload,
            claimed.started_at,
        )
    except Exception as error:
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=claimed.started_at,
            error_code="process_dispatch_error",
            error_message="Process queue dispatch failed",
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    return None
