import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import get_settings
from app.repositories.base import Repository
from app.schemas.import_job import ImportJob, ImportJobPatch, JobStatus
from app.services.normalizer import RawSetPayload
from app.services.provider import (
    ProviderBlockedError,
    ProviderError,
    ProviderPayloadError,
    ProviderTemporaryError,
    get_provider_registry,
)
from app.workers.celery_app import celery_app
from app.workers.normalize_worker import _record_retry, get_worker_repository
from app.workers.process_dispatch import dispatch_process_payload
from app.workers.profile_jobs import (
    ProfileOwnershipLost,
    TERMINAL_JOB_STATUSES,
    claim_profile_job,
    finalize_profile_failure,
    profile_job_counts,
)
from app.workers.recovery import claim_or_reschedule


def get_ftm_adapter() -> object:
    return get_provider_registry().adapter("ftm")


def _transition_terminal(
    repository: Repository,
    job_id: UUID,
    *,
    claim_started_at: datetime,
    status: JobStatus,
    error_code: str,
) -> None:
    repository.transition_claimed_job(
        job_id,
        claim_started_at,
        ImportJobPatch(
            status=status,
            finished_at=datetime.now(UTC),
            error_code=error_code,
            error_message=error_code,
        ),
    )


def _blocked_code(error: ProviderBlockedError) -> str:
    if str(error) == "provider_mode_fixture":
        return "provider_mode_fixture"
    if str(error) == "ftm_robots_denied":
        return "robots_denied"
    if str(error) == "ftm_disabled":
        return "provider_disabled"
    return "provider_blocked"


_FTM_CHECKPOINT_KEY = "ftm_crawl_checkpoint"
def _load_ftm_checkpoint(
    repository: Repository,
    profile_id: UUID,
    parent_job_id: UUID,
    *,
    claim_started_at: datetime,
) -> list[RawSetPayload]:
    profile = repository.get_profile(profile_id)
    if profile is None:
        raise KeyError(f"Search profile {profile_id} not found")
    if profile.source != "ftm" or profile.operation != "crawl":
        raise ProviderPayloadError("ftm_profile_operation_invalid")
    current = repository.get_job(parent_job_id)
    if current is None:
        raise KeyError(f"Import job {parent_job_id} not found")
    checkpoint = current.details.get(_FTM_CHECKPOINT_KEY)
    if checkpoint is None:
        start_url = profile.parameters.get("start_url")
        if not isinstance(start_url, str):
            raise ProviderPayloadError("ftm_start_url_invalid")
        payloads = asyncio.run(
            get_ftm_adapter().crawl(
                start_url,
                max_pages=get_settings().ftm_max_pages_per_run,
            )
        )
        checkpointed = repository.checkpoint_profile_page(
            parent_job_id,
            claim_started_at,
            input_page_token=None,
            next_page_token=None,
            payloads=payloads,
            checkpoint_key=_FTM_CHECKPOINT_KEY,
        )
        if checkpointed is None:
            raise ProfileOwnershipLost()
        checkpoint = checkpointed.details.get(_FTM_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict):
        raise ProviderPayloadError("ftm_checkpoint_invalid")
    payload_values = checkpoint.get("payloads")
    source_ids = checkpoint.get("source_ids")
    if not isinstance(payload_values, list) or not isinstance(source_ids, list):
        raise ProviderPayloadError("ftm_checkpoint_invalid")
    try:
        payloads = [
            RawSetPayload.model_validate(value)
            for value in payload_values
        ]
    except Exception as error:
        raise ProviderPayloadError("ftm_checkpoint_invalid") from error
    if [payload.source_id for payload in payloads] != source_ids:
        raise ProviderPayloadError("ftm_checkpoint_invalid")
    return payloads


