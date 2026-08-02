from uuid import UUID

import pytest

from app.repositories.creator_upload import CreatorUploadPersistenceDenied
from app.services.audio_storage import StoredAudioObject
from app.services.creator_upload_finalization import (
    CreatorUploadFinalizationCompensationError,
    CreatorUploadFinalizer,
)


SESSION_ID = UUID("00000000-0000-4000-8000-000000009991")


class _Repository:
    def __init__(self) -> None:
        self.events: list[str] = []

    def abort_creator_upload(self, session_id: UUID, *, reason: str) -> None:
        assert session_id == SESSION_ID
        assert reason
        self.events.append("abort")
        raise RuntimeError("database unavailable")


class _Objects:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def delete(self, bucket: str, key: str) -> None:
        assert bucket == "audio-quarantine"
        assert key == "objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.events.append("delete")


def test_denial_cleanup_keeps_bytes_until_abort_is_durable() -> None:
    repository = _Repository()
    objects = _Objects(repository.events)
    finalizer = CreatorUploadFinalizer(repository, object(), objects)
    stored = StoredAudioObject(
        bucket="audio-quarantine",
        key="objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        size=23,
        sha256="a" * 64,
        etag="etag-23",
        version_id=None,
        content_type="audio/mpeg",
    )
    denial = CreatorUploadPersistenceDenied("rights expired")

    with pytest.raises(CreatorUploadFinalizationCompensationError) as error:
        finalizer._cleanup_denied_finalization(
            SESSION_ID,
            stored,
            denial,
        )

    assert error.value.primary_error is denial
    assert len(error.value.compensation_errors) == 1
    assert repository.events == ["abort"]
