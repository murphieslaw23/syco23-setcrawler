from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.schemas.audio import AudioAssetRecord, AudioAssetState, AudioBucket
from app.schemas.audio_processing import AudioProcessingJob, AudioProcessingJobStatus
from app.services.audio_processing import AudioProbe
from app.services.audio_storage import StoredAudioObject


Clock = Callable[[], datetime]
_STALE_EXHAUSTED = "stale processing claim exhausted retry budget"
_RETRY_EXHAUSTED = "processing retry budget exhausted"


class AudioProcessingConflict(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _job(row: dict[str, Any]) -> AudioProcessingJob:
    return AudioProcessingJob.model_validate(row)


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


def _version_values(probe: AudioProbe) -> tuple[object, ...]:
    return (
        probe.codec_name,
        probe.format_name,
        probe.duration_seconds,
        probe.bit_rate,
        probe.sample_rate,
        probe.channels,
        Jsonb(probe.tags),
    )


class PostgresAudioProcessingRepository:
    def __init__(self, pool: ConnectionPool, *, clock: Clock = _utc_now) -> None:
        self._pool = pool
        self._clock = clock

    def get_job(self, job_id: UUID) -> AudioProcessingJob | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from audio_processing_jobs where id = %s",
                    (job_id,),
                ).fetchone()
        return None if row is None else _job(row)

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    "select * from audio_assets where id = %s",
                    (asset_id,),
                ).fetchone()
        return None if row is None else _asset(row)

    def claim_due(
        self,
        *,
        limit: int,
        now: datetime | None = None,
        stale_before: datetime | None = None,
        max_attempts: int = 3,
    ) -> list[AudioProcessingJob]:
        if limit < 1:
            return []
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        current = now or self._clock()
        stale = stale_before or current
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                exhausted = cursor.execute(
                    """
                    update audio_processing_jobs
                    set status = 'failed',
                        claim_token = null,
                        claim_started_at = null,
                        next_retry_at = null,
                        last_error = case
                          when status = 'claimed' then %s
                          else %s
                        end,
                        updated_at = %s
                    where attempt_count >= %s
                      and (
                        (status = 'retry' and next_retry_at <= %s)
                        or (status = 'claimed' and claim_started_at <= %s)
                      )
                    returning audio_asset_id
                    """,
                    (
                        _STALE_EXHAUSTED,
                        _RETRY_EXHAUSTED,
                        current,
                        max_attempts,
                        current,
                        stale,
                    ),
                ).fetchall()
                exhausted_ids = [row["audio_asset_id"] for row in exhausted]
                if exhausted_ids:
                    cursor.execute(
                        """
                        update audio_assets
                        set state = 'failed', updated_at = %s
                        where id = any(%s)
                          and state = 'processing'
                          and bucket_name = 'audio-originals'
                        """,
                        (current, exhausted_ids),
                    )

                rows = cursor.execute(
                    """
                    with due as (
                      select jobs.id
                      from audio_processing_jobs as jobs
                      join audio_assets as assets on assets.id = jobs.audio_asset_id
                      where jobs.attempt_count < %s
                        and assets.state in ('approved', 'processing')
                        and assets.bucket_name = 'audio-originals'
                        and (
                          jobs.status = 'queued'
                          or (jobs.status = 'retry' and jobs.next_retry_at <= %s)
                          or (jobs.status = 'claimed' and jobs.claim_started_at <= %s)
                        )
                      order by coalesce(jobs.next_retry_at, jobs.created_at), jobs.id
                      for update of jobs skip locked
                      limit %s
                    )
                    update audio_processing_jobs as jobs
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
                    (max_attempts, current, stale, limit, current, current),
                ).fetchall()
                claimed_ids = [row["audio_asset_id"] for row in rows]
                if claimed_ids:
                    cursor.execute(
                        """
                        update audio_assets
                        set state = 'processing', updated_at = %s
                        where id = any(%s)
                          and state in ('approved', 'processing')
                          and bucket_name = 'audio-originals'
                        """,
                        (current, claimed_ids),
                    )
        return [_job(row) for row in rows]

    def reserve_derivative_key(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        object_key: str,
    ) -> str:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                row = cursor.execute(
                    """
                    update audio_processing_jobs
                    set derivative_object_key = coalesce(derivative_object_key, %s),
                        updated_at = %s
                    where id = %s and status = 'claimed' and claim_token = %s
                    returning derivative_object_key
                    """,
                    (object_key, now, job_id, claim_token),
                ).fetchone()
                if row is None:
                    raise AudioProcessingConflict("audio processing claim changed")
        return row["derivative_object_key"]

    def complete_reuse(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        probe: AudioProbe,
    ) -> AudioProcessingJob:
        return self._complete(
            job_id,
            claim_token=claim_token,
            original_probe=probe,
            derivative=None,
            derivative_probe=None,
        )

    def complete_derivative(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        original_probe: AudioProbe,
        derivative: StoredAudioObject,
        derivative_probe: AudioProbe,
    ) -> AudioProcessingJob:
        if derivative.bucket != AudioBucket.derivatives.value:
            raise ValueError("processing derivative must be stored privately")
        if derivative.content_type != "audio/mpeg":
            raise ValueError("processing derivative must be audio/mpeg")
        return self._complete(
            job_id,
            claim_token=claim_token,
            original_probe=original_probe,
            derivative=derivative,
            derivative_probe=derivative_probe,
        )

    def _complete(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        original_probe: AudioProbe,
        derivative: StoredAudioObject | None,
        derivative_probe: AudioProbe | None,
    ) -> AudioProcessingJob:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                job_row = cursor.execute(
                    "select * from audio_processing_jobs where id = %s for update",
                    (job_id,),
                ).fetchone()
                if (
                    job_row is None
                    or job_row["status"] != "claimed"
                    or job_row["claim_token"] != claim_token
                ):
                    raise AudioProcessingConflict("audio processing claim changed")
                asset_row = cursor.execute(
                    "select * from audio_assets where id = %s for update",
                    (job_row["audio_asset_id"],),
                ).fetchone()
                if asset_row is None:
                    raise AudioProcessingConflict("audio asset was not found")
                asset = _asset(asset_row)
                if (
                    asset.state is not AudioAssetState.processing
                    or asset.bucket_name is not AudioBucket.originals
                ):
                    raise AudioProcessingConflict("audio asset is not processing")

                cursor.execute(
                    """
                    insert into audio_versions (
                      audio_asset_id, version_type, bucket_name, object_key,
                      checksum_sha256, size_bytes, mime_type,
                      codec_name, format_name, duration_seconds,
                      bit_rate, sample_rate, channels, metadata_tags
                    ) values (
                      %s, 'original', 'audio-originals', %s,
                      %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s
                    )
                    on conflict (audio_asset_id, version_type, checksum_sha256)
                    do nothing
                    """,
                    (
                        asset.id,
                        asset.object_key,
                        asset.checksum_sha256,
                        asset.size_bytes,
                        asset.content_type or "application/octet-stream",
                        *_version_values(original_probe),
                    ),
                )

                if derivative is not None:
                    if derivative_probe is None:
                        raise ValueError("derivative probe is required")
                    if job_row["derivative_object_key"] not in {None, derivative.key}:
                        raise AudioProcessingConflict("derivative object identity changed")
                    cursor.execute(
                        """
                        insert into audio_versions (
                          audio_asset_id, version_type, bucket_name, object_key,
                          checksum_sha256, size_bytes, mime_type,
                          codec_name, format_name, duration_seconds,
                          bit_rate, sample_rate, channels, metadata_tags
                        ) values (
                          %s, 'derivative', 'audio-derivatives', %s,
                          %s, %s, 'audio/mpeg',
                          %s, %s, %s, %s, %s, %s, %s
                        )
                        on conflict (audio_asset_id, version_type, checksum_sha256)
                        do nothing
                        """,
                        (
                            asset.id,
                            derivative.key,
                            derivative.sha256,
                            derivative.size,
                            *_version_values(derivative_probe),
                        ),
                    )

                cursor.execute(
                    """
                    update audio_assets
                    set state = 'ready', updated_at = %s
                    where id = %s and state = 'processing'
                    """,
                    (now, asset.id),
                )
                completed = cursor.execute(
                    """
                    update audio_processing_jobs
                    set status = 'completed',
                        claim_token = null,
                        claim_started_at = null,
                        next_retry_at = null,
                        last_error = null,
                        completed_at = %s,
                        updated_at = %s
                    where id = %s and status = 'claimed' and claim_token = %s
                    returning *
                    """,
                    (now, now, job_id, claim_token),
                ).fetchone()
                if completed is None:
                    raise AudioProcessingConflict("audio processing claim changed")
        return _job(completed)

    def record_failure(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        error: str,
        retry_at: datetime,
        max_attempts: int,
    ) -> AudioProcessingJob:
        now = self._clock()
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                current = cursor.execute(
                    "select * from audio_processing_jobs where id = %s for update",
                    (job_id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "claimed"
                    or current["claim_token"] != claim_token
                ):
                    raise AudioProcessingConflict("audio processing claim changed")
                terminal = current["attempt_count"] >= max_attempts
                row = cursor.execute(
                    """
                    update audio_processing_jobs
                    set status = %s,
                        claim_token = null,
                        claim_started_at = null,
                        next_retry_at = %s,
                        last_error = %s,
                        updated_at = %s
                    where id = %s and status = 'claimed' and claim_token = %s
                    returning *
                    """,
                    (
                        "failed" if terminal else "retry",
                        None if terminal else retry_at,
                        error[:2000],
                        now,
                        job_id,
                        claim_token,
                    ),
                ).fetchone()
                if row is None:
                    raise AudioProcessingConflict("audio processing claim changed")
                if terminal:
                    cursor.execute(
                        """
                        update audio_assets
                        set state = 'failed', updated_at = %s
                        where id = %s
                          and state = 'processing'
                          and bucket_name = 'audio-originals'
                        """,
                        (now, current["audio_asset_id"]),
                    )
        return _job(row)
