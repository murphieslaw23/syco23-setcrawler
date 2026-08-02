from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
import os
from uuid import UUID

import pytest

from app.repositories.memory import InMemoryRepository
from app.schemas.creator_upload import (
    CreatorUploadAttestation,
    CreatorUploadStart,
    CreatorUploadStatus,
)
from app.schemas.rights import (
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
)
from app.services.audio_storage import (
    AUDIO_QUARANTINE_BUCKET,
    StoredAudioObject,
)


FIXED_NOW = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)
SET_ID = UUID("00000000-0000-4000-8000-000000000002")
EVIDENCE_URL = "https://rights.example/contracts/creator-upload"
ATTESTATION_URL = "https://rights.example/attestations/creator-upload"
OBJECT_KEY = "objects/cc/cccccccccccccccccccccccccccccccc"
SECOND_OBJECT_KEY = "objects/dd/dddddddddddddddddddddddddddddddd"
CHECKSUM = "c" * 64


def _initial_evidence() -> RightsEvidenceInput:
    return RightsEvidenceInput(
        evidence_type=RightsEvidenceType.contract,
        reference_url=EVIDENCE_URL,
        assertions={"creator_upload_requested": True},
    )


def _attestation(*, expected_version: int) -> CreatorUploadAttestation:
    return CreatorUploadAttestation(
        reference_url=ATTESTATION_URL,
        assertions={
            "rights_holder": True,
            "allows_distribution": True,
            "allows_derivatives": True,
        },
        expected_version=expected_version,
    )


def _stored(
    *,
    key: str = OBJECT_KEY,
    size: int = 23,
    sha256: str = CHECKSUM,
    content_type: str = "audio/mpeg",
) -> StoredAudioObject:
    return StoredAudioObject(
        bucket=AUDIO_QUARANTINE_BUCKET,
        key=key,
        size=size,
        sha256=sha256,
        etag="etag-creator-23",
        version_id=None,
        content_type=content_type,
        metadata={"sha256": sha256},
    )


def _memory_repositories() -> tuple[object, object, object, object]:
    audio = import_module("app.repositories.audio")
    creator = import_module("app.repositories.creator_upload")
    rights_repository = InMemoryRepository.seeded()
    review = rights_repository.create_rights_review(
        RightsReviewCreate(
            set_id=SET_ID,
            provider_key="soundcloud",
            provider_external_id="sc-k-zmk",
            requested_stream=True,
            requested_download=True,
            evidence=[_initial_evidence()],
        ),
        actor="creator-23",
    )
    audio_repository = audio.InMemoryAudioRepository(
        rights_repository,
        clock=lambda: FIXED_NOW,
    )
    creator_repository = creator.InMemoryCreatorUploadRepository(
        rights_repository,
        audio_repository,
        clock=lambda: FIXED_NOW,
    )
    return rights_repository, audio_repository, creator_repository, review


def _complete_memory_upload() -> tuple[object, object, object, object, object]:
    rights_repository, audio_repository, creator_repository, review = (
        _memory_repositories()
    )
    job, initiated = creator_repository.create_creator_upload(
        review.id,
        payload=CreatorUploadStart(
            expected_size_bytes=23,
            content_type="audio/mpeg",
            expected_sha256=CHECKSUM,
        ),
        actor="creator-23",
    )
    uploading = creator_repository.begin_creator_upload(
        initiated.id,
        expected_version=0,
        staging_object_key=OBJECT_KEY,
        storage_upload_id="multipart-creator-23",
    )
    assert uploading is not None
    awaiting = creator_repository.record_creator_upload_progress(
        initiated.id,
        expected_version=1,
        received_size_bytes=23,
    )
    assert awaiting is not None
    return (
        rights_repository,
        audio_repository,
        creator_repository,
        job,
        awaiting,
    )


