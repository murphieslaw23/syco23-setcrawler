from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.repositories.audio_lifecycle import AudioLifecycleConflict
from app.schemas.audio import AudioAssetRecord, AudioAssetState, AudioBucket
from app.schemas.audio_lifecycle import (
    AudioLifecycleAction,
    AudioLifecycleJob,
    AudioLifecycleJobStatus,
    AudioLifecycleTombstone,
    AudioStorageOutcome,
)


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _job(row: dict[str, Any]) -> AudioLifecycleJob:
    return AudioLifecycleJob.model_validate(row)


def _asset(row: dict[str, Any]) -> AudioAssetRecord:
    return AudioAssetRecord(
        id=row["id"],
        rights_review_id=row["rights_review_id"],
        state=AudioAssetState(row["state"]),
        bucket_name=AudioBucket(row["bucket_name"]),
        object_key=row["object_key"],
        checksum_sha256=row["checksum_sha256"],
        size_bytes=row["size_bytes"],
        content_type=row.get("content_type"),
        expires_at=row.get("expires_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _tombstone(row: dict[str, Any]) -> AudioLifecycleTombstone:
    return AudioLifecycleTombstone.model_validate(row)


def _validate_enqueue(
    asset: AudioAssetRecord,
    *,
    action: AudioLifecycleAction,
    now: datetime,
) -> None:
    if asset.state is not AudioAssetState.quarantine:
        raise AudioLifecycleConflict("audio asset is not in quarantine")
    if asset.bucket_name is not AudioBucket.quarantine:
        raise AudioLifecycleConflict("quarantine asset has an invalid storage bucket")
    if action is AudioLifecycleAction.expire:
        if asset.expires_at is None or asset.expires_at > now:
            raise AudioLifecycleConflict("audio asset is not expired")


def _transition(
    asset: AudioAssetRecord,
    *,
    action: AudioLifecycleAction,
    destination_key: str | None,
    now: datetime,
) -> AudioAssetRecord:
    if asset.state is not AudioAssetState.quarantine:
        raise AudioLifecycleConflict("audio asset lifecycle state changed")
    if action is AudioLifecycleAction.approve:
        values = {
            "state": AudioAssetState.approved,
            "bucket_name": AudioBucket.originals,
            "object_key": destination_key or asset.object_key,
            "expires_at": None,
            "updated_at": now,
        }
    else:
        values = {
            "state": (
                AudioAssetState.rejected
                if action is AudioLifecycleAction.reject
                else AudioAssetState.expired
            ),
            "expires_at": None,
            "updated_at": now,
        }
    return AudioAssetRecord.model_validate({**asset.model_dump(), **values})


class PostgresAudioLifecycleRepository:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._clock = clock

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from audio_assets where id = %s",
                    (asset_id,),
                ).fetchone()
        return None if row is None else _asset(row)

    def enqueue_lifecycle(
        self,
        audio_asset_id: UUID,
        *,
        action: AudioLifecycleAction,
        actor: str,
        reason: str,
    ) -> AudioLifecycleJob:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                asset_row = cursor.execute(
                    "select * from audio_assets where id = %s for update",
                    (audio_asset_id,),
                ).fetchone()
                if asset_row is None:
                    raise AudioLifecycleConflict("audio asset was not found")
                _validate_enqueue(_asset(asset_row), action=action, now=now)
                existing = cursor.execute(
                    """
                    select * from audio_asset_lifecycle_jobs
                    where audio_asset_id = %s
                      and status in ('queued', 'claimed', 'retry')
                    order by created_at, id
                    limit 1
                    """,
                    (audio_asset_id,),
                ).fetchone()
                if existing is not None:
                    current = _job(existing)
                    if current.action is not action:
                        raise AudioLifecycleConflict(
                            "audio asset already has another active lifecycle action"
                        )
                    return current
                row = cursor.execute(
                    """
                    insert into audio_asset_lifecycle_jobs (
                      audio_asset_id, action, actor, reason,
                      created_at, updated_at
                    ) values (%s, %s, %s, %s, %s, %s)
                    returning *
                    """,
                    (audio_asset_id, action.value, actor, reason, now, now),
                ).fetchone()
        return _job(row)

    def enqueue_expired_assets(
        self,
        *,
        limit: int,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> list[AudioLifecycleJob]:
        if limit < 1:
            return []
        current = now or self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    """
                    with expired as (
                      select assets.id
                      from audio_assets as assets
                      where assets.state = 'quarantine'
                        and assets.bucket_name = 'audio-quarantine'
                        and assets.expires_at is not null
                        and assets.expires_at <= %s
                        and not exists (
                          select 1
                          from audio_asset_lifecycle_jobs as active
                          where active.audio_asset_id = assets.id
                            and active.status in ('queued', 'claimed', 'retry')
                        )
                      order by assets.expires_at, assets.id
                      for update of assets skip locked
                      limit %s
                    )
                    insert into audio_asset_lifecycle_jobs (
                      audio_asset_id, action, actor, reason,
                      created_at, updated_at
                    )
                    select expired.id, 'expire', %s, %s, %s, %s
                    from expired
                    returning *
                    """,
                    (current, limit, actor, reason, current, current),
                ).fetchall()
        return [_job(row) for row in rows]

    def get_lifecycle_job(self, job_id: UUID) -> AudioLifecycleJob | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from audio_asset_lifecycle_jobs where id = %s",
                    (job_id,),
                ).fetchone()
        return None if row is None else _job(row)

    def claim_due(
        self,
        *,
        limit: int,
        now: datetime | None = None,
        stale_before: datetime | None = None,
    ) -> list[AudioLifecycleJob]:
        if limit < 1:
            return []
        current = now or self._clock()
        stale = stale_before or current
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                rows = cursor.execute(
                    """
                    with due as (
                      select id
                      from audio_asset_lifecycle_jobs
                      where status = 'queued'
                         or (status = 'retry' and next_retry_at <= %s)
                         or (status = 'claimed' and claim_started_at <= %s)
                      order by coalesce(next_retry_at, created_at), id
                      for update skip locked
                      limit %s
                    )
                    update audio_asset_lifecycle_jobs as jobs
                    set status = 'claimed',
                        claim_token = gen_random_uuid(),
                        claim_started_at = %s,
                        attempt_count = jobs.attempt_count + 1,
                        next_retry_at = null,
                        last_error = null,
                        updated_at = %s
                    from due
                    where jobs.id = due.id
                    returning jobs.*
                    """,
                    (current, stale, limit, current, current),
                ).fetchall()
        return [_job(row) for row in rows]

    def complete_lifecycle(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        storage_outcome: AudioStorageOutcome,
        destination_key: str | None = None,
    ) -> tuple[AudioLifecycleJob, AudioLifecycleTombstone, AudioAssetRecord]:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                job_row = cursor.execute(
                    """
                    select * from audio_asset_lifecycle_jobs
                    where id = %s for update
                    """,
                    (job_id,),
                ).fetchone()
                if (
                    job_row is None
                    or job_row["status"] != "claimed"
                    or job_row["claim_token"] != claim_token
                ):
                    raise AudioLifecycleConflict("audio lifecycle claim changed")
                current_job = _job(job_row)
                expected = (
                    AudioStorageOutcome.promoted
                    if current_job.action is AudioLifecycleAction.approve
                    else AudioStorageOutcome.deleted
                )
                if storage_outcome is not expected:
                    raise AudioLifecycleConflict(
                        "storage outcome does not match lifecycle action"
                    )
                asset_row = cursor.execute(
                    "select * from audio_assets where id = %s for update",
                    (current_job.audio_asset_id,),
                ).fetchone()
                if asset_row is None:
                    raise AudioLifecycleConflict("audio asset was not found")
                current_asset = _asset(asset_row)
                transitioned = _transition(
                    current_asset,
                    action=current_job.action,
                    destination_key=destination_key,
                    now=now,
                )
                updated_asset = cursor.execute(
                    """
                    update audio_assets
                    set state = %s,
                        bucket_name = %s,
                        object_key = %s,
                        expires_at = %s,
                        updated_at = %s
                    where id = %s
                    returning *
                    """,
                    (
                        transitioned.state.value,
                        transitioned.bucket_name.value,
                        transitioned.object_key,
                        transitioned.expires_at,
                        now,
                        transitioned.id,
                    ),
                ).fetchone()
                tombstone_row = cursor.execute(
                    """
                    insert into audio_asset_lifecycle_tombstones (
                      lifecycle_job_id, audio_asset_id, action,
                      actor, reason, storage_outcome,
                      checksum_sha256, size_bytes,
                      before_state, after_state, created_at
                    ) values (
                      %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s
                    ) returning *
                    """,
                    (
                        current_job.id,
                        current_asset.id,
                        current_job.action.value,
                        current_job.actor,
                        current_job.reason,
                        storage_outcome.value,
                        current_asset.checksum_sha256,
                        current_asset.size_bytes,
                        Jsonb(current_asset.model_dump(mode="json")),
                        Jsonb(transitioned.model_dump(mode="json")),
                        now,
                    ),
                ).fetchone()
                completed_row = cursor.execute(
                    """
                    update audio_asset_lifecycle_jobs
                    set status = 'completed',
                        claim_token = null,
                        claim_started_at = null,
                        next_retry_at = null,
                        last_error = null,
                        completed_at = %s,
                        updated_at = %s
                    where id = %s
                      and status = 'claimed'
                      and claim_token = %s
                    returning *
                    """,
                    (now, now, current_job.id, claim_token),
                ).fetchone()
                if completed_row is None:
                    raise AudioLifecycleConflict("audio lifecycle claim changed")
        return _job(completed_row), _tombstone(tombstone_row), _asset(updated_asset)

    def record_failure(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        error: str,
        retry_at: datetime,
        max_attempts: int,
    ) -> AudioLifecycleJob:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    """
                    select * from audio_asset_lifecycle_jobs
                    where id = %s for update
                    """,
                    (job_id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "claimed"
                    or current["claim_token"] != claim_token
                ):
                    raise AudioLifecycleConflict("audio lifecycle claim changed")
                terminal = current["attempt_count"] >= max_attempts
                row = cursor.execute(
                    """
                    update audio_asset_lifecycle_jobs
                    set status = %s,
                        claim_token = null,
                        claim_started_at = null,
                        next_retry_at = %s,
                        last_error = %s,
                        updated_at = %s
                    where id = %s
                      and status = 'claimed'
                      and claim_token = %s
                    returning *
                    """,
                    (
                        AudioLifecycleJobStatus.failed.value
                        if terminal
                        else AudioLifecycleJobStatus.retry.value,
                        None if terminal else retry_at,
                        error[:2000],
                        now,
                        job_id,
                        claim_token,
                    ),
                ).fetchone()
                if row is None:
                    raise AudioLifecycleConflict("audio lifecycle claim changed")
        return _job(row)