def _process_ftm_profile(
    repository: Repository,
    profile_id: UUID,
    parent_job_id: UUID,
    *,
    claim_started_at: datetime,
    claim_ttl_seconds: int,
) -> None:
    payloads = _load_ftm_checkpoint(
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
            raise ProviderPayloadError("ftm_child_failed")
        child_claim = repository.claim_job(
            child.id,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        if child_claim is None:
            current_child = repository.get_job(child.id)
            if current_child is not None and current_child.status is JobStatus.processing:
                continue
            raise ProviderTemporaryError("ftm_child_unavailable")
        if child_claim.started_at is None:
            raise ProviderTemporaryError("ftm_child_claim_invalid")
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
            raise ProviderTemporaryError("ftm_process_dispatch_failed") from error
    try:
        finalize_ftm_profile.apply_async(
            args=(str(parent_job_id), claim_started_at.isoformat()),
        )
    except Exception as error:
        raise ProviderTemporaryError("ftm_finalize_dispatch_failed") from error


@celery_app.task(
    bind=True,
    name="app.workers.ftm_scraper.finalize_profile",
)
def finalize_ftm_profile(
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
    if current.status is not JobStatus.processing or current.started_at != owner_token:
        return None
    if current.profile_id is None:
        raise ValueError(f"Import job {parsed_job_id} has no search profile")
    payloads = _load_ftm_checkpoint(
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
        child.status in {JobStatus.queued, JobStatus.processing, JobStatus.retry}
        for child in children
    ):
        raise self.retry(countdown=1, max_retries=None)
    if any(child.status in TERMINAL_JOB_STATUSES - {JobStatus.completed} for child in children):
        repository.finalize_profile_job(
            parsed_job_id,
            owner_token,
            status=JobStatus.failed,
            next_page_token=None,
            result_count=0,
            discard_count=0,
            duplicate_count=0,
            error_code="ftm_child_failed",
            error_message="ftm_child_failed",
        )
        return None
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
            raise ProviderPayloadError("ftm_child_outcome_missing")
    finalized = repository.finalize_profile_job(
        parsed_job_id,
        owner_token,
        status=JobStatus.completed,
        next_page_token=None,
        result_count=counts["result_count"],
        discard_count=counts["discard_count"],
        duplicate_count=counts["duplicate_count"],
        error_code=None,
        error_message=None,
    )
    return counts if finalized is not None else None


@celery_app.task(
    bind=True,
    name="app.workers.ftm_scraper.crawl_profile",
)
def run_ftm_profile(self, job_id: str) -> dict[str, int] | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    settings = get_settings()
    claimed = claim_profile_job(
        self,
        repository,
        parsed_job_id,
        claim_ttl_seconds=settings.job_claim_ttl_seconds,
    )
    if claimed is None:
        current = repository.get_job(parsed_job_id)
        if current is not None and current.status is JobStatus.completed:
            return profile_job_counts(current)
        return None
    if claimed.started_at is None or claimed.profile_id is None:
        raise ValueError(f"Import job {parsed_job_id} has no profile ownership")
    if settings.provider_mode != "live":
        finalize_profile_failure(
            repository,
            claimed,
            error_code="provider_mode_fixture",
            status=JobStatus.blocked,
        )
        return None
    if not settings.ftm_scraper_enabled:
        finalize_profile_failure(
            repository,
            claimed,
            error_code="provider_disabled",
            status=JobStatus.blocked,
        )
        return None
    try:
        _process_ftm_profile(
            repository,
            claimed.profile_id,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            claim_ttl_seconds=settings.job_claim_ttl_seconds,
        )
        return None
    except ProfileOwnershipLost:
        return None
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
        raise self.retry(exc=error, countdown=delay, max_retries=3)
    except ProviderBlockedError as error:
        finalize_profile_failure(
            repository,
            claimed,
            error_code=_blocked_code(error),
            status=JobStatus.blocked,
        )
        return None
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
            error_code="ftm_profile_worker_error",
        )
        raise


@celery_app.task(
    bind=True,
    name="app.workers.ftm_scraper.import_url",
)
def import_ftm(self, job_id: str) -> str | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    job = repository.get_job(parsed_job_id)
    if job is None:
        raise KeyError(f"Import job {parsed_job_id} not found")
    if job.url is None:
        raise ValueError(f"Import job {parsed_job_id} has no URL")
    if job.status is JobStatus.completed:
        return str(job.result_set_id) if job.result_set_id else None

    settings = get_settings()
    claim_ttl_seconds = settings.job_claim_ttl_seconds
    claimed = claim_or_reschedule(
        self,
        repository,
        parsed_job_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claimed is None or claimed.started_at is None:
        return None
    try:
        if settings.provider_mode != "live":
            raise ProviderBlockedError("provider_mode_fixture")
        if not settings.ftm_scraper_enabled:
            raise ProviderBlockedError("ftm_disabled")
        payload = asyncio.run(get_ftm_adapter().fetch(claimed.url))
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
        raise self.retry(exc=error, countdown=delay, max_retries=3)
    except ProviderBlockedError as error:
        _transition_terminal(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            status=JobStatus.blocked,
            error_code=_blocked_code(error),
        )
        return None
    except ProviderError as error:
        _transition_terminal(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            status=JobStatus.failed,
            error_code=str(error),
        )
        raise
    except Exception:
        _transition_terminal(
            repository,
            parsed_job_id,
            claim_started_at=claimed.started_at,
            status=JobStatus.failed,
            error_code="ftm_worker_error",
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
        raise self.retry(exc=error, countdown=delay, max_retries=3)
    return None
