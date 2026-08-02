from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest
from psycopg.errors import CheckViolation, RaiseException


FIXED_NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
SET_ID = UUID("00000000-0000-4000-8000-000000009811")
PROVIDER_ITEM_ID = UUID("00000000-0000-4000-8000-000000009812")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000009813")
CREATOR_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000009814")
SECOND_CREATOR_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000009815")
PROVIDER_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000009816")
CREATOR_JOB_ID = UUID("00000000-0000-4000-8000-000000009817")
PROVIDER_JOB_ID = UUID("00000000-0000-4000-8000-000000009818")
SESSION_ID = UUID("00000000-0000-4000-8000-000000009819")
ASSET_ID = UUID("00000000-0000-4000-8000-000000009820")
OBJECT_KEY = "objects/bb/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CHECKSUM = "b" * 64


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for creator upload trigger verification",
)
def test_postgres_creator_upload_session_enforces_database_fences() -> None:
    database = import_module("app.core.database")
    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'creator-upload-source',
                  'https://soundcloud.com/fixture/creator-upload-source',
                  'Creator Upload Fixture', 3600,
                  '2026-06-01T00:00:00Z', 0.8, 'inbox'
                )
                """,
                (SET_ID,),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  'creator-upload-source',
                  'https://soundcloud.com/fixture/creator-upload-source',
                  'Creator Upload Fixture'
                )
                """,
                (PROVIDER_ITEM_ID,),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values (%s, %s, 'source', true)
                """,
                (SET_ID, PROVIDER_ITEM_ID),
            )
            cursor.execute(
                """
                insert into rights_reviews (
                  id, set_id, provider_id, provider_external_id,
                  requested_stream, requested_download, submitted_by
                ) values (
                  %s, %s, (select id from providers where key = 'soundcloud'),
                  'creator-upload-source', true, true, 'integration-creator'
                )
                """,
                (REVIEW_ID, SET_ID),
            )
            cursor.execute(
                """
                insert into rights_evidence (
                  id, rights_review_id, evidence_type, reference_url,
                  assertions, submitted_by
                ) values
                  (
                    %s, %s, 'creator_attestation',
                    'https://rights.example/creator/primary',
                    '{"rights_holder": true, "allows_distribution": true, "allows_derivatives": true}'::jsonb,
                    'integration-creator'
                  ),
                  (
                    %s, %s, 'creator_attestation',
                    'https://rights.example/creator/secondary',
                    '{"rights_holder": true, "allows_distribution": true, "allows_derivatives": true}'::jsonb,
                    'integration-creator'
                  ),
                  (
                    %s, %s, 'provider_permission',
                    'https://rights.example/provider/fixture',
                    '{"official_download": true}'::jsonb,
                    'integration-creator'
                  )
                """,
                (
                    CREATOR_EVIDENCE_ID,
                    REVIEW_ID,
                    SECOND_CREATOR_EVIDENCE_ID,
                    REVIEW_ID,
                    PROVIDER_EVIDENCE_ID,
                    REVIEW_ID,
                ),
            )
            cursor.execute(
                """
                insert into audio_input_jobs (
                  id, rights_review_id, candidate_external_id,
                  input_kind, status, created_by
                ) values (
                  %s, %s, 'creator-upload-fixture',
                  'creator_upload', 'queued', 'integration-creator'
                )
                """,
                (CREATOR_JOB_ID, REVIEW_ID),
            )
            cursor.execute(
                """
                insert into audio_input_jobs (
                  id, rights_review_id, provider_id,
                  provider_item_external_id, candidate_external_id,
                  input_kind, source_url, status, created_by
                ) values (
                  %s, %s, (select id from providers where key = 'soundcloud'),
                  'creator-upload-source', 'provider-fixture',
                  'provider_acquisition',
                  'https://api.soundcloud.com/tracks/provider-fixture/download',
                  'queued', 'integration-creator'
                )
                """,
                (PROVIDER_JOB_ID, REVIEW_ID),
            )

    try:
        with pytest.raises(RaiseException, match="creator-upload input job"):
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        insert into creator_upload_sessions (
                          id, audio_input_job_id, rights_review_id,
                          expected_size_bytes, content_type,
                          expires_at, created_by
                        ) values (%s, %s, %s, 23, 'audio/mpeg', %s, %s)
                        """,
                        (
                            SESSION_ID,
                            PROVIDER_JOB_ID,
                            REVIEW_ID,
                            FIXED_NOW + timedelta(hours=24),
                            "integration-creator",
                        ),
                    )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into creator_upload_sessions (
                      id, audio_input_job_id, rights_review_id,
                      expected_size_bytes, content_type, expected_sha256,
                      expires_at, created_by, created_at, updated_at
                    ) values (
                      %s, %s, %s, 23, 'audio/mpeg', %s,
                      %s, 'integration-creator', %s, %s
                    )
                    """,
                    (
                        SESSION_ID,
                        CREATOR_JOB_ID,
                        REVIEW_ID,
                        CHECKSUM,
                        FIXED_NOW + timedelta(hours=24),
                        FIXED_NOW,
                        FIXED_NOW,
                    ),
                )

        with pytest.raises(CheckViolation):
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        update creator_upload_sessions
                        set status = 'uploading'
                        where id = %s
                        """,
                        (SESSION_ID,),
                    )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update creator_upload_sessions
                    set status = 'uploading',
                        staging_object_key = %s,
                        storage_upload_id = 'multipart-fixture',
                        received_size_bytes = 22,
                        version = version + 1
                    where id = %s
                    """,
                    (OBJECT_KEY, SESSION_ID),
                )

        with pytest.raises(CheckViolation):
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        update creator_upload_sessions
                        set status = 'awaiting_attestation'
                        where id = %s
                        """,
                        (SESSION_ID,),
                    )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update creator_upload_sessions
                    set status = 'awaiting_attestation',
                        received_size_bytes = expected_size_bytes,
                        version = version + 1
                    where id = %s
                    """,
                    (SESSION_ID,),
                )

        with pytest.raises(RaiseException, match="attestation must match"):
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        update creator_upload_sessions
                        set status = 'completed',
                            attestation_evidence_id = %s,
                            attested_by = 'integration-creator',
                            attested_at = %s
                        where id = %s
                        """,
                        (PROVIDER_EVIDENCE_ID, FIXED_NOW, SESSION_ID),
                    )

        with pytest.raises(RaiseException, match="before its quarantine asset"):
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        update creator_upload_sessions
                        set status = 'completed',
                            attestation_evidence_id = %s,
                            attested_by = 'integration-creator',
                            attested_at = %s
                        where id = %s
                        """,
                        (CREATOR_EVIDENCE_ID, FIXED_NOW, SESSION_ID),
                    )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into audio_assets (
                      id, rights_review_id, bucket_name, object_key,
                      checksum_sha256, size_bytes, state, expires_at,
                      content_type
                    ) values (
                      %s, %s, 'audio-quarantine', %s,
                      %s, 23, 'quarantine', %s, 'audio/mpeg'
                    )
                    """,
                    (
                        ASSET_ID,
                        REVIEW_ID,
                        OBJECT_KEY,
                        CHECKSUM,
                        FIXED_NOW + timedelta(days=30),
                    ),
                )
                cursor.execute(
                    """
                    update audio_input_jobs
                    set status = 'completed',
                        audio_asset_id = %s,
                        finished_at = %s
                    where id = %s
                    """,
                    (ASSET_ID, FIXED_NOW, CREATOR_JOB_ID),
                )
                cursor.execute(
                    """
                    update creator_upload_sessions
                    set status = 'completed',
                        attestation_evidence_id = %s,
                        attested_by = 'integration-creator',
                        attested_at = %s,
                        version = version + 1
                    where id = %s
                    """,
                    (CREATOR_EVIDENCE_ID, FIXED_NOW, SESSION_ID),
                )

        with pytest.raises(RaiseException, match="attestation is immutable"):
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        update creator_upload_sessions
                        set attestation_evidence_id = %s
                        where id = %s
                        """,
                        (SECOND_CREATOR_EVIDENCE_ID, SESSION_ID),
                    )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select status, received_size_bytes, expected_size_bytes,
                           attestation_evidence_id, attested_by,
                           staging_object_key, version
                    from creator_upload_sessions
                    where id = %s
                    """,
                    (SESSION_ID,),
                ).fetchone()
        assert row["status"] == "completed"
        assert row["received_size_bytes"] == row["expected_size_bytes"] == 23
        assert row["attestation_evidence_id"] == CREATOR_EVIDENCE_ID
        assert row["attested_by"] == "integration-creator"
        assert row["staging_object_key"] == OBJECT_KEY
        assert row["version"] == 3
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                cursor.execute(
                    "delete from creator_upload_sessions where id = %s",
                    (SESSION_ID,),
                )
                cursor.execute(
                    "delete from audio_input_jobs where id in (%s, %s)",
                    (CREATOR_JOB_ID, PROVIDER_JOB_ID),
                )
                cursor.execute("delete from audio_assets where id = %s", (ASSET_ID,))
                cursor.execute(
                    "delete from rights_evidence where rights_review_id = %s",
                    (REVIEW_ID,),
                )
                cursor.execute("delete from rights_reviews where id = %s", (REVIEW_ID,))
                cursor.execute("delete from set_provider_items where set_id = %s", (SET_ID,))
                cursor.execute(
                    "delete from provider_items where id = %s",
                    (PROVIDER_ITEM_ID,),
                )
                cursor.execute("delete from sets where id = %s", (SET_ID,))
        pool.close()
