from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.creator_upload import (
    CreatorUploadSession,
    CreatorUploadStart,
    CreatorUploadStatus,
)
from app.schemas.creator_upload_multipart import CreatorUploadManifest


class CreatorUploadInitializationError(RuntimeError):
    """Raised when initialization fails after durable session creation."""


@dataclass(frozen=True, slots=True)
class CreatorUploadStartReceipt:
    """Client-safe initialization result with no MinIO transport identity."""

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
    ) -> "CreatorUploadStartReceipt":
        if session.id != manifest.session_id:
            raise CreatorUploadInitializationError(
                "creator upload manifest does not match its session"
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
    """Server-only orchestration for resumable upload initialization.

    The coordinator never returns a bucket, object key, MinIO upload ID, ETag,
    credential, or presigned URL. Runtime routes/workers remain disabled until
    later release gates explicitly wire this service.
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
    ) -> CreatorUploadStartReceipt:
        _, session = self._creator_repository.create_creator_upload(
            review_id,
            payload=payload,
            actor=actor,
        )
        handle = None
        try:
            handle = self._storage.start(
                expected_size_bytes=session.expected_size_bytes,
                content_type=session.content_type,
                expected_sha256=session.expected_sha256,
            )
            updated_session, manifest = self._multipart_repository.attach_manifest(
                session.id,
                expected_version=session.version,
                handle=handle,
            )
            return CreatorUploadStartReceipt.from_records(
                updated_session,
                manifest,
            )
        except BaseException as initialization_error:
            if handle is None:
                raise
            try:
                self._storage.abort(handle)
            except BaseException as cleanup_error:
                raise ExceptionGroup(
                    "creator upload initialization and multipart cleanup failed",
                    [initialization_error, cleanup_error],
                ) from initialization_error
            raise
