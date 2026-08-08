from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
import json
import os
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from minio.error import S3Error

from app.core.config import get_settings
from app.core.database import create_pool
from app.repositories.postgres import PostgresRepository
from app.schemas.rights import (
    RightsEvidenceInput,
    RightsEvidenceType,
    RightsReviewCreate,
    RightsReviewStatus,
)
from app.services.audio_storage import (
    AUDIO_ORIGINALS_BUCKET,
    AUDIO_QUARANTINE_BUCKET,
    build_audio_storage,
)
from app.workers.celery_app import celery_app


_ACK = "prove-v0.6"
_TASK = "app.workers.audio_lifecycle_worker.execute_audio_lifecycle_jobs"
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchObject"})
_PAYLOAD = b"SYCO23 v0.6 private audio lifecycle proof\n"


def _require_isolated_proof_environment() -> None:
    if os.getenv("AUDIO_LIFECYCLE_PROOF_ACK") != _ACK:
        raise RuntimeError("AUDIO_LIFECYCLE_PROOF_ACK is invalid")
    if os.getenv("AUDIO_LIFECYCLE_PROOF_ISOLATED", "").casefold() != "true":
        raise RuntimeError("isolated lifecycle proof marker is missing")

    settings = get_settings()
    database_host = urlsplit(settings.database_url).hostname
    redis_host = urlsplit(settings.redis_url).hostname
    if settings.environment != "local" or settings.repository_mode != "postgres":
        raise RuntimeError("lifecycle proof requires isolated local PostgreSQL mode")
    if database_host != "db" or redis_host != "redis":
        raise RuntimeError("lifecycle proof refuses non-isolated database or Redis targets")
    if settings.minio_endpoint != "minio:9000" or settings.minio_secure:
        raise RuntimeError("lifecycle proof refuses a non-isolated MinIO target")
    if not settings.audio_storage_enabled or not settings.audio_lifecycle_executor_enabled:
        raise RuntimeError("private audio storage and lifecycle execution must be enabled")


def _insert_source_fixture(
    pool: object,
    *,
    set_id: UUID,
    provider_item_id: UUID,
    external_id: str,
) -> None:
    canonical_url = f"https://soundcloud.com/syco23-proof/{external_id}"
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sets (
                  id, source, source_id, canonical_url, title,
                  duration_seconds, published_at, set_score, review_status
                ) values (
                  %s, 'soundcloud', %s, %s,
                  'Internal v0.6 lifecycle proof', 1200,
                  %s, 0.8, 'inbox'
                )
                """,
                (set_id, external_id, canonical_url, datetime.now(UTC)),
            )
            cursor.execute(
                """
                insert into provider_items (
                  id, provider_id, external_id, canonical_url, title
                ) values (
                  %s, (select id from providers where key = 'soundcloud'),
                  %s, %s, 'Internal v0.6 lifecycle proof'
                )
                """,
                (provider_item_id, external_id, canonical_url),
            )
            cursor.execute(
                """
                insert into set_provider_items (
                  set_id, provider_item_id, relationship, is_primary
                ) values (%s, %s, 'source', true)
                """,
                (set_id, provider_item_id),
            )


def _insert_quarantine_asset(
    pool: object,
    *,
    asset_id: UUID,
    review_id: UUID,
    object_key: str,
    checksum: str,
    size_bytes: int,
) -> None:
    now = datetime.now(UTC)
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into audio_assets (
                  id, rights_review_id, bucket_name, object_key,
                  checksum_sha256, size_bytes, content_type,
                  state, expires_at, created_at, updated_at
                ) values (
                  %s, %s, 'audio-quarantine', %s,
                  %s, %s, 'audio/mpeg', 'quarantine',
                  %s, %s, %s
                )
                """,
                (
                    asset_id,
                    review_id,
                    object_key,
                    checksum,
                    size_bytes,
                    now + timedelta(days=30),
                    now,
                    now,
                ),
            )


def _verify_completed_promotion(pool: object, *, asset_id: UUID, object_key: str) -> None:
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            asset = cursor.execute(
                """
                select state, bucket_name, object_key
                from audio_assets where id = %s
                """,
                (asset_id,),
            ).fetchone()
            job = cursor.execute(
                """
                select status, action
                from audio_asset_lifecycle_jobs
                where audio_asset_id = %s
                order by created_at desc, id desc
                limit 1
                """,
                (asset_id,),
            ).fetchone()
            tombstone = cursor.execute(
                """
                select action, storage_outcome
                from audio_asset_lifecycle_tombstones
                where audio_asset_id = %s
                order by created_at desc, id desc
                limit 1
                """,
                (asset_id,),
            ).fetchone()

    if asset != {
        "state": "approved",
        "bucket_name": AUDIO_ORIGINALS_BUCKET,
        "object_key": object_key,
    }:
        raise RuntimeError("audio asset was not promoted to private originals")
    if job != {"status": "completed", "action": "approve"}:
        raise RuntimeError("audio lifecycle job did not complete approval")
    if tombstone != {"action": "approve", "storage_outcome": "promoted"}:
        raise RuntimeError("audio lifecycle promotion tombstone is missing")


