from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.repositories.audio_lifecycle import InMemoryAudioLifecycleRepository
from app.schemas.audio import AudioAssetRecord, AudioAssetState, AudioBucket
from app.schemas.audio_lifecycle import (
    AudioLifecycleAction,
    AudioLifecycleJobStatus,
)
from app.services.audio_lifecycle import AudioLifecycleExecutor
from app.services.audio_storage import StoredAudioObject


NOW = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
ASSET_ID = UUID("00000000-0000-4000-8000-00000000c101")
RIGHTS_ID = UUID("00000000-0000-4000-8000-00000000c102")
KEY = "objects/cc/cccccccccccccccccccccccccccccccc"
CHECKSUM = "c" * 64


class AudioRepositoryStub:
    def __init__(self, asset: AudioAssetRecord) -> None:
        self.assets = {asset.id: asset}

    def get_audio_asset(self, asset_id: UUID) -> AudioAssetRecord | None:
        return self.assets.get(asset_id)


class CommitThenRaiseRepository(InMemoryAudioLifecycleRepository):
    def __init__(self, audio_repository: AudioRepositoryStub) -> None:
        super().__init__(audio_repository, clock=lambda: NOW)
        self._raise_after_commit = True

    def complete_lifecycle(self, *args, **kwargs):
        result = super().complete_lifecycle(*args, **kwargs)
        if self._raise_after_commit:
            self._raise_after_commit = False
            raise ConnectionError("database response lost after commit")
        return result


class StorageStub:
    def __init__(self) -> None:
        self.objects = {
            (AudioBucket.quarantine.value, KEY): StoredAudioObject(
                bucket=AudioBucket.quarantine.value,
                key=KEY,
                size=23,
                sha256=CHECKSUM,
                etag="source",
                version_id=None,
                content_type="audio/mpeg",
            )
        }

    def stat(self, bucket: str, key: str) -> StoredAudioObject:
        return self.objects[(bucket, key)]

    def copy_to(
        self,
        source_bucket: str,
        source_key: str,
        destination_bucket: str,
        destination_key: str,
    ) -> StoredAudioObject:
        source = self.objects[(source_bucket, source_key)]
        copied = StoredAudioObject(
            bucket=destination_bucket,
            key=destination_key,
            size=source.size,
            sha256=source.sha256,
            etag="destination",
            version_id=None,
            content_type=source.content_type,
        )
        self.objects[(destination_bucket, destination_key)] = copied
        return copied

    def delete(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


def test_executor_accepts_a_completed_job_after_database_response_loss() -> None:
    asset = AudioAssetRecord(
        id=ASSET_ID,
        rights_review_id=RIGHTS_ID,
        state=AudioAssetState.quarantine,
        bucket_name=AudioBucket.quarantine,
        object_key=KEY,
        checksum_sha256=CHECKSUM,
        size_bytes=23,
        content_type="audio/mpeg",
        expires_at=NOW + timedelta(days=30),
        created_at=NOW,
        updated_at=NOW,
    )
    repository = CommitThenRaiseRepository(AudioRepositoryStub(asset))
    storage = StorageStub()
    executor = AudioLifecycleExecutor(
        repository,
        storage,
        clock=lambda: NOW,
        retry_delay=timedelta(minutes=5),
    )
    job = repository.enqueue_lifecycle(
        ASSET_ID,
        action=AudioLifecycleAction.approve,
        actor="admin@example.org",
        reason="rights approved",
    )

    assert executor.run_once(limit=1) == 1

    completed = repository.get_lifecycle_job(job.id)
    assert completed is not None
    assert completed.status is AudioLifecycleJobStatus.completed
    assert completed.next_retry_at is None
    assert repository.get_audio_asset(ASSET_ID).state is AudioAssetState.approved