def test_creator_upload_lifecycle_is_versioned_and_atomic_in_memory() -> None:
    audio = import_module("app.schemas.audio")
    rights = import_module("app.schemas.rights")
    (
        rights_repository,
        audio_repository,
        creator_repository,
        review,
    ) = _memory_repositories()

    job, initiated = creator_repository.create_creator_upload(
        review.id,
        payload=CreatorUploadStart(
            expected_size_bytes=23,
            content_type="audio/mpeg",
            expected_sha256=CHECKSUM,
        ),
        actor="creator-23",
    )

    assert job.input_kind is audio.AudioInputKind.creator_upload
    assert job.status is audio.AudioInputStatus.queued
    assert initiated.status is CreatorUploadStatus.initiated
    assert initiated.version == 0
    assert initiated.expires_at == FIXED_NOW + timedelta(hours=24)
    assert initiated.staging_object_key is None

    uploading = creator_repository.begin_creator_upload(
        initiated.id,
        expected_version=0,
        staging_object_key=OBJECT_KEY,
        storage_upload_id="multipart-creator-23",
    )
    assert uploading is not None
    assert uploading.status is CreatorUploadStatus.uploading
    assert uploading.version == 1
    assert uploading.staging_object_key == OBJECT_KEY
    assert creator_repository.begin_creator_upload(
        initiated.id,
        expected_version=0,
        staging_object_key=SECOND_OBJECT_KEY,
        storage_upload_id="stale-multipart",
    ) is None
    claimed_job = audio_repository.jobs[job.id]
    assert claimed_job.status is audio.AudioInputStatus.processing
    assert claimed_job.claim_started_at == FIXED_NOW

    partial = creator_repository.record_creator_upload_progress(
        initiated.id,
        expected_version=1,
        received_size_bytes=22,
    )
    assert partial is not None
    assert partial.status is CreatorUploadStatus.uploading
    assert partial.version == 2
    with pytest.raises(ValueError, match="backwards"):
        creator_repository.record_creator_upload_progress(
            initiated.id,
            expected_version=2,
            received_size_bytes=21,
        )
    assert creator_repository.record_creator_upload_progress(
        initiated.id,
        expected_version=1,
        received_size_bytes=23,
    ) is None

    awaiting = creator_repository.record_creator_upload_progress(
        initiated.id,
        expected_version=2,
        received_size_bytes=23,
    )
    assert awaiting is not None
    assert awaiting.status is CreatorUploadStatus.awaiting_attestation
    assert awaiting.version == 3

    result = creator_repository.complete_creator_upload(
        initiated.id,
        attestation=_attestation(expected_version=3),
        actor="creator-23",
        stored=_stored(),
    )
    assert result is not None
    completed_session, completed_job, asset = result
    assert completed_session.status is CreatorUploadStatus.completed
    assert completed_session.version == 4
    assert completed_session.attestation_evidence_id is not None
    assert completed_session.attested_by == "creator-23"
    assert completed_job.status is audio.AudioInputStatus.completed
    assert completed_job.audio_asset_id == asset.id
    assert asset.state is audio.AudioAssetState.quarantine
    assert asset.bucket_name is audio.AudioBucket.quarantine
    assert asset.object_key == OBJECT_KEY
    assert asset.expires_at == FIXED_NOW + timedelta(days=30)
    assert audio_repository.assets == {asset.id: asset}

    updated_review = rights_repository.get_rights_review(review.id)
    assert updated_review is not None
    creator_evidence = [
        item
        for item in updated_review.evidence
        if item.evidence_type is rights.RightsEvidenceType.creator_attestation
    ]
    assert len(creator_evidence) == 1
    assert creator_evidence[0].reference_url == ATTESTATION_URL


