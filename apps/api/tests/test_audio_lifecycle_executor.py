from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.repositories.audio_lifecycle import InMemoryAudioLifecycleRepository
from app.schemas.audio import AudioAssetRecord, AudioAssetState, AudioBucket
from app.schemas.audio_lifecycle import (
    AudioLifecycleAction,
    AudioLifecycleJobStatus,
    AudioStorageOutcome,
)
from app.services.audio_lifecycle import AudioLifecycleExecutor
from app.services.audio_storage import StoredAudioObject


NOW = datetime(2026, 8, 2, 18, 0, tzinfo=UTC)
ASSET_ID = UUID("00000000-0000-4000-8000-00000000a101")
RIGHTS_ID = UUID("00000000-0000-4000-8000-00000000a102")
KEY = "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHECKSUM = "a" * 64


class AudioRepositoryStub:
    def __init__(self, asset: AudioAssetRecord) -> None:
        self.assets = {asset.id: asset}

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        return self.assets.get(asset_id)


class StorageStub:
    def __init__(self, asset: AudioAssetRecord) -> None:
        self.objects: dict[tuple[str, str], StoredAudioObject] = {
            (asset.bucket_name.value, asset.object_key): StoredAudioObject(
                bucket=asset.bucket_name.value,
                key=asset.object_key,
                size=asset.size_bytes,
                sha256=asset.checksum_sha256,
                etag="source-etag",
                version_id=None,
                content_type=asset.content_type,
            )
        }
        self.fail_copy_once = False
        self.copy_calls = 0
        self.delete_calls = 0

    def stat(self, bucket: str, key: str) -> StoredAudioObject:
        try:
            return self.objects[(bucket, key)]
        except KeyError as error:
            missing = RuntimeError("object missing")
            missing.code = "NoSuchKey"  # type: ignore[attr-defined]
            raise missing from error

    def copy_to(
        self,
        source_bucket: str,
        source_key: str,
        destination_bucket: str,
        destination_key: str,
    ) -> StoredAudioObject:
        self.copy_calls += 1
        if self.fail_copy_once:
            self.fail_copy_once = False
            raise RuntimeError("temporary copy failure")
        source = self.stat(source_bucket, source_key)
        copied = StoredAudioObject(
            bucket=destination_bucket,
            key=destination_key,
            size=source.size,
            sha256=source.sha256,
            etag="destination-etag",
            version_id=None,
            content_type=source.content_type,
        )
        self.objects[(destination_bucket, destination_key)] = copied
        return copied

    def delete(self, bucket: str, key: str) -> None:
        self.delete_calls += 1
        self.objects.pop((bucket, key), None)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def quarantine_asset(*, expires_at: datetime | None = None) -> AudioAssetRecord:
    return AudioAssetRecord(
        id=ASSET_ID,
        rights_review_id=RIGHTS_ID,
        state=AudioAssetState.quarantine,
        bucket_name=AudioBucket.quarantine,
        object_key=KEY,
        checksum_sha256=CHECKSUM,
        size_bytes=23,
        content_type="audio/mpeg",
        expires_at=expires_at or NOW + timedelta(days=30),
        created_at=NOW,
        updated_at=NOW,
    )


def build_executor(
    asset: AudioAssetRecord,
) -> tuple[AudioLifecycleExecutor, InMemoryAudioLifecycleRepository, StorageStub]:
    audio_repository = AudioRepositoryStub(asset)
    repository = InMemoryAudioLifecycleRepository(
        audio_repository,
        clock=lambda: NOW,
    )
    storage = StorageStub(asset)
    executor = AudioLifecycleExecutor(
        repository,
        storage,
        clock=lambda: NOW,
        max_attempts=3,
        retry_delay=timedelta(minutes=5),
    )
    return executor, repository, storage


def test_approval_promotes_same_key_and_commits_a_tombstone() -> None:
    executor, repository, storage = build_executor(quarantine_asset())
    job = repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.approve,
        actor="admin@example.org",
        reason="rights review approved",
    )

    assert executor.run_once(limit=1) == 1

    completed = repository.get_lifecycle_job(job.id)
    asset = repository.get_audio_asset(ASSET_ID)
    assert completed is not None
    assert completed.status is AudioLifecycleJobStatus.completed
    assert asset is not None
    assert asset.state is AudioAssetState.approved
    assert asset.bucket_name is AudioBucket.originals
    assert asset.object_key == KEY
    assert (AudioBucket.quarantine.value, KEY) not in storage.objects
    assert (AudioBucket.originals.value, KEY) in storage.objects
    assert repository.tombstones[-1].storage_outcome is AudioStorageOutcome.promoted


def test_approval_recovers_when_remote_promotion_preceded_database_completion() -> None:
    executor, repository, storage = build_executor(quarantine_asset())
    source = storage.objects.pop((AudioBucket.quarantine.value, KEY))
    storage.objects[(AudioBucket.originals.value, KEY)] = StoredAudioObject(
        bucket=AudioBucket.originals.value,
        key=KEY,
        size=source.size,
        sha256=source.sha256,
        etag="already-promoted",
        version_id=None,
        content_type=source.content_type,
    )
    repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.approve,
        actor="admin@example.org",
        reason="resume uncertain commit",
    )

    assert executor.run_once(limit=1) == 1
    assert storage.copy_calls == 0
    assert repository.get_audio_asset(ASSET_ID).state is AudioAssetState.approved


