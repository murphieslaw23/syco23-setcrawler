from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.repositories.creator_upload import CreatorUploadPersistenceDenied
from app.schemas.creator_upload import (
    CreatorUploadAttestation,
    CreatorUploadSession,
    CreatorUploadStatus,
)
from app.services.creator_upload_storage import (
    MultipartUploadHandle,
    UploadedPart,
)


class CreatorUploadFinalizationError(RuntimeError):
    pass


class CreatorUploadFinalizationConflict(CreatorUploadFinalizationError):
    pass


class CreatorUploadCommitStateUnknown(CreatorUploadFinalizationError):
    """The database commit state could not be re-read safely."""


class CreatorUploadFinalizationCompensationError(CreatorUploadFinalizationError):
    def __init__(
        self,
        primary_error: Exception,
        compensation_errors: list[Exception],
    ) -> None:
        super().__init__(
            "creator upload finalization failed and cleanup was incomplete"
        )
        self.primary_error = primary_error
        self.compensation_errors = tuple(compensation_errors)


@dataclass(frozen=True, slots=True)
class CreatorUploadCompletionReceipt:
    """Client-safe completion result without private asset/storage identity."""

    session_id: UUID
    status: CreatorUploadStatus
    version: int
    attested_by: str
    attested_at: datetime

    @classmethod
    def from_session(
        cls,
        session: CreatorUploadSession,
    ) -> "CreatorUploadCompletionReceipt":
        if (
            session.status is not CreatorUploadStatus.completed
            or session.attested_by is None
            or session.attested_at is None
        ):
            raise CreatorUploadFinalizationConflict(
                "creator upload session is not completed"
            )
        return cls(
            session_id=session.id,
            status=session.status,
            version=session.version,
            attested_by=session.attested_by,
            attested_at=session.attested_at,
        )


