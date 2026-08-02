from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Callable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.schemas.audio import (
    AudioAssetRecord,
    AudioAssetState,
    AudioBucket,
    AudioInputJob,
    AudioInputKind,
    AudioInputStatus,
)
from app.schemas.rights import RightsReview, RightsReviewStatus
from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    StoredAudioObject,
)
from app.services.provider_contracts import AuthorizedAudioCandidate
from app.services.rights_policy import rights_evidence_complete


Clock = Callable[[], datetime]
_ACTIVE_STATUSES = frozenset(
    {
        AudioInputStatus.queued,
        AudioInputStatus.processing,
        AudioInputStatus.retry,
    }
)


class AudioAcquisitionPersistenceError(RuntimeError):
    """Base error for durable rights-gated audio input state."""


class AudioAcquisitionPersistenceDenied(AudioAcquisitionPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class _RightsSnapshot:
    id: UUID
    set_id: UUID
    provider_id: UUID | None
    provider_key: str
    provider_external_id: str
    status: RightsReviewStatus
    allow_download: bool
    permission_download: bool
    expires_at: datetime | None
    evidence_urls: frozenset[str]
    evidence_complete: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _review_snapshot(review: RightsReview) -> _RightsSnapshot:
    return _RightsSnapshot(
        id=review.id,
        set_id=review.set_id,
        provider_id=None,
        provider_key=review.provider_key,
        provider_external_id=review.provider_external_id,
        status=review.status,
        allow_download=review.allow_download,
        permission_download=review.allow_download,
        expires_at=review.expires_at,
        evidence_urls=frozenset(str(item.reference_url) for item in review.evidence),
        evidence_complete=rights_evidence_complete(tuple(review.evidence)),
    )


def _validate_queue_request(
    snapshot: _RightsSnapshot,
    *,
    provider_item_external_id: str,
    candidate: AuthorizedAudioCandidate,
    now: datetime,
) -> None:
    if snapshot.status is not RightsReviewStatus.approved:
        raise AudioAcquisitionPersistenceDenied(
            "rights review must be approved before audio acquisition"
        )
    if not snapshot.allow_download or not snapshot.permission_download:
        raise AudioAcquisitionPersistenceDenied(
            "rights review does not grant download permission"
        )
    if snapshot.expires_at is not None and snapshot.expires_at <= now:
        raise AudioAcquisitionPersistenceDenied(
            "rights review has expired"
        )
    if (
        snapshot.provider_key != candidate.provider_key
        or snapshot.provider_external_id != provider_item_external_id
        or candidate.external_id != provider_item_external_id
    ):
        raise AudioAcquisitionPersistenceDenied(
            "provider identity does not match the approved rights review"
        )
    candidate_evidence = frozenset(
        str(reference) for reference in candidate.evidence_references
    )
    if (
        not snapshot.evidence_complete
        or not candidate_evidence
        or not candidate_evidence.issubset(snapshot.evidence_urls)
    ):
        raise AudioAcquisitionPersistenceDenied(
            "candidate evidence does not match the approved rights review"
        )


def _validate_stored_object(stored: StoredAudioObject) -> None:
    if stored.bucket != AUDIO_QUARANTINE_BUCKET:
        raise AudioAcquisitionPersistenceDenied(
            "completed acquisition must remain in audio-quarantine"
        )
    if stored.sha256 is None or len(stored.sha256) != 64:
        raise AudioAcquisitionPersistenceDenied(
            "completed acquisition requires a SHA-256 checksum"
        )
    if stored.size < 1:
        raise AudioAcquisitionPersistenceDenied(
            "completed acquisition has invalid size"
        )


def _job_from_row(row: dict[str, Any]) -> AudioInputJob:
    return AudioInputJob(
        id=row["id"],
        rights_review_id=row["rights_review_id"],
        input_kind=AudioInputKind(row["input_kind"]),
        provider_key=row.get("provider_key"),
        provider_item_external_id=row.get("provider_item_external_id"),
        candidate_external_id=row["candidate_external_id"],
        source_url=row.get("source_url"),
        expected_sha256=row.get("expected_sha256"),
        status=AudioInputStatus(row["status"]),
        attempt_count=row["attempt_count"],
        claim_started_at=row.get("claim_started_at"),
        next_retry_at=row.get("next_retry_at"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        audio_asset_id=row.get("audio_asset_id"),
        created_by=row["created_by"],
        details=row.get("details") or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _asset_from_row(row: dict[str, Any]) -> AudioAssetRecord:
    return AudioAssetRecord(
        id=row["id"],
        rights_review_id=row["rights_review_id"],
        state=AudioAssetState(row["state"]),
        bucket_name=AudioBucket(row["bucket_name"]),
        object_key=row["object_key"],
        checksum_sha256=row["checksum_sha256"],
        size_bytes=row["size_bytes"],
        content_type=row.get("content_type"),
        expires_at=row.get("expires_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class InMemoryAudioRepository:
    def __init__(
        self,
        rights_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._rights_repository = rights_repository
        self._clock = clock
        self._lock = RLock()
        self.jobs: dict[UUID, AudioInputJob] = {}
        self.assets: dict[UUID, AudioAssetRecord] = {}

    def queue_provider_acquisition(
        self,
        review_id: UUID,
        *,
        provider_item_external_id: str,
        candidate: AuthorizedAudioCandidate,
        actor: str,
    ) -> AudioInputJob:
        with self._lock:
            review = self._rights_repository.get_rights_review(review_id)
            if review is None:
                raise AudioAcquisitionPersistenceDenied(
                    "approved rights review was not found"
                )
            now = self._clock()
            _validate_queue_request(
                _review_snapshot(review),
                provider_item_external_id=provider_item_external_id,
                candidate=candidate,
                now=now,
            )
            for job in self.jobs.values():
                if (
                    job.rights_review_id == review_id
                    and job.input_kind is AudioInputKind.provider_acquisition
                    and job.candidate_external_id == candidate.external_id
                    and job.status in _ACTIVE_STATUSES
                ):
                    return job

            job = AudioInputJob(
                rights_review_id=review_id,
                input_kind=AudioInputKind.provider_acquisition,
                provider_key=candidate.provider_key,
                provider_item_external_id=provider_item_external_id,
                candidate_external_id=candidate.external_id,
                source_url=str(candidate.source_url),
                expected_sha256=candidate.expected_sha256,
                created_by=actor,
                details={
                    "evidence_references": [
                        str(reference)
                        for reference in candidate.evidence_references
                    ],
                    "provider_evidence": candidate.evidence,
                },
                created_at=now,
                updated_at=now,
            )
            self.jobs[job.id] = job
            return job

    def claim_audio_job(
        self,
        job_id: UUID,
        *,
        claim_ttl_seconds: int,
    ) -> AudioInputJob | None:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            now = self._clock()
            stale_before = now - timedelta(seconds=claim_ttl_seconds)
            eligible = job.status in {
                AudioInputStatus.queued,
                AudioInputStatus.retry,
            }
            if (
                job.status is AudioInputStatus.processing
                and job.claim_started_at is not None
                and job.claim_started_at <= stale_before
            ):
                eligible = True
            if not eligible:
                return None

            claimed = job.model_copy(
                update={
                    "status": AudioInputStatus.processing,
                    "attempt_count": job.attempt_count + 1,
                    "claim_started_at": now,
                    "started_at": job.started_at or now,
                    "next_retry_at": None,
                    "updated_at": now,
                }
            )
            self.jobs[job.id] = claimed
            return claimed

    def complete_audio_acquisition(
        self,
        job_id: UUID,
        *,
        claim_started_at: datetime,
        stored: StoredAudioObject,
    ) -> tuple[AudioInputJob, AudioAssetRecord] | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status is not AudioInputStatus.processing
                or job.claim_started_at != claim_started_at
            ):
                return None
            review = self._rights_repository.get_rights_review(
                job.rights_review_id
            )
            if review is None:
                raise AudioAcquisitionPersistenceDenied(
                    "approved rights review was not found"
                )
            now = self._clock()
            candidate = AuthorizedAudioCandidate(
                provider_key=job.provider_key or "",
                external_id=job.candidate_external_id,
                source_url=job.source_url or "",
                evidence_references=tuple(
                    job.details.get("evidence_references", [])
                ),
                expected_sha256=job.expected_sha256,
                evidence=job.details.get("provider_evidence", {}),
            )
            _validate_queue_request(
                _review_snapshot(review),
                provider_item_external_id=job.provider_item_external_id or "",
                candidate=candidate,
                now=now,
            )
            _validate_stored_object(stored)

            asset = AudioAssetRecord(
                id=uuid4(),
                rights_review_id=job.rights_review_id,
                state=AudioAssetState.quarantine,
                bucket_name=AudioBucket.quarantine,
                object_key=stored.key,
                checksum_sha256=stored.sha256 or "",
                size_bytes=stored.size,
                content_type=stored.content_type,
                expires_at=now + timedelta(days=30),
                created_at=now,
                updated_at=now,
            )
            completed = job.model_copy(
                update={
                    "status": AudioInputStatus.completed,
                    "audio_asset_id": asset.id,
                    "finished_at": now,
                    "updated_at": now,
                }
            )
            self.assets[asset.id] = asset
            self.jobs[job.id] = completed
            return completed, asset

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        return self.assets.get(asset_id)


class PostgresAudioRepository:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._clock = clock

    @staticmethod
    def _select_review(
        cursor: Any,
        review_id: UUID,
        *,
        for_update: bool,
    ) -> _RightsSnapshot | None:
        suffix = " for update of rr" if for_update else ""
        row = cursor.execute(
            f"""
            select rr.id, rr.set_id, rr.provider_id,
                   p.key as provider_key,
                   rr.provider_external_id, rr.status,
                   rr.allow_download, rr.expires_at,
                   exists (
                     select 1 from audio_permissions ap
                     where ap.rights_review_id = rr.id
                       and ap.allow_download
                       and ap.revoked_at is null
                   ) as permission_download
            from rights_reviews rr
            join providers p on p.id = rr.provider_id
            where rr.id = %s{suffix}
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        evidence_rows = cursor.execute(
            """
            select evidence_type, reference_url, assertions
            from rights_evidence
            where rights_review_id = %s
            order by created_at, id
            """,
            (review_id,),
        ).fetchall()
        from app.schemas.rights import RightsEvidenceInput, RightsEvidenceType

        evidence = tuple(
            RightsEvidenceInput(
                evidence_type=RightsEvidenceType(item["evidence_type"]),
                reference_url=item["reference_url"],
                assertions=item["assertions"] or {},
            )
            for item in evidence_rows
        )
        return _RightsSnapshot(
            id=row["id"],
            set_id=row["set_id"],
            provider_id=row["provider_id"],
            provider_key=row["provider_key"],
            provider_external_id=row["provider_external_id"],
            status=RightsReviewStatus(row["status"]),
            allow_download=row["allow_download"],
            permission_download=row["permission_download"],
            expires_at=row["expires_at"],
            evidence_urls=frozenset(
                str(item.reference_url) for item in evidence
            ),
            evidence_complete=rights_evidence_complete(evidence),
        )

    def queue_provider_acquisition(
        self,
        review_id: UUID,
        *,
        provider_item_external_id: str,
        candidate: AuthorizedAudioCandidate,
        actor: str,
    ) -> AudioInputJob:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                snapshot = self._select_review(
                    cursor,
                    review_id,
                    for_update=True,
                )
                if snapshot is None:
                    raise AudioAcquisitionPersistenceDenied(
                        "approved rights review was not found"
                    )
                _validate_queue_request(
                    snapshot,
                    provider_item_external_id=provider_item_external_id,
                    candidate=candidate,
                    now=now,
                )
                existing = cursor.execute(
                    """
                    select j.*, p.key as provider_key
                    from audio_input_jobs j
                    left join providers p on p.id = j.provider_id
                    where j.rights_review_id = %s
                      and j.input_kind = 'provider_acquisition'
                      and j.candidate_external_id = %s
                      and j.status in ('queued', 'processing', 'retry')
                    order by j.created_at, j.id
                    limit 1
                    """,
                    (review_id, candidate.external_id),
                ).fetchone()
                if existing is not None:
                    return _job_from_row(existing)

                details = {
                    "evidence_references": [
                        str(reference)
                        for reference in candidate.evidence_references
                    ],
                    "provider_evidence": candidate.evidence,
                }
                row = cursor.execute(
                    """
                    insert into audio_input_jobs (
                      rights_review_id, provider_id,
                      provider_item_external_id, candidate_external_id,
                      input_kind, source_url, expected_sha256,
                      status, attempt_count, created_by, details,
                      created_at, updated_at
                    ) values (
                      %s, %s, %s, %s,
                      'provider_acquisition', %s, %s,
                      'queued', 0, %s, %s,
                      %s, %s
                    )
                    returning *, (
                      select key from providers where id = provider_id
                    ) as provider_key
                    """,
                    (
                        review_id,
                        snapshot.provider_id,
                        provider_item_external_id,
                        candidate.external_id,
                        str(candidate.source_url),
                        candidate.expected_sha256,
                        actor,
                        Jsonb(details),
                        now,
                        now,
                    ),
                ).fetchone()
                return _job_from_row(row)

    def claim_audio_job(
        self,
        job_id: UUID,
        *,
        claim_ttl_seconds: int,
    ) -> AudioInputJob | None:
        if claim_ttl_seconds < 1:
            raise ValueError("claim_ttl_seconds must be positive")
        now = self._clock()
        stale_before = now - timedelta(seconds=claim_ttl_seconds)
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select j.*, p.key as provider_key
                    from audio_input_jobs j
                    left join providers p on p.id = j.provider_id
                    where j.id = %s
                    for update of j
                    """,
                    (job_id,),
                ).fetchone()
                if current is None:
                    return None
                eligible = current["status"] in {"queued", "retry"}
                if (
                    current["status"] == "processing"
                    and current["claim_started_at"] is not None
                    and current["claim_started_at"] <= stale_before
                ):
                    eligible = True
                if not eligible:
                    return None
                row = cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'processing',
                        attempt_count = attempt_count + 1,
                        claim_started_at = %s,
                        started_at = coalesce(started_at, %s),
                        next_retry_at = null,
                        updated_at = %s
                    where id = %s
                    returning *, (
                      select key from providers where id = provider_id
                    ) as provider_key
                    """,
                    (now, now, now, job_id),
                ).fetchone()
                return _job_from_row(row)

    def complete_audio_acquisition(
        self,
        job_id: UUID,
        *,
        claim_started_at: datetime,
        stored: StoredAudioObject,
    ) -> tuple[AudioInputJob, AudioAssetRecord] | None:
        _validate_stored_object(stored)
        now = self._clock()
        expires_at = now + timedelta(days=30)
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select j.*, p.key as provider_key
                    from audio_input_jobs j
                    left join providers p on p.id = j.provider_id
                    where j.id = %s
                    for update of j
                    """,
                    (job_id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "processing"
                    or current["claim_started_at"] != claim_started_at
                ):
                    return None
                snapshot = self._select_review(
                    cursor,
                    current["rights_review_id"],
                    for_update=True,
                )
                if snapshot is None:
                    raise AudioAcquisitionPersistenceDenied(
                        "approved rights review was not found"
                    )
                candidate = AuthorizedAudioCandidate(
                    provider_key=current["provider_key"],
                    external_id=current["candidate_external_id"],
                    source_url=current["source_url"],
                    evidence_references=tuple(
                        (current["details"] or {}).get(
                            "evidence_references", []
                        )
                    ),
                    expected_sha256=current["expected_sha256"],
                    evidence=(current["details"] or {}).get(
                        "provider_evidence", {}
                    ),
                )
                _validate_queue_request(
                    snapshot,
                    provider_item_external_id=current[
                        "provider_item_external_id"
                    ],
                    candidate=candidate,
                    now=now,
                )

                asset_row = cursor.execute(
                    """
                    insert into audio_assets (
                      rights_review_id, bucket_name, object_key,
                      checksum_sha256, size_bytes, content_type,
                      state, expires_at, created_at, updated_at
                    ) values (
                      %s, 'audio-quarantine', %s,
                      %s, %s, %s,
                      'quarantine', %s, %s, %s
                    )
                    returning *
                    """,
                    (
                        current["rights_review_id"],
                        stored.key,
                        stored.sha256,
                        stored.size,
                        stored.content_type,
                        expires_at,
                        now,
                        now,
                    ),
                ).fetchone()
                asset = _asset_from_row(asset_row)
                job_row = cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'completed',
                        audio_asset_id = %s,
                        finished_at = %s,
                        updated_at = %s
                    where id = %s
                      and status = 'processing'
                      and claim_started_at = %s
                    returning *, (
                      select key from providers where id = provider_id
                    ) as provider_key
                    """,
                    (asset.id, now, now, job_id, claim_started_at),
                ).fetchone()
                if job_row is None:
                    raise AudioAcquisitionPersistenceError(
                        "audio claim changed during completion"
                    )
                return _job_from_row(job_row), asset

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from audio_assets where id = %s",
                    (asset_id,),
                ).fetchone()
        return None if row is None else _asset_from_row(row)