def test_invalid_stored_upload_rolls_back_memory_completion() -> None:
    creator = import_module("app.repositories.creator_upload")
    (
        rights_repository,
        audio_repository,
        creator_repository,
        job,
        awaiting,
    ) = _complete_memory_upload()
    review_before = rights_repository.get_rights_review(
        awaiting.rights_review_id
    )
    assert review_before is not None

    with pytest.raises(
        creator.CreatorUploadPersistenceDenied,
        match="does not match",
    ):
        creator_repository.complete_creator_upload(
            awaiting.id,
            attestation=_attestation(expected_version=awaiting.version),
            actor="creator-23",
            stored=_stored(key=SECOND_OBJECT_KEY),
        )

    assert creator_repository.get_creator_upload(awaiting.id) == awaiting
    assert audio_repository.jobs[job.id].audio_asset_id is None
    assert audio_repository.assets == {}
    assert rights_repository.get_rights_review(awaiting.rights_review_id) == review_before


def test_rejected_or_expired_review_cannot_start_creator_upload() -> None:
    creator = import_module("app.repositories.creator_upload")
    rights_repository, audio_repository, creator_repository, review = (
        _memory_repositories()
    )
    rejected = rights_repository.reject_rights_review(
        review.id,
        actor="admin-23",
        reason="Evidence rejected",
    )
    assert rejected is not None

    with pytest.raises(
        creator.CreatorUploadPersistenceDenied,
        match="active rights review",
    ):
        creator_repository.create_creator_upload(
            review.id,
            payload=CreatorUploadStart(
                expected_size_bytes=23,
                content_type="audio/mpeg",
            ),
            actor="creator-23",
        )
    assert creator_repository.sessions == {}
    assert audio_repository.jobs == {}


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for creator-upload transaction verification",
)
def test_postgres_creator_upload_completion_is_one_transaction() -> None:
    database = import_module("app.core.database")
    creator = import_module("app.repositories.creator_upload")
    postgres = import_module("app.repositories.postgres")

    pool = database.create_pool(os.environ["TEST_DATABASE_URL"])
    pool.open()
    rights_repository = postgres.PostgresRepository(pool)
    repository = creator.PostgresCreatorUploadRepository(
        pool,
        clock=lambda: FIXED_NOW,
    )
    set_id = UUID("00000000-0000-4000-8000-000000009831")
    provider_item_id = UUID("00000000-0000-4000-8000-000000009832")
    review_id: UUID | None = None
    session_id: UUID | None = None
    job_id: UUID | None = None
    asset_id: UUID | None = None

    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', 'creator-transaction-source',
                  'https://soundcloud.com/fixture/creator-transaction-source',
                  'Creator Transaction Fixture', 3600,
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
                  'creator-transaction-source',
                  'https://soundcloud.com/fixture/creator-transaction-source',
                  'Creator Transaction Fixture'
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
    try:
        review = rights_repository.create_rights_review(
            RightsReviewCreate(
                set_id=set_id,
                provider_key="soundcloud",
                provider_external_id="creator-transaction-source",
                requested_stream=True,
                requested_download=True,
                evidence=[_initial_evidence()],
            ),
            actor="integration-creator",
        )
        review_id = review.id
        job, initiated = repository.create_creator_upload(
            review.id,
            payload=CreatorUploadStart(
                expected_size_bytes=23,
                content_type="audio/mpeg",
                expected_sha256=CHECKSUM,
            ),
            actor="integration-creator",
        )
        session_id = initiated.id
        job_id = job.id
        uploading = repository.begin_creator_upload(
            initiated.id,
            expected_version=0,
            staging_object_key=OBJECT_KEY,
            storage_upload_id="multipart-integration-23",
        )
        assert uploading is not None
        assert repository.begin_creator_upload(
            initiated.id,
            expected_version=0,
            staging_object_key=SECOND_OBJECT_KEY,
            storage_upload_id="stale-multipart",
        ) is None
        awaiting = repository.record_creator_upload_progress(
            initiated.id,
            expected_version=1,
            received_size_bytes=23,
        )
        assert awaiting is not None

        with pytest.raises(
            creator.CreatorUploadPersistenceDenied,
            match="does not match",
        ):
            repository.complete_creator_upload(
                initiated.id,
                attestation=_attestation(expected_version=2),
                actor="integration-creator",
                stored=_stored(key=SECOND_OBJECT_KEY),
            )

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                unchanged = cursor.execute(
                    """
                    select s.status, s.version,
                           j.status as job_status, j.audio_asset_id,
                           count(e.id) filter (
                             where e.evidence_type = 'creator_attestation'
                           ) as attestation_count
                    from creator_upload_sessions s
                    join audio_input_jobs j on j.id = s.audio_input_job_id
                    left join rights_evidence e
                      on e.rights_review_id = s.rights_review_id
                    where s.id = %s
                    group by s.status, s.version, j.status, j.audio_asset_id
                    """,
                    (initiated.id,),
                ).fetchone()
        assert unchanged["status"] == "awaiting_attestation"
        assert unchanged["version"] == 2
        assert unchanged["job_status"] == "processing"
        assert unchanged["audio_asset_id"] is None
        assert unchanged["attestation_count"] == 0

        result = repository.complete_creator_upload(
            initiated.id,
            attestation=_attestation(expected_version=2),
            actor="integration-creator",
            stored=_stored(),
        )
        assert result is not None
        completed_session, completed_job, asset = result
        asset_id = asset.id
        assert completed_session.status is CreatorUploadStatus.completed
        assert completed_session.version == 3
        assert completed_job.audio_asset_id == asset.id

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    select s.status, s.version, s.attestation_evidence_id,
                           s.attested_by, j.status as job_status,
                           j.audio_asset_id, a.state, a.bucket_name,
                           a.object_key, a.checksum_sha256, a.expires_at,
                           e.evidence_type, e.reference_url
                    from creator_upload_sessions s
                    join audio_input_jobs j on j.id = s.audio_input_job_id
                    join audio_assets a on a.id = j.audio_asset_id
                    join rights_evidence e on e.id = s.attestation_evidence_id
                    where s.id = %s
                    """,
                    (initiated.id,),
                ).fetchone()
        assert row["status"] == "completed"
        assert row["version"] == 3
        assert row["attested_by"] == "integration-creator"
        assert row["job_status"] == "completed"
        assert row["audio_asset_id"] == asset.id
        assert row["state"] == "quarantine"
        assert row["bucket_name"] == AUDIO_QUARANTINE_BUCKET
        assert row["object_key"] == OBJECT_KEY
        assert row["checksum_sha256"] == CHECKSUM
        assert row["expires_at"] == FIXED_NOW + timedelta(days=30)
        assert row["evidence_type"] == "creator_attestation"
        assert row["reference_url"] == ATTESTATION_URL
    finally:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local session_replication_role = replica")
                if session_id is not None:
                    cursor.execute(
                        "delete from creator_upload_sessions where id = %s",
                        (session_id,),
                    )
                if job_id is not None:
                    cursor.execute(
                        "delete from audio_input_jobs where id = %s",
                        (job_id,),
                    )
                if asset_id is not None:
                    cursor.execute(
                        "delete from audio_assets where id = %s",
                        (asset_id,),
                    )
                if review_id is not None:
                    cursor.execute(
                        "delete from audio_permissions where rights_review_id = %s",
                        (review_id,),
                    )
                    cursor.execute(
                        "delete from rights_review_events where rights_review_id = %s",
                        (review_id,),
                    )
                    cursor.execute(
                        "delete from rights_evidence where rights_review_id = %s",
                        (review_id,),
                    )
                    cursor.execute(
                        "delete from rights_reviews where id = %s",
                        (review_id,),
                    )
                cursor.execute(
                    "delete from set_provider_items where set_id = %s",
                    (set_id,),
                )
                cursor.execute(
                    "delete from provider_items where id = %s",
                    (provider_item_id,),
                )
                cursor.execute("delete from sets where id = %s", (set_id,))
        pool.close()