def test_rejection_is_idempotent_when_quarantine_object_is_already_missing() -> None:
    executor, repository, storage = build_executor(quarantine_asset())
    storage.objects.clear()
    job = repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.reject,
        actor="admin@example.org",
        reason="rights denied",
    )

    assert executor.run_once(limit=1) == 1

    completed = repository.get_lifecycle_job(job.id)
    assert completed is not None
    assert completed.status is AudioLifecycleJobStatus.completed
    assert repository.get_audio_asset(ASSET_ID).state is AudioAssetState.rejected
    assert repository.tombstones[-1].storage_outcome is AudioStorageOutcome.deleted


def test_transient_storage_failure_retries_with_a_fresh_claim() -> None:
    executor, repository, storage = build_executor(quarantine_asset())
    storage.fail_copy_once = True
    job = repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.approve,
        actor="admin@example.org",
        reason="rights review approved",
    )

    assert executor.run_once(limit=1) == 1
    retry = repository.get_lifecycle_job(job.id)
    assert retry is not None
    assert retry.status is AudioLifecycleJobStatus.retry
    assert retry.claim_token is None
    assert retry.next_retry_at == NOW + timedelta(minutes=5)

    assert executor.run_once(limit=1, now=retry.next_retry_at) == 1
    completed = repository.get_lifecycle_job(job.id)
    assert completed is not None
    assert completed.status is AudioLifecycleJobStatus.completed
    assert completed.attempt_count == 2


def test_retry_deadline_is_measured_from_the_individual_failure_time() -> None:
    asset = quarantine_asset()
    clock = MutableClock(NOW)
    audio_repository = AudioRepositoryStub(asset)
    repository = InMemoryAudioLifecycleRepository(
        audio_repository,
        clock=clock,
    )

    class SlowFailingStorage(StorageStub):
        def copy_to(
            self,
            source_bucket: str,
            source_key: str,
            destination_bucket: str,
            destination_key: str,
        ) -> StoredAudioObject:
            self.copy_calls += 1
            clock.now = NOW + timedelta(minutes=4)
            raise RuntimeError("slow copy failed")

    storage = SlowFailingStorage(asset)
    executor = AudioLifecycleExecutor(
        repository,
        storage,
        clock=clock,
        max_attempts=3,
        retry_delay=timedelta(minutes=5),
    )
    job = repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.approve,
        actor="admin@example.org",
        reason="rights review approved",
    )

    assert executor.run_once(limit=1) == 1

    retry = repository.get_lifecycle_job(job.id)
    assert retry is not None
    assert retry.status is AudioLifecycleJobStatus.retry
    assert retry.next_retry_at == NOW + timedelta(minutes=9)


def test_stale_claims_stop_at_the_configured_attempt_budget() -> None:
    executor, repository, storage = build_executor(quarantine_asset())
    job = repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.approve,
        actor="admin@example.org",
        reason="rights review approved",
    )

    first = repository.claim_due(limit=1, now=NOW)
    assert first[0].attempt_count == 1
    second = repository.claim_due(
        limit=1,
        now=NOW + timedelta(minutes=20),
        stale_before=NOW + timedelta(minutes=5),
    )
    assert second[0].attempt_count == 2
    third = repository.claim_due(
        limit=1,
        now=NOW + timedelta(minutes=40),
        stale_before=NOW + timedelta(minutes=25),
    )
    assert third[0].attempt_count == 3

    assert executor.run_once(limit=1, now=NOW + timedelta(minutes=60)) == 0

    exhausted = repository.get_lifecycle_job(job.id)
    assert exhausted is not None
    assert exhausted.status is AudioLifecycleJobStatus.failed
    assert exhausted.attempt_count == 3
    assert exhausted.claim_token is None
    assert exhausted.claim_started_at is None
    assert exhausted.next_retry_at is None
    assert exhausted.last_error == "stale claim exhausted retry budget"
    assert storage.copy_calls == 0


def test_expiry_cannot_be_enqueued_before_the_asset_deadline() -> None:
    _, repository, _ = build_executor(quarantine_asset())

    try:
        repository.enqueue_lifecycle(
            ASSET_ID,
            action=AudioLifecycleAction.expire,
            actor="system-expiry",
            reason="scheduled expiry",
        )
    except RuntimeError as error:
        assert "not expired" in str(error)
    else:
        raise AssertionError("future quarantine asset accepted for expiry")


def test_executor_claims_and_deletes_expired_quarantine_automatically() -> None:
    executor, repository, storage = build_executor(
        quarantine_asset(expires_at=NOW - timedelta(seconds=1))
    )

    assert executor.run_once(limit=1) == 1

    asset = repository.get_audio_asset(ASSET_ID)
    assert asset is not None
    assert asset.state is AudioAssetState.expired
    assert (AudioBucket.quarantine.value, KEY) not in storage.objects
    assert len(repository.jobs) == 1
    job = next(iter(repository.jobs.values()))
    assert job.action is AudioLifecycleAction.expire
    assert job.status is AudioLifecycleJobStatus.completed
    assert repository.tombstones[-1].storage_outcome is AudioStorageOutcome.deleted
