from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories.creator_upload_multipart import (
    CreatorUploadMultipartConflict,
    InMemoryCreatorUploadMultipartRepository,
)
from app.schemas.audio import AudioInputJob, AudioInputKind, AudioInputStatus
from app.schemas.creator_upload import CreatorUploadSession, CreatorUploadStatus
from app.services.creator_upload_storage import MultipartUploadHandle, UploadedPart


FIXED_NOW = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)
PART_SIZE = 5 * 1024 * 1024
FINAL_SIZE = 23
TOTAL_SIZE = PART_SIZE + FINAL_SIZE
CHECKSUM = "a" * 64
OBJECT_KEY = "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
UPLOAD_ID = "multipart-ledger-23"
SESSION_ID = UUID("00000000-0000-4000-8000-000000009901")
JOB_ID = UUID("00000000-0000-4000-8000-000000009902")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009903")


class _CreatorRepository:
    def __init__(self, session: CreatorUploadSession) -> None:
        self.sessions = {session.id: session}

    def get_creator_upload(self, session_id: UUID) -> CreatorUploadSession | None:
        return self.sessions.get(session_id)


class _AudioRepository:
    def __init__(self, job: AudioInputJob) -> None:
        self.jobs = {job.id: job}


def _session() -> CreatorUploadSession:
    return CreatorUploadSession(
        id=SESSION_ID,
        audio_input_job_id=JOB_ID,
        rights_review_id=REVIEW_ID,
        expected_size_bytes=TOTAL_SIZE,
        content_type="audio/mpeg",
        expected_sha256=CHECKSUM,
        expires_at=FIXED_NOW + timedelta(hours=24),
        created_by="creator-23",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _job() -> AudioInputJob:
    return AudioInputJob(
        id=JOB_ID,
        rights_review_id=REVIEW_ID,
        input_kind=AudioInputKind.creator_upload,
        candidate_external_id=f"creator-upload:{SESSION_ID}",
        expected_sha256=CHECKSUM,
        created_by="creator-23",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _handle(**changes: object) -> MultipartUploadHandle:
    values: dict[str, object] = {
        "bucket": "audio-quarantine",
        "key": OBJECT_KEY,
        "upload_id": UPLOAD_ID,
        "expected_size_bytes": TOTAL_SIZE,
        "content_type": "audio/mpeg",
        "expected_sha256": CHECKSUM,
        "part_size_bytes": PART_SIZE,
    }
    values.update(changes)
    return MultipartUploadHandle(**values)


def _part(
    part_number: int,
    *,
    etag: str | None = None,
    size_bytes: int | None = None,
    checksum_sha256: str | None = None,
) -> UploadedPart:
    expected_size = PART_SIZE if part_number == 1 else FINAL_SIZE
    return UploadedPart(
        part_number=part_number,
        etag=etag or f"etag-{part_number}",
        size_bytes=expected_size if size_bytes is None else size_bytes,
        checksum_sha256=checksum_sha256 or str(part_number) * 64,
    )


def _memory_repository() -> tuple[
    InMemoryCreatorUploadMultipartRepository,
    _CreatorRepository,
    _AudioRepository,
]:
    creator = _CreatorRepository(_session())
    audio = _AudioRepository(_job())
    repository = InMemoryCreatorUploadMultipartRepository(
        creator,
        audio,
        clock=lambda: FIXED_NOW + timedelta(minutes=1),
    )
    return repository, creator, audio


def test_manifest_claims_job_and_is_idempotent_only_for_exact_replay() -> None:
    repository, creator, audio = _memory_repository()
    handle = _handle()

    session, manifest = repository.attach_manifest(
        SESSION_ID,
        expected_version=0,
        handle=handle,
    )

    assert session.status is CreatorUploadStatus.uploading
    assert session.version == 1
    assert session.staging_object_key == OBJECT_KEY
    assert session.storage_upload_id == UPLOAD_ID
    assert manifest.expected_part_count == 2
    assert manifest.part_size_bytes == PART_SIZE
    claimed = audio.jobs[JOB_ID]
    assert claimed.status is AudioInputStatus.processing
    assert claimed.attempt_count == 1
    assert claimed.claim_started_at == FIXED_NOW + timedelta(minutes=1)

    replay_session, replay_manifest = repository.attach_manifest(
        SESSION_ID,
        expected_version=0,
        handle=handle,
    )
    assert replay_session == creator.sessions[SESSION_ID]
    assert replay_manifest == manifest

    with pytest.raises(CreatorUploadMultipartConflict, match="conflicts"):
        repository.attach_manifest(
            SESSION_ID,
            expected_version=1,
            handle=_handle(upload_id="different-upload"),
        )


def test_part_ledger_is_idempotent_and_completes_only_exact_plan() -> None:
    repository, creator, _ = _memory_repository()
    repository.attach_manifest(SESSION_ID, expected_version=0, handle=_handle())

    first_session, first = repository.record_part(
        SESSION_ID,
        expected_version=1,
        part=_part(1),
    )
    assert first_session.status is CreatorUploadStatus.uploading
    assert first_session.received_size_bytes == PART_SIZE
    assert first_session.version == 2

    replay_session, replay = repository.record_part(
        SESSION_ID,
        expected_version=1,
        part=_part(1),
    )
    assert replay_session == creator.sessions[SESSION_ID]
    assert replay == first

    with pytest.raises(CreatorUploadMultipartConflict, match="replay"):
        repository.record_part(
            SESSION_ID,
            expected_version=2,
            part=_part(1, etag="different-etag"),
        )
    with pytest.raises(CreatorUploadMultipartConflict, match="size"):
        repository.record_part(
            SESSION_ID,
            expected_version=2,
            part=_part(2, size_bytes=FINAL_SIZE - 1),
        )

    completed, final = repository.record_part(
        SESSION_ID,
        expected_version=2,
        part=_part(2),
    )
    assert final.part_number == 2
    assert completed.status is CreatorUploadStatus.awaiting_attestation
    assert completed.received_size_bytes == TOTAL_SIZE
    assert completed.version == 3
    assert [part.part_number for part in repository.list_parts(SESSION_ID)] == [1, 2]

    with pytest.raises(CreatorUploadMultipartConflict, match="not accepting"):
        repository.record_part(
            SESSION_ID,
            expected_version=3,
            part=UploadedPart(
                part_number=3,
                etag="etag-3",
                size_bytes=1,
                checksum_sha256="3" * 64,
            ),
        )


def test_manifest_rejects_session_identity_drift_before_state_change() -> None:
    repository, creator, audio = _memory_repository()

    with pytest.raises(Exception, match="size does not match"):
        repository.attach_manifest(
            SESSION_ID,
            expected_version=0,
            handle=_handle(expected_size_bytes=TOTAL_SIZE + 1),
        )

    assert creator.sessions[SESSION_ID].status is CreatorUploadStatus.initiated
    assert audio.jobs[JOB_ID].status is AudioInputStatus.queued
    assert repository.manifests == {}


def test_part_ledger_migration_is_private_immutable_and_plan_bound() -> None:
    migration = (
        Path(__file__).parents[3]
        / "supabase/migrations/20260802150000_creator_upload_part_ledger.sql"
    )
    assert migration.exists()
    text = migration.read_text().casefold()

    assert "create table public.creator_upload_manifests" in text
    assert "create table public.creator_upload_parts" in text
    assert "expected_part_count between 1 and 10000" in text
    assert "multipart manifest does not match the declared upload size" in text
    assert "multipart part size does not match the upload plan" in text
    assert "ledger rows are immutable" in text
    assert "aborted or expired" in text
    assert "enable row level security" in text
    assert "service_role" in text
    assert "never expose to nuxt" in text
    assert "grant" not in "\n".join(
        line for line in text.splitlines() if "anon" in line or "authenticated" in line
    )
    assert "public_url" not in text
    assert "presigned" not in text


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for multipart ledger verification",
)
def test_postgres_part_ledger_fences_replays_and_completes_exact_plan() -> None:
    database = import_module("app.core.database")
    repository_module = import_module("app.repositories.creator_upload_multipart")

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    repository = repository_module.PostgresCreatorUploadMultipartRepository(
        pool,
        clock=lambda: datetime.now(UTC),
    )

    set_id = UUID("00000000-0000-4000-8000-000000009911")
    provider_item_id = UUID("00000000-0000-4000-8000-000000009912")
    review_id = UUID("00000000-0000-4000-8000-000000009913")
    job_id = UUID("00000000-0000-4000-8000-000000009914")
    session_id = UUID("00000000-0000-4000-8000-000000009915")
    object_key = "objects/bb/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    handle = MultipartUploadHandle(
        bucket="audio-quarantine",
        key=object_key,
        upload_id="postgres-multipart-ledger",
        expected_size_bytes=TOTAL_SIZE,
        content_type="audio/mpeg",
        expected_sha256=CHECKSUM,
        part_size_bytes=PART_SIZE,
    )

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'ledger-source',
                  'https://soundcloud.com/fixture/ledger-source',
                  'Multipart Ledger Fixture', 3600,
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
                  'ledger-source',
                  'https://soundcloud.com/fixture/ledger-source',
                  'Multipart Ledger Fixture'
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
                  'ledger-source', true, true, 'integration-creator'
                )
                """,
                (review_id, set_id),
            )
            cursor.execute(
                """
                insert into audio_input_jobs (
                  id, rights_review_id, candidate_external_id,
                  input_kind, expected_sha256, status, created_by
                ) values (
                  %s, %s, %s, 'creator_upload', %s,
                  'queued', 'integration-creator'
                )
                """,
                (job_id, review_id, f"creator-upload:{session_id}", CHECKSUM),
            )
            cursor.execute(
                """
                insert into creator_upload_sessions (
                  id, audio_input_job_id, rights_review_id,
                  expected_size_bytes, content_type, expected_sha256,
                  expires_at, created_by
                ) values (
                  %s, %s, %s, %s, 'audio/mpeg', %s,
                  now() + interval '1 day', 'integration-creator'
                )
                """,
                (session_id, job_id, review_id, TOTAL_SIZE, CHECKSUM),
            )

    try:
        session, manifest = repository.attach_manifest(
            session_id,
            expected_version=0,
            handle=handle,
        )
        assert session.status is CreatorUploadStatus.uploading
        assert manifest.expected_part_count == 2

        first_session, first = repository.record_part(
            session_id,
            expected_version=1,
            part=_part(1),
        )
        assert first_session.received_size_bytes == PART_SIZE

        replay_session, replay = repository.record_part(
            session_id,
            expected_version=1,
            part=_part(1),
        )
        assert replay == first
        assert replay_session.version == 2

        with pytest.raises(
            repository_module.CreatorUploadMultipartConflict,
            match="replay",
        ):
            repository.record_part(
                session_id,
                expected_version=2,
                part=_part(1, checksum_sha256="f" * 64),
            )

        completed, _ = repository.record_part(
            session_id,
            expected_version=2,
            part=_part(2),
        )
        assert completed.status is CreatorUploadStatus.awaiting_attestation
        assert completed.received_size_bytes == TOTAL_SIZE

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select s.status, s.received_size_bytes, s.version,
                           j.status as job_status,
                           m.expected_part_count,
                           count(p.part_number)::integer as part_count,
                           sum(p.size_bytes)::bigint as ledger_bytes
                    from creator_upload_sessions s
                    join audio_input_jobs j on j.id = s.audio_input_job_id
                    join creator_upload_manifests m on m.session_id = s.id
                    join creator_upload_parts p on p.session_id = s.id
                    where s.id = %s
                    group by s.id, j.status, m.expected_part_count
                    """,
                    (session_id,),
                ).fetchone()
        assert row["status"] == "awaiting_attestation"
        assert row["received_size_bytes"] == TOTAL_SIZE
        assert row["version"] == 3
        assert row["job_status"] == "processing"
        assert row["expected_part_count"] == 2
        assert row["part_count"] == 2
        assert row["ledger_bytes"] == TOTAL_SIZE
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                cursor.execute(
                    "delete from creator_upload_parts where session_id = %s",
                    (session_id,),
                )
                cursor.execute(
                    "delete from creator_upload_manifests where session_id = %s",
                    (session_id,),
                )
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
