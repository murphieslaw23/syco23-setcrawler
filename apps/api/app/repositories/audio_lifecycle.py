from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import Any, Callable
from uuid import UUID, uuid4

from app.schemas.audio import AudioAssetRecord, AudioAssetState, AudioBucket
from app.schemas.audio_lifecycle import (
    AudioLifecycleAction,
    AudioLifecycleJob,
    AudioLifecycleJobStatus,
    AudioLifecycleTombstone,
    AudioStorageOutcome,
)


Clock = Callable[[], datetime]
_ACTIVE_STATUSES = frozenset(
    {
        AudioLifecycleJobStatus.queued,
        AudioLifecycleJobStatus.claimed,
        AudioLifecycleJobStatus.retry,
    }
)


class AudioLifecycleError(RuntimeError):
    """Base error for private audio lifecycle persistence."""


class AudioLifecycleConflict(AudioLifecycleError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


def _transition_asset(
    asset: AudioAssetRecord,
    *,
    action: AudioLifecycleAction,
    destination_key: str | None,
    now: datetime,
) -> AudioAssetRecord:
    if asset.state is not AudioAssetState.quarantine:
        raise AudioLifecycleConflict("audio asset lifecycle state changed")
    if action is AudioLifecycleAction.approve:
        return asset.model_copy(
            update={
                "state": AudioAssetState.approved,
                "bucket_name": AudioBucket.originals,
                "object_key": destination_key or asset.object_key,
                "expires_at": None,
                "updated_at": now,
            }
        )
    return asset.model_copy(
        update={
            "state": (
                AudioAssetState.rejected
                if action is AudioLifecycleAction.reject
                else AudioAssetState.expired
            ),
            "expires_at": None,
            "updated_at": now,
        }
    )


class InMemoryAudioLifecycleRepository:
    def __init__(
        self,
        audio_repository: Any,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._audio_repository = audio_repository
        self._clock = clock
        self._lock = RLock()
        self.jobs: dict[UUID, AudioLifecycleJob] = {}
        self.tombstones: list[AudioLifecycleTombstone] = []

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        return self._audio_repository.get_audio_asset(asset_id)

    def _store_asset(self, asset: AudioAssetRecord) -> None:
        assets = getattr(self._audio_repository, "assets", None)
        if not isinstance(assets, dict):
            raise AudioLifecycleError(
                "in-memory audio repository does not expose mutable assets"
            )
        assets[asset.id] = asset

    def enqueue_lifecycle(
        self,
        audio_asset_id: UUID,
        *,
        action: AudioLifecycleAction,
        actor: str,
        reason: str,
    ) -> AudioLifecycleJob:
        with self._lock:
            asset = self.get_audio_asset(audio_asset_id)
            if asset is None:
                raise AudioLifecycleConflict("audio asset was not found")
            now = self._clock()
            _validate_enqueue(asset, action=action, now=now)
            for job in self.jobs.values():
                if job.audio_asset_id != audio_asset_id or job.status not in _ACTIVE_STATUSES:
                    continue
                if job.action is not action:
                    raise AudioLifecycleConflict(
                        "audio asset already has another active lifecycle action"
                    )
                return job
            job = AudioLifecycleJob(
                audio_asset_id=audio_asset_id,
                action=action,
                actor=actor,
                reason=reason,
                created_at=now,
                updated_at=now,
            )
            self.jobs[job.id] = job
            return job

    def get_lifecycle_job(self, job_id: UUID) -> AudioLifecycleJob | None:
        return self.jobs.get(job_id)

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
        with self._lock:
            candidates = sorted(
                self.jobs.values(),
                key=lambda item: (item.created_at, item.id),
            )
            claimed: list[AudioLifecycleJob] = []
            for job in candidates:
                due = job.status is AudioLifecycleJobStatus.queued
                due = due or (
                    job.status is AudioLifecycleJobStatus.retry
                    and job.next_retry_at is not None
                    and job.next_retry_at <= current
                )
                due = due or (
                    job.status is AudioLifecycleJobStatus.claimed
                    and stale_before is not None
                    and job.claim_started_at is not None
                    and job.claim_started_at <= stale_before
                )
                if not due:
                    continue
                updated = AudioLifecycleJob.model_validate(
                    {
                        **job.model_dump(),
                        "status": AudioLifecycleJobStatus.claimed,
                        "claim_token": uuid4(),
                        "claim_started_at": current,
                        "attempt_count": job.attempt_count + 1,
                        "next_retry_at": None,
                        "last_error": None,
                        "updated_at": current,
                    }
                )
                self.jobs[job.id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
            return claimed

    def complete_lifecycle(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        storage_outcome: AudioStorageOutcome,
        destination_key: str | None = None,
    ) -> tuple[AudioLifecycleJob, AudioLifecycleTombstone, AudioAssetRecord]:
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status is not AudioLifecycleJobStatus.claimed
                or job.claim_token != claim_token
            ):
                raise AudioLifecycleConflict("audio lifecycle claim changed")
            expected_outcome = (
                AudioStorageOutcome.promoted
                if job.action is AudioLifecycleAction.approve
                else AudioStorageOutcome.deleted
            )
            if storage_outcome is not expected_outcome:
                raise AudioLifecycleConflict(
                    "storage outcome does not match lifecycle action"
                )
            asset = self.get_audio_asset(job.audio_asset_id)
            if asset is None:
                raise AudioLifecycleConflict("audio asset was not found")
            now = self._clock()
            transitioned = _transition_asset(
                asset,
                action=job.action,
                destination_key=destination_key,
                now=now,
            )
            completed = AudioLifecycleJob.model_validate(
                {
                    **job.model_dump(),
                    "status": AudioLifecycleJobStatus.completed,
                    "claim_token": None,
                    "claim_started_at": None,
                    "next_retry_at": None,
                    "last_error": None,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            tombstone = AudioLifecycleTombstone(
                lifecycle_job_id=job.id,
                audio_asset_id=asset.id,
                action=job.action,
                actor=job.actor,
                reason=job.reason,
                storage_outcome=storage_outcome,
                checksum_sha256=asset.checksum_sha256,
                size_bytes=asset.size_bytes,
                before_state=asset.model_dump(mode="json"),
                after_state=transitioned.model_dump(mode="json"),
                created_at=now,
            )
            self._store_asset(transitioned)
            self.jobs[job.id] = completed
            self.tombstones.append(tombstone)
            return completed, tombstone, transitioned

    def record_failure(
        self,
        job_id: UUID,
        *,
        claim_token: UUID,
        error: str,
        retry_at: datetime,
        max_attempts: int,
    ) -> AudioLifecycleJob:
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status is not AudioLifecycleJobStatus.claimed
                or job.claim_token != claim_token
            ):
                raise AudioLifecycleConflict("audio lifecycle claim changed")
            now = self._clock()
            terminal = job.attempt_count >= max_attempts
            updated = AudioLifecycleJob.model_validate(
                {
                    **job.model_dump(),
                    "status": (
                        AudioLifecycleJobStatus.failed
                        if terminal
                        else AudioLifecycleJobStatus.retry
                    ),
                    "claim_token": None,
                    "claim_started_at": None,
                    "next_retry_at": None if terminal else retry_at,
                    "last_error": error[:2000],
                    "updated_at": now,
                }
            )
            self.jobs[job.id] = updated
            return updated
