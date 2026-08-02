from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import Any, Callable
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.repositories.creator_upload import CreatorUploadPersistenceDenied
from app.schemas.audio import AudioInputStatus
from app.schemas.creator_upload import CreatorUploadSession, CreatorUploadStatus
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)
from app.services.audio_storage import AUDIO_QUARANTINE_BUCKET, MinioAudioStorage
from app.services.creator_upload_storage import (
    MultipartUploadHandle,
    UploadedPart,
)


Clock = Callable[[], datetime]


class CreatorUploadMultipartConflict(RuntimeError):
    """Raised when a multipart replay or optimistic version conflicts."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _session_from_row(row: dict[str, Any]) -> CreatorUploadSession:
    return CreatorUploadSession.model_validate(row)


def _manifest_from_row(row: dict[str, Any]) -> CreatorUploadManifest:
    return CreatorUploadManifest.model_validate(row)


def _part_from_row(row: dict[str, Any]) -> CreatorUploadPartRecord:
    return CreatorUploadPartRecord.model_validate(row)


def _updated_session(
    session: CreatorUploadSession,
    **changes: Any,
) -> CreatorUploadSession:
    return CreatorUploadSession.model_validate(
        {**session.model_dump(), **changes}
    )


def _validate_handle(
    session: CreatorUploadSession,
    handle: MultipartUploadHandle,
) -> None:
    if handle.bucket != AUDIO_QUARANTINE_BUCKET:
        raise CreatorUploadPersistenceDenied(
            "multipart upload must remain in quarantine"
        )
    MinioAudioStorage._validate_key(handle.key)
    if not handle.upload_id:
        raise CreatorUploadPersistenceDenied("multipart upload id is missing")
    if handle.expected_size_bytes != session.expected_size_bytes:
        raise CreatorUploadPersistenceDenied(
            "multipart upload size does not match the session"
        )
    if handle.content_type != session.content_type:
        raise CreatorUploadPersistenceDenied(
            "multipart upload content type does not match the session"
        )
    if handle.expected_sha256 != session.expected_sha256:
        raise CreatorUploadPersistenceDenied(
            "multipart upload checksum does not match the session"
        )
    if handle.part_count < 1 or handle.part_count > 10_000:
        raise CreatorUploadPersistenceDenied(
            "multipart upload part count is outside bounds"
        )


def _validate_manifest_replay(
    session: CreatorUploadSession,
    manifest: CreatorUploadManifest,
    handle: MultipartUploadHandle,
) -> None:
    if (
        manifest.part_size_bytes != handle.part_size_bytes
        or manifest.expected_part_count != handle.part_count
        or session.staging_object_key != handle.key
        or session.storage_upload_id != handle.upload_id
    ):
        raise CreatorUploadMultipartConflict(
            "multipart manifest conflicts with stored state"
        )


def _expected_part_size(
    session: CreatorUploadSession,
    manifest: CreatorUploadManifest,
    part_number: int,
) -> int:
    if part_number < 1 or part_number > manifest.expected_part_count:
        raise CreatorUploadMultipartConflict(
            "multipart part number is outside the upload plan"
        )
    consumed = (part_number - 1) * manifest.part_size_bytes
    return min(
        manifest.part_size_bytes,
        session.expected_size_bytes - consumed,
    )


def _validate_part(
    session: CreatorUploadSession,
    manifest: CreatorUploadManifest,
    part: UploadedPart,
) -> None:
    expected_size = _expected_part_size(session, manifest, part.part_number)
    if part.size_bytes != expected_size:
        raise CreatorUploadMultipartConflict(
            "multipart part size does not match the upload plan"
        )
    CreatorUploadPartRecord(
        session_id=session.id,
        part_number=part.part_number,
        etag=part.etag,
        size_bytes=part.size_bytes,
        checksum_sha256=part.checksum_sha256,
    )


def _same_part(record: CreatorUploadPartRecord, part: UploadedPart) -> bool:
    return (
        record.part_number == part.part_number
        and record.etag == part.etag
        and record.size_bytes == part.size_bytes
        and record.checksum_sha256 == part.checksum_sha256
    )


class InMemoryCreatorUploadMultipartRepository:
    def __init__(
        self,
        creator_repository: Any,
        audio_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._creator_repository = creator_repository
        self._audio_repository = audio_repository
        self._clock = clock
        self._lock = RLock()
        self.manifests: dict[UUID, CreatorUploadManifest] = {}
        self.parts: dict[tuple[UUID, int], CreatorUploadPartRecord] = {}

    def attach_manifest(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        handle: MultipartUploadHandle,
    ) -> tuple[CreatorUploadSession, CreatorUploadManifest]:
        with self._lock:
            session = self._creator_repository.get_creator_upload(session_id)
            if session is None:
                raise CreatorUploadPersistenceDenied(
                    "creator upload session was not found"
                )
            _validate_handle(session, handle)
            existing = self.manifests.get(session_id)
            if existing is not None:
                _validate_manifest_replay(session, existing, handle)
                return session, existing
            if session.version != expected_version:
                raise CreatorUploadMultipartConflict(
                    "creator upload version conflict"
                )
            if session.status is not CreatorUploadStatus.initiated:
                raise CreatorUploadMultipartConflict(
                    "creator upload is not initiated"
                )
            now = self._clock()
            if session.expires_at <= now:
                raise CreatorUploadPersistenceDenied(
                    "creator upload session has expired"
                )
            job = self._audio_repository.jobs.get(session.audio_input_job_id)
            if job is None or job.status is not AudioInputStatus.queued:
                raise CreatorUploadMultipartConflict(
                    "creator upload job is not queued"
                )

            manifest = CreatorUploadManifest(
                session_id=session_id,
                part_size_bytes=handle.part_size_bytes,
                expected_part_count=handle.part_count,
                created_at=now,
            )
            claimed_job = job.model_copy(
                update={
                    "status": AudioInputStatus.processing,
                    "attempt_count": job.attempt_count + 1,
                    "claim_started_at": now,
                    "started_at": job.started_at or now,
                    "updated_at": now,
                }
            )
            updated_session = _updated_session(
                session,
                status=CreatorUploadStatus.uploading,
                staging_object_key=handle.key,
                storage_upload_id=handle.upload_id,
                version=session.version + 1,
                updated_at=now,
            )
            self.manifests[session_id] = manifest
            self._audio_repository.jobs[job.id] = claimed_job
            self._creator_repository.sessions[session_id] = updated_session
            return updated_session, manifest

    def record_part(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        part: UploadedPart,
    ) -> tuple[CreatorUploadSession, CreatorUploadPartRecord]:
        with self._lock:
            session = self._creator_repository.get_creator_upload(session_id)
            if session is None:
                raise CreatorUploadPersistenceDenied(
                    "creator upload session was not found"
                )
            existing = self.parts.get((session_id, part.part_number))
            if existing is not None:
                if not _same_part(existing, part):
                    raise CreatorUploadMultipartConflict(
                        "multipart part replay conflicts with stored state"
                    )
                return session, existing
            if session.version != expected_version:
                raise CreatorUploadMultipartConflict(
                    "creator upload version conflict"
                )
            if session.status is not CreatorUploadStatus.uploading:
                raise CreatorUploadMultipartConflict(
                    "creator upload is not accepting multipart parts"
                )
            now = self._clock()
            if session.expires_at <= now:
                raise CreatorUploadPersistenceDenied(
                    "creator upload session has expired"
                )
            manifest = self.manifests.get(session_id)
            if manifest is None:
                raise CreatorUploadPersistenceDenied(
                    "creator upload manifest was not found"
                )
            _validate_part(session, manifest, part)

            record = CreatorUploadPartRecord(
                session_id=session_id,
                part_number=part.part_number,
                etag=part.etag,
                size_bytes=part.size_bytes,
                checksum_sha256=part.checksum_sha256,
                created_at=now,
            )
            next_parts = [*self.list_parts(session_id), record]
            received_size = sum(item.size_bytes for item in next_parts)
            if received_size > session.expected_size_bytes:
                raise CreatorUploadMultipartConflict(
                    "multipart ledger exceeds declared size"
                )
            complete = (
                len(next_parts) == manifest.expected_part_count
                and received_size == session.expected_size_bytes
            )
            if len(next_parts) == manifest.expected_part_count and not complete:
                raise CreatorUploadMultipartConflict(
                    "multipart ledger is complete but byte totals do not match"
                )
            updated_session = _updated_session(
                session,
                received_size_bytes=received_size,
                status=(
                    CreatorUploadStatus.awaiting_attestation
                    if complete
                    else CreatorUploadStatus.uploading
                ),
                version=session.version + 1,
                updated_at=now,
            )
            self.parts[(session_id, part.part_number)] = record
            self._creator_repository.sessions[session_id] = updated_session
            return updated_session, record

    def list_parts(self, session_id: UUID) -> list[CreatorUploadPartRecord]:
        return sorted(
            (
                record
                for (stored_session_id, _), record in self.parts.items()
                if stored_session_id == session_id
            ),
            key=lambda item: item.part_number,
        )


class PostgresCreatorUploadMultipartRepository:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._clock = clock

    def attach_manifest(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        handle: MultipartUploadHandle,
    ) -> tuple[CreatorUploadSession, CreatorUploadManifest]:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                session_row = cursor.execute(
                    """
                    select * from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if session_row is None:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload session was not found"
                    )
                session = _session_from_row(session_row)
                _validate_handle(session, handle)
                manifest_row = cursor.execute(
                    """
                    select * from creator_upload_manifests
                    where session_id = %s
                    """,
                    (session_id,),
                ).fetchone()
                if manifest_row is not None:
                    manifest = _manifest_from_row(manifest_row)
                    _validate_manifest_replay(session, manifest, handle)
                    return session, manifest
                if session.version != expected_version:
                    raise CreatorUploadMultipartConflict(
                        "creator upload version conflict"
                    )
                if session.status is not CreatorUploadStatus.initiated:
                    raise CreatorUploadMultipartConflict(
                        "creator upload is not initiated"
                    )
                if session.expires_at <= now:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload session has expired"
                    )
                job = cursor.execute(
                    """
                    select * from audio_input_jobs
                    where id = %s
                    for update
                    """,
                    (session.audio_input_job_id,),
                ).fetchone()
                if job is None or job["status"] != AudioInputStatus.queued.value:
                    raise CreatorUploadMultipartConflict(
                        "creator upload job is not queued"
                    )

                manifest_row = cursor.execute(
                    """
                    insert into creator_upload_manifests (
                      session_id, part_size_bytes,
                      expected_part_count, created_at
                    ) values (%s, %s, %s, %s)
                    returning *
                    """,
                    (
                        session_id,
                        handle.part_size_bytes,
                        handle.part_count,
                        now,
                    ),
                ).fetchone()
                job_update = cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'processing',
                        attempt_count = attempt_count + 1,
                        claim_started_at = %s,
                        started_at = coalesce(started_at, %s),
                        updated_at = %s
                    where id = %s and status = 'queued'
                    returning id
                    """,
                    (now, now, now, session.audio_input_job_id),
                ).fetchone()
                if job_update is None:
                    raise CreatorUploadMultipartConflict(
                        "creator upload job changed"
                    )
                session_row = cursor.execute(
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
                        handle.key,
                        handle.upload_id,
                        now,
                        session_id,
                        expected_version,
                    ),
                ).fetchone()
                if session_row is None:
                    raise CreatorUploadMultipartConflict(
                        "creator upload version conflict"
                    )
                return (
                    _session_from_row(session_row),
                    _manifest_from_row(manifest_row),
                )

    def record_part(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        part: UploadedPart,
    ) -> tuple[CreatorUploadSession, CreatorUploadPartRecord]:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                session_row = cursor.execute(
                    """
                    select * from creator_upload_sessions
                    where id = %s
                    for update
                    """,
                    (session_id,),
                ).fetchone()
                if session_row is None:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload session was not found"
                    )
                session = _session_from_row(session_row)
                existing_row = cursor.execute(
                    """
                    select * from creator_upload_parts
                    where session_id = %s and part_number = %s
                    """,
                    (session_id, part.part_number),
                ).fetchone()
                if existing_row is not None:
                    existing = _part_from_row(existing_row)
                    if not _same_part(existing, part):
                        raise CreatorUploadMultipartConflict(
                            "multipart part replay conflicts with stored state"
                        )
                    return session, existing
                if session.version != expected_version:
                    raise CreatorUploadMultipartConflict(
                        "creator upload version conflict"
                    )
                if session.status is not CreatorUploadStatus.uploading:
                    raise CreatorUploadMultipartConflict(
                        "creator upload is not accepting multipart parts"
                    )
                if session.expires_at <= now:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload session has expired"
                    )
                manifest_row = cursor.execute(
                    """
                    select * from creator_upload_manifests
                    where session_id = %s
                    """,
                    (session_id,),
                ).fetchone()
                if manifest_row is None:
                    raise CreatorUploadPersistenceDenied(
                        "creator upload manifest was not found"
                    )
                manifest = _manifest_from_row(manifest_row)
                _validate_part(session, manifest, part)

                part_row = cursor.execute(
                    """
                    insert into creator_upload_parts (
                      session_id, part_number, etag,
                      size_bytes, checksum_sha256, created_at
                    ) values (%s, %s, %s, %s, %s, %s)
                    returning *
                    """,
                    (
                        session_id,
                        part.part_number,
                        part.etag,
                        part.size_bytes,
                        part.checksum_sha256,
                        now,
                    ),
                ).fetchone()
                totals = cursor.execute(
                    """
                    select count(*)::integer as part_count,
                           coalesce(sum(size_bytes), 0)::bigint as received_size
                    from creator_upload_parts
                    where session_id = %s
                    """,
                    (session_id,),
                ).fetchone()
                received_size = totals["received_size"]
                if received_size > session.expected_size_bytes:
                    raise CreatorUploadMultipartConflict(
                        "multipart ledger exceeds declared size"
                    )
                complete = (
                    totals["part_count"] == manifest.expected_part_count
                    and received_size == session.expected_size_bytes
                )
                if (
                    totals["part_count"] == manifest.expected_part_count
                    and not complete
                ):
                    raise CreatorUploadMultipartConflict(
                        "multipart ledger is complete but byte totals do not match"
                    )
                session_row = cursor.execute(
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
                        received_size,
                        (
                            CreatorUploadStatus.awaiting_attestation.value
                            if complete
                            else CreatorUploadStatus.uploading.value
                        ),
                        now,
                        session_id,
                        expected_version,
                    ),
                ).fetchone()
                if session_row is None:
                    raise CreatorUploadMultipartConflict(
                        "creator upload version conflict"
                    )
                return _session_from_row(session_row), _part_from_row(part_row)

    def list_parts(self, session_id: UUID) -> list[CreatorUploadPartRecord]:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    """
                    select * from creator_upload_parts
                    where session_id = %s
                    order by part_number
                    """,
                    (session_id,),
                ).fetchall()
        return [_part_from_row(row) for row in rows]
