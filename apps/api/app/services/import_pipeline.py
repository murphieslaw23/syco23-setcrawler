from datetime import datetime
from uuid import UUID

from app.repositories.base import Repository
from app.schemas.import_job import JobStatus
from app.services.enricher import extract_field_candidates
from app.services.heuristic import calculate_set_score
from app.services.normalizer import RawSetPayload, duplicate_fingerprint


RETRY_DELAYS = (5, 30, 120)


def retry_delay(attempt: int) -> int:
    return RETRY_DELAYS[attempt - 1]


def process_payload(
    repository: Repository,
    job_id: UUID,
    payload: RawSetPayload,
    *,
    claim_ttl_seconds: int = 300,
    claim_started_at: datetime | None = None,
) -> UUID | None:
    job = repository.get_job(job_id)
    if job is None:
        raise KeyError(f"Import job {job_id} not found")
    if job.status is JobStatus.completed:
        return job.result_set_id
    if claim_started_at is not None:
        if (
            job.status is not JobStatus.processing
            or job.started_at != claim_started_at
        ):
            return None
    elif job.status in {
        JobStatus.queued,
        JobStatus.retry,
        JobStatus.processing,
    }:
        job = repository.claim_job(
            job_id,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        if job is None:
            current = repository.get_job(job_id)
            if current is None:
                raise KeyError(f"Import job {job_id} not found")
            if current.status is JobStatus.completed:
                return current.result_set_id
            if current.status is JobStatus.processing:
                return None
    if job is None or job.status is not JobStatus.processing:
        raise ValueError(
            f"Import job {job_id} is not available for processing"
        )
    if job.started_at is None:
        raise ValueError(f"Import job {job_id} has no ownership token")
    owner_token = job.started_at

    score = calculate_set_score(
        payload.title,
        payload.duration_seconds or 0,
        repository.get_heuristic_config(),
    )
    fingerprint = duplicate_fingerprint(
        payload.title,
        payload.duration_seconds or 0,
    )
    duplicate_id = repository.find_duplicate(payload, fingerprint)
    if duplicate_id:
        completed = repository.complete_duplicate_job(
            job_id,
            duplicate_id,
            claim_started_at=owner_token,
        )
        return duplicate_id if completed is not None else None
    if not score.accepted:
        repository.complete_discarded_job(
            job_id,
            score,
            claim_started_at=owner_token,
        )
        return None
    candidates = extract_field_candidates(
        payload.title,
        payload.description,
    )
    persisted_id = repository.persist_processed_set(
        payload=payload,
        score=score,
        candidates=candidates,
        job_id=job_id,
        fingerprint=fingerprint,
        claim_started_at=owner_token,
    )
    if persisted_id is not None:
        repository.suggest_merge_candidates(persisted_id)
    return persisted_id
