from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.schemas.audio import AudioAssetRecord, AudioAssetState, AudioBucket
from app.schemas.audio_lifecycle import (
    AudioLifecycleAction,
    AudioLifecycleJob,
    AudioStorageOutcome,
)
from app.services.audio_storage import StoredAudioObject


Clock = Callable[[], datetime]
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchObject"})


class AudioLifecycleExecutionError(RuntimeError):
    """Raised when private storage and durable lifecycle state diverge."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_missing(error: Exception) -> bool:
    return getattr(error, "code", None) in _MISSING_CODES


def _validate_object(
    stored: StoredAudioObject,
    asset: AudioAssetRecord,
    *,
    bucket: AudioBucket,
) -> None:
    if stored.bucket != bucket.value or stored.key != asset.object_key:
        raise AudioLifecycleExecutionError(
            "stored audio identity does not match the lifecycle asset"
        )
    if stored.size != asset.size_bytes:
        raise AudioLifecycleExecutionError(
            "stored audio size does not match the lifecycle asset"
        )
    if stored.sha256 != asset.checksum_sha256:
        raise AudioLifecycleExecutionError(
            "stored audio checksum does not match the lifecycle asset"
        )
    if (
        asset.content_type is not None
        and stored.content_type is not None
        and stored.content_type.casefold() != asset.content_type.casefold()
    ):
        raise AudioLifecycleExecutionError(
            "stored audio content type does not match the lifecycle asset"
        )


class AudioLifecycleExecutor:
    def __init__(
        self,
        repository: Any,
        storage: Any,
        *,
        clock: Clock = _utc_now,
        max_attempts: int = 3,
        retry_delay: timedelta = timedelta(minutes=5),
        claim_timeout: timedelta = timedelta(minutes=15),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive")
        if claim_timeout <= timedelta(0):
            raise ValueError("claim_timeout must be positive")
        self._repository = repository
        self._storage = storage
        self._clock = clock
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._claim_timeout = claim_timeout

    def run_once(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> int:
        current = now or self._clock()
        jobs = self._repository.claim_due(
            limit=limit,
            now=current,
            stale_before=current - self._claim_timeout,
        )
        for job in jobs:
            try:
                self._execute(job)
            except Exception as error:
                if job.claim_token is None:
                    raise AudioLifecycleExecutionError(
                        "claimed lifecycle job has no claim token"
                    ) from error
                self._repository.record_failure(
                    job.id,
                    claim_token=job.claim_token,
                    error=f"{type(error).__name__}: {error}",
                    retry_at=current + self._retry_delay,
                    max_attempts=self._max_attempts,
                )
        return len(jobs)

    def _execute(self, job: AudioLifecycleJob) -> None:
        if job.claim_token is None:
            raise AudioLifecycleExecutionError(
                "claimed lifecycle job has no claim token"
            )
        asset = self._repository.get_audio_asset(job.audio_asset_id)
        if asset is None:
            raise AudioLifecycleExecutionError("audio asset was not found")
        if asset.state is not AudioAssetState.quarantine:
            raise AudioLifecycleExecutionError(
                "audio asset is no longer in quarantine"
            )
        if asset.bucket_name is not AudioBucket.quarantine:
            raise AudioLifecycleExecutionError(
                "quarantine asset has an invalid bucket"
            )

        if job.action is AudioLifecycleAction.approve:
            self._promote(job, asset)
            return
        self._delete(job, asset)

    def _promote(
        self,
        job: AudioLifecycleJob,
        asset: AudioAssetRecord,
    ) -> None:
        promoted = self._stat_optional(AudioBucket.originals, asset.object_key)
        if promoted is None:
            source = self._storage.stat(
                AudioBucket.quarantine.value,
                asset.object_key,
            )
            _validate_object(source, asset, bucket=AudioBucket.quarantine)
            promoted = self._storage.copy_to(
                AudioBucket.quarantine.value,
                asset.object_key,
                AudioBucket.originals.value,
                asset.object_key,
            )
        _validate_object(promoted, asset, bucket=AudioBucket.originals)
        self._delete_optional(AudioBucket.quarantine, asset.object_key)
        self._repository.complete_lifecycle(
            job.id,
            claim_token=job.claim_token,
            storage_outcome=AudioStorageOutcome.promoted,
            destination_key=asset.object_key,
        )

    def _delete(
        self,
        job: AudioLifecycleJob,
        asset: AudioAssetRecord,
    ) -> None:
        source = self._stat_optional(AudioBucket.quarantine, asset.object_key)
        if source is not None:
            _validate_object(source, asset, bucket=AudioBucket.quarantine)
        self._delete_optional(AudioBucket.quarantine, asset.object_key)
        self._repository.complete_lifecycle(
            job.id,
            claim_token=job.claim_token,
            storage_outcome=AudioStorageOutcome.deleted,
        )

    def _stat_optional(
        self,
        bucket: AudioBucket,
        key: str,
    ) -> StoredAudioObject | None:
        try:
            return self._storage.stat(bucket.value, key)
        except Exception as error:
            if _is_missing(error):
                return None
            raise

    def _delete_optional(self, bucket: AudioBucket, key: str) -> None:
        try:
            self._storage.delete(bucket.value, key)
        except Exception as error:
            if not _is_missing(error):
                raise
