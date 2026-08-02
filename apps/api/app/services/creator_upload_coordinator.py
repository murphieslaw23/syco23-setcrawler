from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.repositories.creator_upload_multipart import CreatorUploadMultipartConflict
from app.schemas.creator_upload import (
    CreatorUploadSession,
    CreatorUploadStart,
    CreatorUploadStatus,
)
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)
from app.services.creator_upload_storage import MultipartUploadHandle


_TERMINAL_UPLOAD_STATES = frozenset(
    {
        CreatorUploadStatus.completed,
        CreatorUploadStatus.aborted,
        CreatorUploadStatus.expired,
    }
)


class CreatorUploadCompensationError(RuntimeError):
    """A primary upload failure plus one or more failed cleanup actions."""

    def __init__(
        self,
        primary_error: Exception,
        compensation_errors: list[Exception],
    ) -> None:
        super().__init__(
            "creator upload failed and compensation was incomplete"
        )
        self.primary_error = primary_error
        self.compensation_errors = tuple(compensation_errors)

    @property
    def compensation_error(self) -> Exception | None:
        return self.compensation_errors[0] if self.compensation_errors else None


class CreatorUploadProgress(BaseModel):
    """Client-safe progress without MinIO object or multipart identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    status: CreatorUploadStatus
    expected_size_bytes: int
    received_size_bytes: int
    part_size_bytes: int
    expected_part_count: int
    version: int
    expires_at: datetime

    @classmethod
    def from_records(
        cls,
        session: CreatorUploadSession,
        manifest: CreatorUploadManifest,
    ) -> "CreatorUploadProgress":
        if session.id != manifest.session_id:
            raise CreatorUploadMultipartConflict(
                "multipart manifest conflicts with its upload session"
            )
        return cls(
            session_id=session.id,
            status=session.status,
            expected_size_bytes=session.expected_size_bytes,
            received_size_bytes=session.received_size_bytes,
            part_size_bytes=manifest.part_size_bytes,
            expected_part_count=manifest.expected_part_count,
            version=session.version,
            expires_at=session.expires_at,
        )


class CreatorUploadCoordinator:
    """Server-only creator-upload transport and ledger orchestration.

    The service never returns a bucket, object key, multipart upload ID, ETag,
    credential, or presigned URL. Runtime routes and workers remain disabled.
    """

    def __init__(
        self,
        creator_repository: Any,
        multipart_repository: Any,
        storage: Any,
    ) -> None:
        self._creator_repository = creator_repository
        self._multipart_repository = multipart_repository
        self._storage = storage

    def start_upload(
        self,
        review_id: UUID,
        *,
        payload: CreatorUploadStart,
        actor: str,
    ) -> CreatorUploadProgress:
        """Create and initialize a private upload without exposing storage state.

        This preserves the earlier service-level entrypoint while keeping all
        storage identity inside the coordinator/repository boundary.
        """
        create = getattr(
            self._creator_repository,
            "create_creator_upload",
            None,
        )
        if not callable(create):
            raise CreatorUploadMultipartConflict(
                "creator upload repository cannot create sessions"
            )
        _job, session = create(
            review_id,
            payload=payload,
            actor=actor,
        )
        try:
            return self.start(session.id, expected_version=session.version)
        except Exception as primary_error:
            compensation_errors: list[Exception] = []
            try:
                self._creator_repository.abort_creator_upload(
                    session.id,
                    reason="creator upload initialization failed",
                )
            except Exception as compensation_error:
                compensation_errors.append(compensation_error)

            if isinstance(primary_error, CreatorUploadCompensationError):
                compensation_errors = [
                    *primary_error.compensation_errors,
                    *compensation_errors,
                ]
                raise CreatorUploadCompensationError(
                    primary_error.primary_error,
                    compensation_errors,
                ) from primary_error
            if compensation_errors:
                raise CreatorUploadCompensationError(
                    primary_error,
                    compensation_errors,
                ) from primary_error
            raise

    def start(
        self,
        session_id: UUID,
        *,
        expected_version: int,
    ) -> CreatorUploadProgress:
        session = self._require_active_session(session_id)
        existing_manifest = self._multipart_repository.get_manifest(session_id)
        if existing_manifest is not None:
            if session.status is CreatorUploadStatus.initiated:
                raise CreatorUploadMultipartConflict(
                    "initiated creator upload cannot already have a manifest"
                )
            return CreatorUploadProgress.from_records(
                session,
                existing_manifest,
            )
        if session.status is not CreatorUploadStatus.initiated:
            raise CreatorUploadMultipartConflict(
                "creator upload manifest is missing for active session"
            )

        handle = self._storage.start(
            expected_size_bytes=session.expected_size_bytes,
            content_type=session.content_type,
            expected_sha256=session.expected_sha256,
        )
        try:
            updated_session, manifest = self._multipart_repository.attach_manifest(
                session_id,
                expected_version=expected_version,
                handle=handle,
            )
        except Exception as primary_error:
            try:
                self._storage.abort(handle)
            except Exception as compensation_error:
                raise CreatorUploadCompensationError(
                    primary_error,
                    [compensation_error],
                ) from primary_error
            raise
        return CreatorUploadProgress.from_records(updated_session, manifest)

    def upload_part(
        self,
        session_id: UUID,
        *,
        expected_version: int,
        part_number: int,
        data: bytes,
    ) -> CreatorUploadProgress:
        if not isinstance(data, bytes):
            raise TypeError("creator upload part data must be bytes")
        session = self._require_active_session(session_id)
        if session.status not in {
            CreatorUploadStatus.uploading,
            CreatorUploadStatus.awaiting_attestation,
        }:
            raise CreatorUploadMultipartConflict(
                "creator upload cannot accept parts from its current state"
            )
        manifest = self._multipart_repository.get_manifest(session_id)
        if manifest is None:
            raise CreatorUploadMultipartConflict(
                "creator upload manifest was not found"
            )
        handle = self._handle_from_private_state(session, manifest)
        existing = self._multipart_repository.get_part(
            session_id,
            part_number,
        )
        if existing is not None:
            self._validate_exact_replay(existing, data)
            return CreatorUploadProgress.from_records(session, manifest)
        if session.status is CreatorUploadStatus.awaiting_attestation:
            raise CreatorUploadMultipartConflict(
                "complete creator upload cannot accept a missing part"
            )

        uploaded = self._storage.upload_part(
            handle,
            part_number=part_number,
            data=data,
        )
        try:
            updated_session, _ = self._multipart_repository.record_part(
                session_id,
                expected_version=expected_version,
                part=uploaded,
            )
        except Exception as primary_error:
            compensation_errors: list[Exception] = []
            try:
                self._creator_repository.abort_creator_upload(
                    session_id,
                    reason="multipart part could not be persisted",
                )
            except Exception as compensation_error:
                compensation_errors.append(compensation_error)
            try:
                self._storage.abort(handle)
            except Exception as compensation_error:
                compensation_errors.append(compensation_error)
            if compensation_errors:
                raise CreatorUploadCompensationError(
                    primary_error,
                    compensation_errors,
                ) from primary_error
            raise
        return CreatorUploadProgress.from_records(updated_session, manifest)

    def _require_active_session(self, session_id: UUID) -> CreatorUploadSession:
        session = self._creator_repository.get_creator_upload(session_id)
        if session is None:
            raise CreatorUploadMultipartConflict(
                "creator upload session was not found"
            )
        if session.status in _TERMINAL_UPLOAD_STATES:
            raise CreatorUploadMultipartConflict(
                "creator upload session is terminal"
            )
        return session

    @staticmethod
    def _handle_from_private_state(
        session: CreatorUploadSession,
        manifest: CreatorUploadManifest,
    ) -> MultipartUploadHandle:
        if session.staging_object_key is None or session.storage_upload_id is None:
            raise CreatorUploadMultipartConflict(
                "creator upload private storage state is incomplete"
            )
        return MultipartUploadHandle(
            bucket="audio-quarantine",
            key=session.staging_object_key,
            upload_id=session.storage_upload_id,
            expected_size_bytes=session.expected_size_bytes,
            content_type=session.content_type,
            expected_sha256=session.expected_sha256,
            part_size_bytes=manifest.part_size_bytes,
        )

    @staticmethod
    def _validate_exact_replay(
        existing: CreatorUploadPartRecord,
        data: bytes,
    ) -> None:
        if (
            existing.size_bytes != len(data)
            or existing.checksum_sha256 != sha256(data).hexdigest()
        ):
            raise CreatorUploadMultipartConflict(
                "multipart part replay conflicts with stored state"
            )
