from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import Any, Callable
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.schemas.creator_upload import CreatorUploadStatus
from app.schemas.creator_upload_cleanup import (
    CreatorUploadCleanupJob,
    CreatorUploadCleanupOutcome,
    CreatorUploadCleanupReason,
    CreatorUploadCleanupStatus,
    CreatorUploadCleanupTombstone,
)


Clock = Callable[[], datetime]


class CreatorUploadCleanupError(RuntimeError):
    pass


class CreatorUploadCleanupConflict(CreatorUploadCleanupError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _job_from_row(row: dict[str, Any]) -> CreatorUploadCleanupJob:
    return CreatorUploadCleanupJob.model_validate(row)


def _tombstone_from_row(row: dict[str, Any]) -> CreatorUploadCleanupTombstone:
    return CreatorUploadCleanupTombstone.model_validate(row)


def _terminal_status_for(reason: CreatorUploadCleanupReason) -> CreatorUploadStatus:
    if reason is CreatorUploadCleanupReason.expired:
        return CreatorUploadStatus.expired
    return CreatorUploadStatus.aborted


class InMemoryCreatorUploadCleanupRepository:
    def __init__(
        self,
        creator_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._creator_repository = creator_repository
        self._clock = clock
        self._lock = RLock()
        self.jobs: dict[UUID, CreatorUploadCleanupJob] = {}
        self.job_by_session: dict[UUID, UUID] = {}
        self.tombstones: list[CreatorUploadCleanupTombstone] = []

    def enqueue_cleanup(
        self,
        session_id: UUID,
        *,
        reason: CreatorUploadCleanupReason,
        requested_by: str,
    ) -> CreatorUploadCleanupJob:
        with self._lock:
            session = self._creator_repository.get_creator_upload(session_id)
            if session is None:
                raise CreatorUploadCleanupConflict("creator upload session was not found")
            expected_status = _terminal_status_for(reason)
            if session.status is not expected_status:
                raise CreatorUploadCleanupConflict(
                    "cleanup reason does not match the terminal upload state"
                )
            existing_id = self.job_by_session.get(session_id)
            if existing_id is not None:
                existing = self.jobs[existing_id]
                if existing.reason is not reason:
                    raise CreatorUploadCleanupConflict(
                        "cleanup job already exists with another reason"
                    )
                return existing
            now = self._clock()
            job = CreatorUploadCleanupJob(
                session_id=session_id,
                reason=reason,
                object_key=session.staging_object_key,
                storage_upload_id=session.storage_upload_id,
                requested_by=requested_by,
                created_at=now,
                updated_at=now,
            )
            self.jobs[job.id] = job
            self.job_by_session[session_id] = job.id
            return job

    def get_cleanup_job(self, job_id: UUID) -> CreatorUploadCleanupJob | None:
        return self.jobs.get(job_id)

    def claim_due(
        self,
        *,
        limit: int,
        now: datetime | None = None,
        stale_before: datetime | None = None,
    ) -> list[CreatorUploadCleanupJob]:
        if limit < 1:
            return []
        current = now or self._clock()
        with self._lock:
            candidates = sorted(self.jobs.values(), key=lambda job: (job.created_at, job.id))
            claimed: list[CreatorUploadCleanupJob] = []
            for job in candidates:
                due = job.status is CreatorUploadCleanupStatus.queued
                due = due or (
                    job.status is CreatorUploadCleanupStatus.retry
                    and job.next_retry_at is not None
                    and job.next_retry_at <= current
                )
                due = due or (
                    job.status is CreatorUploadCleanupStatus.processing
                    and stale_before is not None
                    and job.claim_started_at is not None
                    and job.claim_started_at <= stale_before
                )
                if not due:
                    continue
                updated = CreatorUploadCleanupJob.model_validate(
                    {
                        **job.model_dump(),
                        "status": CreatorUploadCleanupStatus.processing,
                        "attempt_count": job.attempt_count + 1,
                        "claim_started_at": current,
                        "next_retry_at": None,
                        "updated_at": current,
                    }
                )
                self.jobs[job.id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
            return claimed

    def record_attempt(
        self,
        job_id: UUID,
        *,
        outcome: CreatorUploadCleanupOutcome,
        multipart_aborted: bool,
        object_deleted: bool,
        ledger_deleted: bool,
        error_code: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> tuple[CreatorUploadCleanupJob, CreatorUploadCleanupTombstone]:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.status is not CreatorUploadCleanupStatus.processing:
                raise CreatorUploadCleanupConflict("cleanup job is not claimed")
            now = self._clock()
            if outcome is CreatorUploadCleanupOutcome.retry:
                if next_retry_at is None or error_code is None:
                    raise CreatorUploadCleanupConflict("retry requires time and error code")
                status = CreatorUploadCleanupStatus.retry
                completed_at = None
            elif outcome is CreatorUploadCleanupOutcome.completed:
                status = CreatorUploadCleanupStatus.completed
                completed_at = now
                next_retry_at = None
                error_code = None
            else:
                if error_code is None:
                    raise CreatorUploadCleanupConflict("dead letter requires error code")
                status = CreatorUploadCleanupStatus.dead_letter
                completed_at = now
                next_retry_at = None
            updated = CreatorUploadCleanupJob.model_validate(
                {
                    **job.model_dump(),
                    "status": status,
                    "claim_started_at": None,
                    "next_retry_at": next_retry_at,
                    "last_error_code": error_code,
                    "completed_at": completed_at,
                    "updated_at": now,
                }
            )
            tombstone = CreatorUploadCleanupTombstone(
                cleanup_job_id=job.id,
                session_id=job.session_id,
                reason=job.reason,
                outcome=outcome,
                attempt_number=job.attempt_count,
                multipart_aborted=multipart_aborted,
                object_deleted=object_deleted,
                ledger_deleted=ledger_deleted,
                error_code=error_code,
                created_at=now,
            )
            self.jobs[job.id] = updated
            self.tombstones.append(tombstone)
            return updated, tombstone


class PostgresCreatorUploadCleanupRepository:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._clock = clock

    def enqueue_cleanup(
        self,
        session_id: UUID,
        *,
        reason: CreatorUploadCleanupReason,
        requested_by: str,
    ) -> CreatorUploadCleanupJob:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                session = cursor.execute(
                    """
                    select id, status, staging_object_key, storage_upload_id
                    from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise CreatorUploadCleanupConflict(
                        "creator upload session was not found"
                    )
                if session["status"] != _terminal_status_for(reason).value:
                    raise CreatorUploadCleanupConflict(
                        "cleanup reason does not match the terminal upload state"
                    )
                row = cursor.execute(
                    """
                    insert into creator_upload_cleanup_jobs (
                      session_id, reason, object_key, storage_upload_id,
                      requested_by
                    ) values (%s, %s, %s, %s, %s)
                    on conflict (session_id) do nothing
                    returning *
                    """,
                    (
                        session_id,
                        reason.value,
                        session["staging_object_key"],
                        session["storage_upload_id"],
                        requested_by,
                    ),
                ).fetchone()
                if row is None:
                    row = cursor.execute(
                        """
                        select * from creator_upload_cleanup_jobs
                        where session_id = %s
                        """,
                        (session_id,),
                    ).fetchone()
                    if row is None or row["reason"] != reason.value:
                        raise CreatorUploadCleanupConflict(
                            "cleanup job already exists with another reason"
                        )
        return _job_from_row(row)

    def get_cleanup_job(self, job_id: UUID) -> CreatorUploadCleanupJob | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from creator_upload_cleanup_jobs where id = %s",
                    (job_id,),
                ).fetchone()
        return None if row is None else _job_from_row(row)

    def claim_due(
        self,
        *,
        limit: int,
        now: datetime | None = None,
        stale_before: datetime | None = None,
    ) -> list[CreatorUploadCleanupJob]:
        if limit < 1:
            return []
        current = now or self._clock()
        stale = stale_before or current
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    """
                    with due as (
                      select id
                      from creator_upload_cleanup_jobs
                      where status = 'queued'
                         or (status = 'retry' and next_retry_at <= %s)
                         or (status = 'processing' and claim_started_at <= %s)
                      order by coalesce(next_retry_at, created_at), id
                      for update skip locked
                      limit %s
                    )
                    update creator_upload_cleanup_jobs as jobs
                    set status = 'processing',
                        attempt_count = jobs.attempt_count + 1,
                        claim_started_at = %s,
                        next_retry_at = null,
                        updated_at = %s
                    from due
                    where jobs.id = due.id
                    returning jobs.*
                    """,
                    (current, stale, limit, current, current),
                ).fetchall()
        return [_job_from_row(row) for row in rows]

    def record_attempt(
        self,
        job_id: UUID,
        *,
        outcome: CreatorUploadCleanupOutcome,
        multipart_aborted: bool,
        object_deleted: bool,
        ledger_deleted: bool,
        error_code: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> tuple[CreatorUploadCleanupJob, CreatorUploadCleanupTombstone]:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                job = cursor.execute(
                    """
                    select * from creator_upload_cleanup_jobs
                    where id = %s
                    for update
                    """,
                    (job_id,),
                ).fetchone()
                if job is None or job["status"] != "processing":
                    raise CreatorUploadCleanupConflict("cleanup job is not claimed")
                if outcome is CreatorUploadCleanupOutcome.retry:
                    if next_retry_at is None or error_code is None:
                        raise CreatorUploadCleanupConflict(
                            "retry requires time and error code"
                        )
                    status = "retry"
                    completed_at = None
                elif outcome is CreatorUploadCleanupOutcome.completed:
                    status = "completed"
                    completed_at = now
                    next_retry_at = None
                    error_code = None
                else:
                    if error_code is None:
                        raise CreatorUploadCleanupConflict(
                            "dead letter requires error code"
                        )
                    status = "dead_letter"
                    completed_at = now
                    next_retry_at = None
                updated = cursor.execute(
                    """
                    update creator_upload_cleanup_jobs
                    set status = %s,
                        claim_started_at = null,
                        next_retry_at = %s,
                        last_error_code = %s,
                        completed_at = %s,
                        updated_at = %s
                    where id = %s
                    returning *
                    """,
                    (
                        status,
                        next_retry_at,
                        error_code,
                        completed_at,
                        now,
                        job_id,
                    ),
                ).fetchone()
                tombstone = cursor.execute(
                    """
                    insert into creator_upload_cleanup_tombstones (
                      cleanup_job_id, session_id, reason, outcome,
                      attempt_number, multipart_aborted, object_deleted,
                      ledger_deleted, error_code, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning *
                    """,
                    (
                        job_id,
                        job["session_id"],
                        job["reason"],
                        outcome.value,
                        job["attempt_count"],
                        multipart_aborted,
                        object_deleted,
                        ledger_deleted,
                        error_code,
                        now,
                    ),
                ).fetchone()
        return _job_from_row(updated), _tombstone_from_row(tombstone)