class CreatorUploadFinalizer:
    """Complete multipart bytes and atomically persist attestation plus asset.

    Storage and PostgreSQL cannot share one distributed transaction. A verified
    private object is retained after a transient persistence failure so the next
    call can resume through the storage adapter's ``NoSuchUpload`` verification
    path. Rights denial is terminal and requires a durable session/job abort
    before object deletion. Unknown database commit state never deletes bytes
    that may already be referenced.
    """

    def __init__(
        self,
        repository: Any,
        multipart_storage: Any,
        object_storage: Any,
    ) -> None:
        self._repository = repository
        self._multipart_storage = multipart_storage
        self._object_storage = object_storage

    def finalize(
        self,
        session_id: UUID,
        *,
        attestation: CreatorUploadAttestation,
        actor: str,
    ) -> CreatorUploadCompletionReceipt:
        session = self._require_session(session_id)
        if session.status is CreatorUploadStatus.completed:
            return self._completed_replay(session, attestation, actor)
        if session.status is not CreatorUploadStatus.awaiting_attestation:
            raise CreatorUploadFinalizationConflict(
                "creator upload is not awaiting attestation"
            )
        if session.version != attestation.expected_version:
            raise CreatorUploadFinalizationConflict(
                "creator upload version conflict"
            )

        manifest = self._repository.get_manifest(session_id)
        if manifest is None:
            raise CreatorUploadFinalizationConflict(
                "creator upload multipart manifest was not found"
            )
        records = sorted(
            self._repository.list_parts(session_id),
            key=lambda record: record.part_number,
        )
        expected_numbers = list(range(1, manifest.expected_part_count + 1))
        if [record.part_number for record in records] != expected_numbers:
            raise CreatorUploadFinalizationConflict(
                "creator upload multipart ledger requires every planned part"
            )
        if sum(record.size_bytes for record in records) != session.expected_size_bytes:
            raise CreatorUploadFinalizationConflict(
                "creator upload multipart ledger byte total is inconsistent"
            )

        handle = self._handle(session, manifest.part_size_bytes)
        parts = [
            UploadedPart(
                part_number=record.part_number,
                etag=record.etag,
                size_bytes=record.size_bytes,
                checksum_sha256=record.checksum_sha256,
            )
            for record in records
        ]

        try:
            stored = self._multipart_storage.complete(handle, parts)
        except Exception as primary_error:
            self._abort_incomplete_finalization(
                session_id,
                handle,
                primary_error,
            )
            raise

        try:
            result = self._repository.complete_creator_upload(
                session_id,
                attestation=attestation,
                actor=actor,
                stored=stored,
            )
        except CreatorUploadPersistenceDenied as primary_error:
            self._cleanup_denied_finalization(
                session_id,
                stored,
                primary_error,
            )
            raise
        except Exception as primary_error:
            return self._resolve_transient_persistence_failure(
                session_id,
                stored,
                primary_error,
            )
        if result is None:
            return self._resolve_transient_persistence_failure(
                session_id,
                stored,
                CreatorUploadFinalizationConflict(
                    "creator upload changed during finalization"
                ),
            )
        completed_session = result[0]
        return CreatorUploadCompletionReceipt.from_session(completed_session)

    def _resolve_transient_persistence_failure(
        self,
        session_id: UUID,
        stored: Any,
        primary_error: Exception,
    ) -> CreatorUploadCompletionReceipt:
        try:
            current = self._repository.get_creator_upload(session_id)
        except Exception as status_error:
            raise CreatorUploadCommitStateUnknown(
                "creator upload commit state could not be verified"
            ) from ExceptionGroup(
                "persistence failure and status verification failure",
                [primary_error, status_error],
            )
        if current is not None and current.status is CreatorUploadStatus.completed:
            return CreatorUploadCompletionReceipt.from_session(current)
        if current is not None and current.status in {
            CreatorUploadStatus.aborted,
            CreatorUploadStatus.expired,
        }:
            self._delete_stored_object(stored)
        # Awaiting-attestation remains retryable. The verified object stays
        # private and the next storage completion re-validates it after
        # ``NoSuchUpload`` instead of requiring another upload.
        raise primary_error

    def _cleanup_denied_finalization(
        self,
        session_id: UUID,
        stored: Any,
        primary_error: Exception,
    ) -> None:
        try:
            self._repository.abort_creator_upload(
                session_id,
                reason="creator upload rights review denied or expired",
            )
        except Exception as abort_error:
            # Keep the verified private object when the durable state transition
            # failed. A later cleanup pass can retry from the still-recoverable
            # session instead of facing an open database record with no bytes.
            raise CreatorUploadFinalizationCompensationError(
                primary_error,
                [abort_error],
            ) from primary_error

        try:
            self._delete_stored_object(stored)
        except Exception as delete_error:
            raise CreatorUploadFinalizationCompensationError(
                primary_error,
                [delete_error],
            ) from primary_error

    def _delete_stored_object(self, stored: Any) -> None:
        try:
            self._object_storage.delete(stored.bucket, stored.key)
        except Exception as error:
            if getattr(error, "code", None) not in {
                "NoSuchKey",
                "NoSuchObject",
            }:
                raise

    def _abort_incomplete_finalization(
        self,
        session_id: UUID,
        handle: MultipartUploadHandle,
        primary_error: Exception,
    ) -> None:
        compensation_errors: list[Exception] = []
        try:
            self._repository.abort_creator_upload(
                session_id,
                reason="multipart completion failed verification",
            )
        except Exception as cleanup_error:
            compensation_errors.append(cleanup_error)
        try:
            self._multipart_storage.abort(handle)
        except Exception as cleanup_error:
            compensation_errors.append(cleanup_error)
        if compensation_errors:
            raise CreatorUploadFinalizationCompensationError(
                primary_error,
                compensation_errors,
            ) from primary_error

    def _require_session(self, session_id: UUID) -> CreatorUploadSession:
        session = self._repository.get_creator_upload(session_id)
        if session is None:
            raise CreatorUploadFinalizationConflict(
                "creator upload session was not found"
            )
        return session

    @staticmethod
    def _handle(
        session: CreatorUploadSession,
        part_size_bytes: int,
    ) -> MultipartUploadHandle:
        if session.staging_object_key is None or session.storage_upload_id is None:
            raise CreatorUploadFinalizationConflict(
                "creator upload private storage state is incomplete"
            )
        return MultipartUploadHandle(
            bucket="audio-quarantine",
            key=session.staging_object_key,
            upload_id=session.storage_upload_id,
            expected_size_bytes=session.expected_size_bytes,
            content_type=session.content_type,
            expected_sha256=session.expected_sha256,
            part_size_bytes=part_size_bytes,
        )

    @staticmethod
    def _completed_replay(
        session: CreatorUploadSession,
        attestation: CreatorUploadAttestation,
        actor: str,
    ) -> CreatorUploadCompletionReceipt:
        if (
            session.attested_by != actor
            or attestation.expected_version
            not in {session.version, session.version - 1}
        ):
            raise CreatorUploadFinalizationConflict(
                "completed creator upload replay does not match the original actor/version"
            )
        return CreatorUploadCompletionReceipt.from_session(session)
