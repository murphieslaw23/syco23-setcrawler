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
from app.schemas.creator_upload import (
    CreatorUploadAttestation,
    CreatorUploadSession,
    CreatorUploadStart,
    CreatorUploadStatus,
)
from app.schemas.rights import RightsEvidence, RightsEvidenceType, RightsReviewStatus
from app.services.audio_storage import AUDIO_QUARANTINE_BUCKET, StoredAudioObject


Clock = Callable[[], datetime]
CREATOR_UPLOAD_SESSION_TTL = timedelta(hours=24)
QUARANTINE_RETENTION = timedelta(days=30)


class CreatorUploadPersistenceError(RuntimeError):
    """Base error for private resumable creator-upload persistence."""


class CreatorUploadPersistenceDenied(CreatorUploadPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class _ReviewSnapshot:
    id: UUID
    status: RightsReviewStatus
    expires_at: datetime | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_review(snapshot: _ReviewSnapshot, *, now: datetime) -> None:
    if snapshot.status in {RightsReviewStatus.rejected, RightsReviewStatus.expired}:
        raise CreatorUploadPersistenceDenied(
            "creator upload requires an active rights review"
        )
    if snapshot.expires_at is not None and snapshot.expires_at <= now:
        raise CreatorUploadPersistenceDenied("creator upload rights review has expired")


def _validate_stored_upload(
    session: CreatorUploadSession,
    stored: StoredAudioObject,
) -> None:
    if stored.bucket != AUDIO_QUARANTINE_BUCKET:
        raise CreatorUploadPersistenceDenied(
            "creator upload must remain in audio-quarantine"
        )
    if stored.key != session.staging_object_key:
        raise CreatorUploadPersistenceDenied(
            "stored object does not match the creator upload session"
        )
    if stored.size != session.expected_size_bytes:
        raise CreatorUploadPersistenceDenied(
            "stored object size does not match the creator upload declaration"
        )
    if stored.content_type != session.content_type:
        raise CreatorUploadPersistenceDenied(
            "stored object content type does not match the creator upload declaration"
        )
    if stored.sha256 is None or len(stored.sha256) != 64:
        raise CreatorUploadPersistenceDenied(
            "stored creator upload requires a SHA-256 checksum"
        )
    if (
        session.expected_sha256 is not None
        and stored.sha256 != session.expected_sha256
    ):
        raise CreatorUploadPersistenceDenied(
            "stored creator upload checksum does not match the declaration"
        )


def _session_from_row(row: dict[str, Any]) -> CreatorUploadSession:
    return CreatorUploadSession.model_validate(row)


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


def _updated_session(
    session: CreatorUploadSession,
    **changes: Any,
) -> CreatorUploadSession:
    return CreatorUploadSession.model_validate(
        {**session.model_dump(), **changes}
    )


class InMemoryCreatorUploadRepository:
    def __init__(
        self,
        rights_repository: Any,
        audio_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._rights_repository = rights_repository
        self._audio_repository = audio_repository
        self._clock = clock
        self._lock = RLock()
        self.sessions: dict[UUID, CreatorUploadSession] = {}

    def create_creator_upload(
        self,
        review_id: UUID,
        *,
        payload: CreatorUploadStart,
        actor: str,
    ) -> tuple[AudioInputJob, CreatorUploadSession]:
        with self._lock:
            review = self._rights_repository.get_rights_review(review_id)
            if review is None:
                raise CreatorUploadPersistenceDenied("rights review was not found")
            now = self._clock()
            _validate_review(
                _ReviewSnapshot(
                    id=review.id,
                    status=review.status,
                    expires_at=review.expires_at,
                ),
                now=now,
            )
            session_id = uuid4()
            job = AudioInputJob(
                rights_review_id=review_id,
                input_kind=AudioInputKind.creator_upload,
                candidate_external_id=f"creator-upload:{session_id}",
                expected_sha256=payload.expected_sha256,
                created_by=actor,
                details={
                    "expected_size_bytes": payload.expected_size_bytes,
                    "content_type": payload.content_type,
                },
                created_at=now,
                updated_at=now,
            )
            session = CreatorUploadSession(
                id=session_id,
                audio_input_job_id=job.id,
                rights_review_id=review_id,
                expected_size_bytes=payload.expected_size_bytes,
                content_type=payload.content_type,
                expected_sha256=payload.expected_sha256,
                expires_at=now + CREATOR_UPLOAD_SESSION_TTL,
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            self._audio_repository.jobs[job.id] = job
            self.sessions[session.id] = session
            return job, session

    def get_creator_upload(
        self,
        session_id: UUID,
    ) -> CreatorUploadSession | None:
        return self.sessions.get(session_id)

    def begin_creator_upload(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        staging_object_key: str,
        storage_upload_id: str,
    ) -> CreatorUploadSession | None:
        with self._lock:
            session = self.sessions.get(session_id)
            if (
                session is None
                or session.version != expected_version
                or session.status is not CreatorUploadStatus.initiated
            ):
                return None
            now = self._clock()
            if session.expires_at <= now:
                raise CreatorUploadPersistenceDenied("creator upload session has expired")
            job = self._audio_repository.jobs.get(session.audio_input_job_id)
            if job is None or job.status is not AudioInputStatus.queued:
                return None
            updated = _updated_session(
                session,
                status=CreatorUploadStatus.uploading,
                staging_object_key=staging_object_key,
                storage_upload_id=storage_upload_id,
                version=session.version + 1,
                updated_at=now,
            )
            claimed_job = job.model_copy(
                update={
                    "status": AudioInputStatus.processing,
                    "attempt_count": job.attempt_count + 1,
                    "claim_started_at": now,
                    "started_at": now,
                    "updated_at": now,
                }
            )
            self._audio_repository.jobs[job.id] = claimed_job
            self.sessions[session.id] = updated
            return updated

    def record_creator_upload_progress(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        received_size_bytes: int,
    ) -> CreatorUploadSession | None:
        with self._lock:
            session = self.sessions.get(session_id)
            if (
                session is None
                or session.version != expected_version
                or session.status is not CreatorUploadStatus.uploading
            ):
                return None
            if received_size_bytes < session.received_size_bytes:
                raise ValueError("creator upload progress cannot move backwards")
            if received_size_bytes > session.expected_size_bytes:
                raise ValueError("creator upload progress exceeds the declared size")
            now = self._clock()
            status = (
                CreatorUploadStatus.awaiting_attestation
                if received_size_bytes == session.expected_size_bytes
                else CreatorUploadStatus.uploading
            )
            updated = _updated_session(
                session,
                received_size_bytes=received_size_bytes,
                status=status,
                version=session.version + 1,
                updated_at=now,
            )
            self.sessions[session.id] = updated
            return updated

    def complete_creator_upload(
        self,
        session_id: UUID,
        *,
        attestation: CreatorUploadAttestation,
        actor: str,
        stored: StoredAudioObject,
    ) -> tuple[CreatorUploadSession, AudioInputJob, AudioAssetRecord] | None:
        with self._lock:
            session = self.sessions.get(session_id)
            if (
                session is None
                or session.version != attestation.expected_version
                or session.status is not CreatorUploadStatus.awaiting_attestation
            ):
                return None
            now = self._clock()
            review = self._rights_repository.get_rights_review(
                session.rights_review_id
            )
            if review is None:
                raise CreatorUploadPersistenceDenied("rights review was not found")
            _validate_review(
                _ReviewSnapshot(
                    id=review.id,
                    status=review.status,
                    expires_at=review.expires_at,
                ),
                now=now,
            )
            _validate_stored_upload(session, stored)
            job = self._audio_repository.jobs.get(session.audio_input_job_id)
            if job is None or job.status is not AudioInputStatus.processing:
                return None

            evidence = RightsEvidence(
                rights_review_id=review.id,
                evidence_type=RightsEvidenceType.creator_attestation,
                reference_url=attestation.reference_url,
                assertions=attestation.assertions,
                submitted_by=actor,
                created_at=now,
            )
            asset = AudioAssetRecord(
                rights_review_id=review.id,
                state=AudioAssetState.quarantine,
                bucket_name=AudioBucket.quarantine,
                object_key=stored.key,
                checksum_sha256=stored.sha256 or "",
                size_bytes=stored.size,
                content_type=stored.content_type,
                expires_at=now + QUARANTINE_RETENTION,
                created_at=now,
                updated_at=now,
            )
            completed_job = job.model_copy(
                update={
                    "status": AudioInputStatus.completed,
                    "audio_asset_id": asset.id,
                    "finished_at": now,
                    "updated_at": now,
                }
            )
            completed_session = _updated_session(
                session,
                status=CreatorUploadStatus.completed,
                attestation_evidence_id=evidence.id,
                attested_by=actor,
                attested_at=now,
                version=session.version + 1,
                updated_at=now,
            )
            updated_review = review.model_copy(
                update={
                    "evidence": [*review.evidence, evidence],
                    "updated_at": now,
                }
            )
            if not hasattr(self._rights_repository, "rights_reviews"):
                raise CreatorUploadPersistenceError(
                    "in-memory rights repository does not support evidence append"
                )

            self._rights_repository.rights_reviews[review.id] = updated_review
            self._audio_repository.assets[asset.id] = asset
            self._audio_repository.jobs[job.id] = completed_job
            self.sessions[session.id] = completed_session
            return completed_session, completed_job, asset


class PostgresCreatorUploadRepository:
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
    ) -> _ReviewSnapshot | None:
        suffix = " for update" if for_update else ""
        row = cursor.execute(
            f"""
            select id, status, expires_at
            from rights_reviews
            where id = %s{suffix}
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        return _ReviewSnapshot(
            id=row["id"],
            status=RightsReviewStatus(row["status"]),
            expires_at=row["expires_at"],
        )

    def create_creator_upload(
        self,
        review_id: UUID,
        *,
        payload: CreatorUploadStart,
        actor: str,
    ) -> tuple[AudioInputJob, CreatorUploadSession]:
        now = self._clock()
        session_id = uuid4()
        job_id = uuid4()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                review = self._select_review(
                    cursor,
                    review_id,
                    for_update=True,
                )
                if review is None:
                    raise CreatorUploadPersistenceDenied("rights review was not found")
                _validate_review(review, now=now)
                job_row = cursor.execute(
                    """
                    insert into audio_input_jobs (
                      id, rights_review_id, candidate_external_id,
                      input_kind, expected_sha256, status, attempt_count,
                      created_by, details, created_at, updated_at
                    ) values (
                      %s, %s, %s,
                      'creator_upload', %s, 'queued', 0,
                      %s, %s, %s, %s
                    )
                    returning *
                    """,
                    (
                        job_id,
                        review_id,
                        f"creator-upload:{session_id}",
                        payload.expected_sha256,
                        actor,
                        Jsonb(
                            {
                                "expected_size_bytes": payload.expected_size_bytes,
                                "content_type": payload.content_type,
                            }
                        ),
                        now,
                        now,
                    ),
                ).fetchone()
                session_row = cursor.execute(
                    """
                    insert into creator_upload_sessions (
                      id, audio_input_job_id, rights_review_id,
                      expected_size_bytes, received_size_bytes,
                      content_type, expected_sha256, status,
                      expires_at, created_by, version,
                      created_at, updated_at
                    ) values (
                      %s, %s, %s,
                      %s, 0,
                      %s, %s, 'initiated',
                      %s, %s, 0,
                      %s, %s
                    )
                    returning *
                    """,
                    (
                        session_id,
                        job_id,
                        review_id,
                        payload.expected_size_bytes,
                        payload.content_type,
                        payload.expected_sha256,
                        now + CREATOR_UPLOAD_SESSION_TTL,
                        actor,
                        now,
                        now,
                    ),
                ).fetchone()
        return _job_from_row(job_row), _session_from_row(session_row)

    def get_creator_upload(
        self,
        session_id: UUID,
    ) -> CreatorUploadSession | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from creator_upload_sessions where id = %s",
                    (session_id,),
                ).fetchone()
        return None if row is None else _session_from_row(row)

    def begin_creator_upload(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        staging_object_key: str,
        storage_upload_id: str,
    ) -> CreatorUploadSession | None:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current_row = cursor.execute(
                    """
                    select * from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if current_row is None:
                    return None
                current = _session_from_row(current_row)
                if (
                    current.version != expected_version
                    or current.status is not CreatorUploadStatus.initiated
                ):
                    return None
                if current.expires_at <= now:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload session has expired"
                    )
                job = cursor.execute(
                    """
                    select * from audio_input_jobs
                    where id = %s
                    for update
                    """,
                    (current.audio_input_job_id,),
                ).fetchone()
                if job is None or job["status"] != "queued":
                    return None
                cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'processing',
                        attempt_count = attempt_count + 1,
                        claim_started_at = %s,
                        started_at = %s,
                        updated_at = %s
                    where id = %s
                    """,
                    (now, now, now, current.audio_input_job_id),
                )
                row = cursor.execute(
                    """
                    update creator_upload_sessions
                    set status = 'uploading',
                        staging_object_key = %s,
                        storage_upload_id = %s,
                        version = version + 1,
                        updated_at = %s
                    where id = %s and version = %s
                    returning *
                    """,
                    (
                        staging_object_key,
                        storage_upload_id,
                        now,
                        session_id,
                        expected_version,
                    ),
                ).fetchone()
                return None if row is None else _session_from_row(row)

    def record_creator_upload_progress(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        received_size_bytes: int,
    ) -> CreatorUploadSession | None:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current_row = cursor.execute(
                    """
                    select * from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if current_row is None:
                    return None
                current = _session_from_row(current_row)
                if (
                    current.version != expected_version
                    or current.status is not CreatorUploadStatus.uploading
                ):
                    return None
                if received_size_bytes < current.received_size_bytes:
                    raise ValueError("creator upload progress cannot move backwards")
                if received_size_bytes > current.expected_size_bytes:
                    raise ValueError(
                        "creator upload progress exceeds the declared size"
                    )
                status = (
                    "awaiting_attestation"
                    if received_size_bytes == current.expected_size_bytes
                    else "uploading"
                )
                row = cursor.execute(
                    """
                    update creator_upload_sessions
                    set received_size_bytes = %s,
                        status = %s,
                        version = version + 1,
                        updated_at = %s
                    where id = %s and version = %s
                    returning *
                    """,
                    (
                        received_size_bytes,
                        status,
                        now,
                        session_id,
                        expected_version,
                    ),
                ).fetchone()
                return None if row is None else _session_from_row(row)

    def complete_creator_upload(
        self,
        session_id: UUID,
        *,
        attestation: CreatorUploadAttestation,
        actor: str,
        stored: StoredAudioObject,
    ) -> tuple[CreatorUploadSession, AudioInputJob, AudioAssetRecord] | None:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current_row = cursor.execute(
                    """
                    select * from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if current_row is None:
                    return None
                current = _session_from_row(current_row)
                if (
                    current.version != attestation.expected_version
                    or current.status
                    is not CreatorUploadStatus.awaiting_attestation
                ):
                    return None
                review = self._select_review(
                    cursor,
                    current.rights_review_id,
                    for_update=True,
                )
                if review is None:
                    raise CreatorUploadPersistenceDenied("rights review was not found")
                _validate_review(review, now=now)
                _validate_stored_upload(current, stored)
                job_row = cursor.execute(
                    """
                    select * from audio_input_jobs
                    where id = %s
                    for update
                    """,
                    (current.audio_input_job_id,),
                ).fetchone()
                if job_row is None or job_row["status"] != "processing":
                    return None

                evidence_row = cursor.execute(
                    """
                    insert into rights_evidence (
                      rights_review_id, evidence_type, reference_url,
                      assertions, submitted_by, created_at
                    ) values (
                      %s, 'creator_attestation', %s,
                      %s, %s, %s
                    )
                    returning *
                    """,
                    (
                        current.rights_review_id,
                        attestation.reference_url,
                        Jsonb(attestation.assertions),
                        actor,
                        now,
                    ),
                ).fetchone()
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
                        current.rights_review_id,
                        stored.key,
                        stored.sha256,
                        stored.size,
                        stored.content_type,
                        now + QUARANTINE_RETENTION,
                        now,
                        now,
                    ),
                ).fetchone()
                completed_job_row = cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'completed',
                        audio_asset_id = %s,
                        finished_at = %s,
                        updated_at = %s
                    where id = %s and status = 'processing'
                    returning *
                    """,
                    (
                        asset_row["id"],
                        now,
                        now,
                        current.audio_input_job_id,
                    ),
                ).fetchone()
                if completed_job_row is None:
                    raise CreatorUploadPersistenceError(
                        "creator upload job changed during completion"
                    )
                completed_session_row = cursor.execute(
                    """
                    update creator_upload_sessions
                    set status = 'completed',
                        attestation_evidence_id = %s,
                        attested_by = %s,
                        attested_at = %s,
                        version = version + 1,
                        updated_at = %s
                    where id = %s and version = %s
                    returning *
                    """,
                    (
                        evidence_row["id"],
                        actor,
                        now,
                        now,
                        session_id,
                        attestation.expected_version,
                    ),
                ).fetchone()
                if completed_session_row is None:
                    raise CreatorUploadPersistenceError(
                        "creator upload session changed during completion"
                    )
                return (
                    _session_from_row(completed_session_row),
                    _job_from_row(completed_job_row),
                    _asset_from_row(asset_row),
                )
