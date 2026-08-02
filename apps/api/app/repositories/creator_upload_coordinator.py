from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.repositories.creator_upload import CreatorUploadPersistenceDenied
from app.schemas.audio import AudioInputStatus
from app.schemas.creator_upload import CreatorUploadSession, CreatorUploadStatus
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)


Clock = Callable[[], datetime]
_ABORTABLE_UPLOAD_STATES = frozenset(
    {
        CreatorUploadStatus.initiated,
        CreatorUploadStatus.uploading,
        CreatorUploadStatus.awaiting_attestation,
    }
)
_ABORTABLE_JOB_STATES = frozenset(
    {
        AudioInputStatus.queued,
        AudioInputStatus.processing,
        AudioInputStatus.retry,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _updated_session(
    session: CreatorUploadSession,
    **changes: Any,
) -> CreatorUploadSession:
    return CreatorUploadSession.model_validate(
        {**session.model_dump(), **changes}
    )


class InMemoryCreatorUploadCoordinatorRepository:
    """Coordinator-only view over private creator and multipart repositories."""

    def __init__(
        self,
        creator_repository: Any,
        audio_repository: Any,
        multipart_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._creator = creator_repository
        self._audio = audio_repository
        self._multipart = multipart_repository
        self._clock = clock
        self._lock = RLock()

    def get_creator_upload(
        self,
        session_id: UUID,
    ) -> CreatorUploadSession | None:
        return self._creator.get_creator_upload(session_id)

    def get_manifest(
        self,
        session_id: UUID,
    ) -> CreatorUploadManifest | None:
        return self._multipart.manifests.get(session_id)

    def get_part(
        self,
        session_id: UUID,
        part_number: int,
    ) -> CreatorUploadPartRecord | None:
        return self._multipart.parts.get((session_id, part_number))

    def attach_manifest(self, *args: Any, **kwargs: Any) -> Any:
        return self._multipart.attach_manifest(*args, **kwargs)

    def record_part(self, *args: Any, **kwargs: Any) -> Any:
        return self._multipart.record_part(*args, **kwargs)

    def abort_creator_upload(
        self,
        session_id: UUID,
        *,
        reason: str,
    ) -> CreatorUploadSession:
        if not reason.strip():
            raise ValueError("creator upload abort reason is required")
        with self._lock:
            session = self._creator.get_creator_upload(session_id)
            if session is None:
                raise CreatorUploadPersistenceDenied(
                    "creator upload session was not found"
                )
            if session.status is CreatorUploadStatus.aborted:
                return session
            if session.status not in _ABORTABLE_UPLOAD_STATES:
                raise CreatorUploadPersistenceDenied(
                    "creator upload cannot be aborted from its current state"
                )
            job = self._audio.jobs.get(session.audio_input_job_id)
            if job is None:
                raise CreatorUploadPersistenceDenied(
                    "creator upload audio job was not found"
                )
            if job.status not in _ABORTABLE_JOB_STATES:
                raise CreatorUploadPersistenceDenied(
                    "creator upload audio job cannot be aborted"
                )
            now = self._clock()
            updated_session = _updated_session(
                session,
                status=CreatorUploadStatus.aborted,
                version=session.version + 1,
                updated_at=now,
            )
            updated_job = job.model_copy(
                update={
                    "status": AudioInputStatus.blocked,
                    "finished_at": now,
                    "updated_at": now,
                    "details": {
                        **job.details,
                        "abort_reason": reason,
                        "abort_source": "creator_upload_coordinator",
                    },
                }
            )
            self._creator.sessions[session_id] = updated_session
            self._audio.jobs[job.id] = updated_job
            return updated_session


class PostgresCreatorUploadCoordinatorRepository:
    """PostgreSQL coordinator facade with atomic session/job compensation."""

    def __init__(
        self,
        pool: ConnectionPool,
        multipart_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._multipart = multipart_repository
        self._clock = clock

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
        return None if row is None else CreatorUploadSession.model_validate(row)

    def get_manifest(
        self,
        session_id: UUID,
    ) -> CreatorUploadManifest | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select * from creator_upload_manifests
                    where session_id = %s
                    """,
                    (session_id,),
                ).fetchone()
        return None if row is None else CreatorUploadManifest.model_validate(row)

    def get_part(
        self,
        session_id: UUID,
        part_number: int,
    ) -> CreatorUploadPartRecord | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select * from creator_upload_parts
                    where session_id = %s and part_number = %s
                    """,
                    (session_id, part_number),
                ).fetchone()
        return None if row is None else CreatorUploadPartRecord.model_validate(row)

    def attach_manifest(self, *args: Any, **kwargs: Any) -> Any:
        return self._multipart.attach_manifest(*args, **kwargs)

    def record_part(self, *args: Any, **kwargs: Any) -> Any:
        return self._multipart.record_part(*args, **kwargs)

    def abort_creator_upload(
        self,
        session_id: UUID,
        *,
        reason: str,
    ) -> CreatorUploadSession:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("creator upload abort reason is required")
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select * from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload session was not found"
                    )
                session = CreatorUploadSession.model_validate(row)
                if session.status is CreatorUploadStatus.aborted:
                    return session
                if session.status not in _ABORTABLE_UPLOAD_STATES:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload cannot be aborted from its current state"
                    )
                job = cursor.execute(
                    """
                    select * from audio_input_jobs
                    where id = %s
                    for update
                    """,
                    (session.audio_input_job_id,),
                ).fetchone()
                if job is None:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload audio job was not found"
                    )
                if AudioInputStatus(job["status"]) not in _ABORTABLE_JOB_STATES:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload audio job cannot be aborted"
                    )
                cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'blocked',
                        finished_at = %s,
                        updated_at = %s,
                        details = details || %s
                    where id = %s
                    """,
                    (
                        now,
                        now,
                        Jsonb(
                            {
                                "abort_reason": normalized_reason,
                                "abort_source": "creator_upload_coordinator",
                            }
                        ),
                        session.audio_input_job_id,
                    ),
                )
                updated = cursor.execute(
                    """
                    update creator_upload_sessions
                    set status = 'aborted',
                        version = version + 1,
                        updated_at = %s
                    where id = %s
                    returning *
                    """,
                    (now, session_id),
                ).fetchone()
        return CreatorUploadSession.model_validate(updated)
