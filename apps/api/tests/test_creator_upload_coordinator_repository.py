from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories.creator_upload_coordinator import (
    InMemoryCreatorUploadCoordinatorRepository,
)
from app.schemas.audio import AudioInputJob, AudioInputKind, AudioInputStatus
from app.schemas.creator_upload import CreatorUploadSession, CreatorUploadStatus
from app.schemas.creator_upload_multipart import (
    CreatorUploadManifest,
    CreatorUploadPartRecord,
)


NOW = datetime(2026, 8, 2, 15, 0, tzinfo=UTC)
SESSION_ID = UUID("00000000-0000-4000-8000-000000009941")
JOB_ID = UUID("00000000-0000-4000-8000-000000009942")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009943")


def _session() -> CreatorUploadSession:
    return CreatorUploadSession(
        id=SESSION_ID,
        audio_input_job_id=JOB_ID,
        rights_review_id=REVIEW_ID,
        expected_size_bytes=23,
        received_size_bytes=0,
        content_type="audio/mpeg",
        staging_object_key="objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        storage_upload_id="coordinator-facade",
        status=CreatorUploadStatus.uploading,
        expires_at=NOW + timedelta(hours=24),
        created_by="creator-23",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _job() -> AudioInputJob:
    return AudioInputJob(
        id=JOB_ID,
        rights_review_id=REVIEW_ID,
        input_kind=AudioInputKind.creator_upload,
        candidate_external_id=f"creator-upload:{SESSION_ID}",
        status=AudioInputStatus.processing,
        attempt_count=1,
        claim_started_at=NOW,
        started_at=NOW,
        created_by="creator-23",
        created_at=NOW,
        updated_at=NOW,
    )


def test_memory_facade_reads_private_ledger_and_aborts_idempotently() -> None:
    session = _session()
    job = _job()
    manifest = CreatorUploadManifest(
        session_id=SESSION_ID,
        part_size_bytes=5 * 1024 * 1024,
        expected_part_count=1,
        created_at=NOW,
    )
    part = CreatorUploadPartRecord(
        session_id=SESSION_ID,
        part_number=1,
        etag="etag-23",
        size_bytes=23,
        checksum_sha256="a" * 64,
        created_at=NOW,
    )
    creator = SimpleNamespace(
        sessions={SESSION_ID: session},
        get_creator_upload=lambda session_id: (
            session if session_id == SESSION_ID else None
        ),
    )
    audio = SimpleNamespace(jobs={JOB_ID: job})
    multipart = SimpleNamespace(
        manifests={SESSION_ID: manifest},
        parts={(SESSION_ID, 1): part},
    )
    repository = InMemoryCreatorUploadCoordinatorRepository(
        creator,
        audio,
        multipart,
        clock=lambda: NOW + timedelta(minutes=1),
    )

    assert repository.get_manifest(SESSION_ID) == manifest
    assert repository.get_part(SESSION_ID, 1) == part

    aborted = repository.abort_creator_upload(
        SESSION_ID,
        reason="ledger persistence failed",
    )

    assert aborted.status is CreatorUploadStatus.aborted
    assert aborted.version == 2
    blocked = audio.jobs[JOB_ID]
    assert blocked.status is AudioInputStatus.blocked
    assert blocked.finished_at == NOW + timedelta(minutes=1)
    assert blocked.details["abort_reason"] == "ledger persistence failed"
    assert blocked.details["abort_source"] == "creator_upload_coordinator"

    replay = repository.abort_creator_upload(
        SESSION_ID,
        reason="duplicate compensation",
    )
    assert replay == aborted
    assert replay.version == 2


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for coordinator abort verification",
)
def test_postgres_facade_atomically_aborts_session_and_job() -> None:
    database = import_module("app.core.database")
    facade_module = import_module(
        "app.repositories.creator_upload_coordinator"
    )

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = facade_module.PostgresCreatorUploadCoordinatorRepository(
        pool,
        SimpleNamespace(),
        clock=lambda: NOW,
    )

    set_id = UUID("00000000-0000-4000-8000-000000009951")
    provider_item_id = UUID("00000000-0000-4000-8000-000000009952")
    review_id = UUID("00000000-0000-4000-8000-000000009953")
    job_id = UUID("00000000-0000-4000-8000-000000009954")
    session_id = UUID("00000000-0000-4000-8000-000000009955")

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'coordinator-source',
                  'https://soundcloud.com/fixture/coordinator-source',
                  'Coordinator Abort Fixture', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (set_id,),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  'coordinator-source',
                  'https://soundcloud.com/fixture/coordinator-source',
                  'Coordinator Abort Fixture'
                )
                """,
                (provider_item_id,),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values (%s, %s, 'source', true)
                """,
                (set_id, provider_item_id),
            )
            cursor.execute(
                """
                insert into rights_reviews (
                  id, set_id, provider_id, provider_external_id,
                  requested_stream, requested_download, submitted_by
                ) values (
                  %s, %s, (select id from providers where key = 'soundcloud'),
                  'coordinator-source', true, true, 'integration-creator'
                )
                """,
                (review_id, set_id),
            )
            cursor.execute(
                """
                insert into audio_input_jobs (
                  id, rights_review_id, candidate_external_id,
                  input_kind, status, attempt_count, claim_started_at,
                  started_at, created_by
                ) values (
                  %s, %s, %s, 'creator_upload', 'processing', 1, %s,
                  %s, 'integration-creator'
                )
                """,
                (job_id, review_id, f"creator-upload:{session_id}", NOW, NOW),
            )
            cursor.execute(
                """
                insert into creator_upload_sessions (
                  id, audio_input_job_id, rights_review_id,
                  expected_size_bytes, content_type,
                  staging_object_key, storage_upload_id,
                  status, expires_at, created_by, version,
                  created_at, updated_at
                ) values (
                  %s, %s, %s, 23, 'audio/mpeg',
                  'objects/bb/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                  'coordinator-postgres', 'uploading',
                  %s, 'integration-creator', 1, %s, %s
                )
                """,
                (
                    session_id,
                    job_id,
                    review_id,
                    NOW + timedelta(hours=24),
                    NOW - timedelta(minutes=1),
                    NOW - timedelta(minutes=1),
                ),
            )

    try:
        aborted = repository.abort_creator_upload(
            session_id,
            reason="multipart ledger unavailable",
        )
        assert aborted.status is CreatorUploadStatus.aborted
        assert aborted.version == 2

        replay = repository.abort_creator_upload(
            session_id,
            reason="duplicate compensation",
        )
        assert replay.version == 2

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select s.status, s.version,
                           j.status as job_status, j.finished_at,
                           j.details->>'abort_reason' as abort_reason,
                           j.details->>'abort_source' as abort_source
                    from creator_upload_sessions s
                    join audio_input_jobs j on j.id = s.audio_input_job_id
                    where s.id = %s
                    """,
                    (session_id,),
                ).fetchone()
        assert row["status"] == "aborted"
        assert row["version"] == 2
        assert row["job_status"] == "blocked"
        assert row["finished_at"] == NOW
        assert row["abort_reason"] == "multipart ledger unavailable"
        assert row["abort_source"] == "creator_upload_coordinator"
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                cursor.execute(
                    "delete from creator_upload_sessions where id = %s",
                    (session_id,),
                )
                cursor.execute("delete from audio_input_jobs where id = %s", (job_id,))
                cursor.execute("delete from rights_reviews where id = %s", (review_id,))
                cursor.execute("delete from set_provider_items where set_id = %s", (set_id,))
                cursor.execute("delete from provider_items where id = %s", (provider_item_id,))
                cursor.execute("delete from sets where id = %s", (set_id,))
        pool.close()