def _assert_quarantine_missing(storage: object, object_key: str) -> None:
    try:
        storage.stat(AUDIO_QUARANTINE_BUCKET, object_key)
    except S3Error as error:
        if error.code in _MISSING_CODES:
            return
        raise
    raise RuntimeError("quarantine object still exists after promotion")


def cleanup(storage: object, object_key: str | None) -> None:
    if object_key is None:
        return
    for bucket in (AUDIO_QUARANTINE_BUCKET, AUDIO_ORIGINALS_BUCKET):
        try:
            storage.delete(bucket, object_key)
        except S3Error as error:
            if error.code not in _MISSING_CODES:
                raise


def main() -> int:
    _require_isolated_proof_environment()
    settings = get_settings()
    pool = create_pool(settings.database_url)
    pool.open()
    pool.wait()
    repository = PostgresRepository(pool)
    storage = build_audio_storage(settings)

    token = uuid4().hex
    set_id = uuid4()
    provider_item_id = uuid4()
    asset_id = uuid4()
    external_id = f"lifecycle-proof-{token}"
    checksum = sha256(_PAYLOAD).hexdigest()
    object_key: str | None = None
    proof_passed = False

    try:
        stored = storage.put_stream(
            AUDIO_QUARANTINE_BUCKET,
            BytesIO(_PAYLOAD),
            length=len(_PAYLOAD),
            content_type="audio/mpeg",
            expected_sha256=checksum,
        )
        object_key = stored.key
        if stored.sha256 != checksum or stored.size != len(_PAYLOAD):
            raise RuntimeError("quarantine write integrity verification failed")

        _insert_source_fixture(
            pool,
            set_id=set_id,
            provider_item_id=provider_item_id,
            external_id=external_id,
        )
        review = repository.create_rights_review(
            RightsReviewCreate(
                set_id=set_id,
                provider_key="soundcloud",
                provider_external_id=external_id,
                requested_stream=True,
                requested_download=False,
                evidence=[
                    RightsEvidenceInput(
                        evidence_type=RightsEvidenceType.creator_attestation,
                        reference_url=f"https://rights.syco23.org/proofs/{token}",
                        assertions={
                            "rights_holder": True,
                            "allows_distribution": True,
                            "allows_derivatives": True,
                        },
                    )
                ],
            ),
            actor="audio-lifecycle-production-proof",
        )
        _insert_quarantine_asset(
            pool,
            asset_id=asset_id,
            review_id=review.id,
            object_key=object_key,
            checksum=checksum,
            size_bytes=len(_PAYLOAD),
        )

        approved = repository.approve_rights_review(
            review.id,
            actor="audio-lifecycle-production-proof",
            allow_stream=True,
            allow_download=False,
            reason="protected v0.6 private lifecycle proof",
        )
        if approved is None or approved.status is not RightsReviewStatus.approved:
            raise RuntimeError("rights approval did not commit")

        task = celery_app.send_task(_TASK, queue="audio-lifecycle")
        processed = task.get(timeout=60, propagate=True)
        if not isinstance(processed, int) or processed < 1:
            raise RuntimeError("execute_audio_lifecycle_jobs processed no lifecycle work")

        _verify_completed_promotion(pool, asset_id=asset_id, object_key=object_key)
        promoted = storage.stat(AUDIO_ORIGINALS_BUCKET, object_key)
        if promoted.sha256 != checksum or promoted.size != len(_PAYLOAD):
            raise RuntimeError("promoted private original failed integrity verification")
        _assert_quarantine_missing(storage, object_key)
        proof_passed = True
        print(
            json.dumps(
                {
                    "proof_passed": True,
                    "rights_handoff": "approved",
                    "lifecycle_job": "completed",
                    "storage_outcome": "promoted",
                    "private_storage": True,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        try:
            cleanup(storage, object_key)
        finally:
            pool.close()
        if not proof_passed:
            print(json.dumps({"proof_passed": False}, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
