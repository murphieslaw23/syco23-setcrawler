from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.schemas.creator_upload import (
    CreatorUploadAttestation,
    CreatorUploadSession,
)
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)
from app.services.audio_storage import StoredAudioObject


class InMemoryCreatorUploadFinalizationRepository:
    """Private facade for the creator-upload completion transaction."""

    def __init__(
        self,
        creator_repository: Any,
        multipart_repository: Any,
        abort_repository: Any,
    ) -> None:
        self._creator = creator_repository
        self._multipart = multipart_repository
        self._abort = abort_repository

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

    def list_parts(
        self,
        session_id: UUID,
    ) -> list[CreatorUploadPartRecord]:
        return self._multipart.list_parts(session_id)

    def complete_creator_upload(
        self,
        session_id: UUID,
        *,
        attestation: CreatorUploadAttestation,
        actor: str,
        stored: StoredAudioObject,
    ) -> Any:
        return self._creator.complete_creator_upload(
            session_id,
            attestation=attestation,
            actor=actor,
            stored=stored,
        )

    def abort_creator_upload(
        self,
        session_id: UUID,
        *,
        reason: str,
    ) -> CreatorUploadSession:
        return self._abort.abort_creator_upload(
            session_id,
            reason=reason,
        )


class PostgresCreatorUploadFinalizationRepository:
    """PostgreSQL facade without exposing multipart transport state."""

    def __init__(
        self,
        pool: ConnectionPool,
        creator_repository: Any,
        multipart_repository: Any,
        abort_repository: Any,
    ) -> None:
        self._pool = pool
        self._creator = creator_repository
        self._multipart = multipart_repository
        self._abort = abort_repository

    def get_creator_upload(
        self,
        session_id: UUID,
    ) -> CreatorUploadSession | None:
        return self._creator.get_creator_upload(session_id)

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

    def list_parts(
        self,
        session_id: UUID,
    ) -> list[CreatorUploadPartRecord]:
        return self._multipart.list_parts(session_id)

    def complete_creator_upload(
        self,
        session_id: UUID,
        *,
        attestation: CreatorUploadAttestation,
        actor: str,
        stored: StoredAudioObject,
    ) -> Any:
        return self._creator.complete_creator_upload(
            session_id,
            attestation=attestation,
            actor=actor,
            stored=stored,
        )

    def abort_creator_upload(
        self,
        session_id: UUID,
        *,
        reason: str,
    ) -> CreatorUploadSession:
        return self._abort.abort_creator_upload(
            session_id,
            reason=reason,
        )
