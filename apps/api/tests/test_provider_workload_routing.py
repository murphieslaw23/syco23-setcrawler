from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.schemas import ImportJob, JobType, SetSource
from app.services.provider import build_provider_registry
from app.services.provider_contracts import (
    ProviderCapability,
    ProviderWorkload,
)
from app.workers import ftm_scraper, soundcloud_importer, youtube_poller
from app.workers.provider_routing import (
    ProviderDispatchError,
    ProviderOperation,
    build_provider_dispatch,
    dispatch_job,
)


class _Signature:
    def __init__(self, task_name: str) -> None:
        self.task_name = task_name
        self.calls: list[dict] = []

    def apply_async(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Celery:
    def __init__(self) -> None:
        self.signatures: list[_Signature] = []

    def signature(self, task_name: str) -> _Signature:
        signature = _Signature(task_name)
        self.signatures.append(signature)
        return signature


def _settings() -> Settings:
    return Settings(
        environment="fixture",
        repository_mode="memory",
        provider_mode="fixture",
        youtube_api_key="",
        ftm_scraper_enabled=False,
    )


def test_url_and_profile_jobs_produce_normalized_dispatch_envelopes() -> None:
    url_job = ImportJob(
        id=uuid4(),
        url="https://soundcloud.com/syco23/ritual-set",
        source=SetSource.soundcloud,
        job_type=JobType.url_import,
    )
    profile_job = ImportJob(
        id=uuid4(),
        source=SetSource.youtube,
        job_type=JobType.search_profile,
        profile_id=uuid4(),
    )

    url_dispatch = build_provider_dispatch(url_job)
    assert url_dispatch.provider_key == "soundcloud"
    assert url_dispatch.capability is ProviderCapability.metadata
    assert url_dispatch.operation is ProviderOperation.resolve_metadata
    assert url_dispatch.arguments == {
        "url": "https://soundcloud.com/syco23/ritual-set"
    }

    profile_dispatch = build_provider_dispatch(profile_job)
    assert profile_dispatch.provider_key == "youtube"
    assert profile_dispatch.capability is ProviderCapability.discovery
    assert profile_dispatch.operation is ProviderOperation.discover
    assert profile_dispatch.arguments == {
        "profile_id": str(profile_job.profile_id)
    }


def test_descriptor_owns_task_and_workload_routing() -> None:
    registry = build_provider_registry(_settings())
    celery = _Celery()
    job = ImportJob(
        id=uuid4(),
        url="https://www.youtube.com/watch?v=ritual23",
        source=SetSource.youtube,
    )

    dispatch = dispatch_job(job, registry=registry, celery=celery)

    assert dispatch.provider_key == "youtube"
    assert len(celery.signatures) == 1
    signature = celery.signatures[0]
    assert signature.task_name == "app.workers.youtube_poller.import_url"
    assert signature.calls == [
        {
            "args": (str(job.id),),
            "queue": "provider-api",
            "headers": {
                "syco23_provider": "youtube",
                "syco23_capability": "metadata",
                "syco23_operation": "resolve_metadata",
                "syco23_arguments": {
                    "url": "https://www.youtube.com/watch?v=ritual23"
                },
            },
        }
    ]


def test_all_builtin_dispatches_use_workload_queues_and_never_audio() -> None:
    registry = build_provider_registry(_settings())
    celery = _Celery()
    jobs = (
        ImportJob(
            url="https://www.youtube.com/watch?v=ritual23",
            source=SetSource.youtube,
        ),
        ImportJob(
            url="https://soundcloud.com/syco23/ritual-set",
            source=SetSource.soundcloud,
        ),
        ImportJob(
            url="https://freeteknomusic.org/sets/ritual23",
            source=SetSource.freeteknomusic,
        ),
    )

    for job in jobs:
        dispatch_job(job, registry=registry, celery=celery)

    queues = [signature.calls[0]["queue"] for signature in celery.signatures]
    assert queues == ["provider-api", "provider-scrape", "provider-scrape"]
    assert ProviderWorkload.audio.value not in queues


def test_audio_capabilities_and_audio_queue_are_rejected_before_publish() -> None:
    registry = build_provider_registry(_settings())
    celery = _Celery()
    job = ImportJob(
        url="https://soundcloud.com/syco23/ritual-set",
        source=SetSource.soundcloud,
    )
    dispatch = build_provider_dispatch(job).model_copy(
        update={
            "capability": ProviderCapability.authorized_audio,
            "operation": ProviderOperation.resolve_metadata,
        }
    )

    with pytest.raises(ProviderDispatchError, match="audio"):
        dispatch_job(job, registry=registry, celery=celery, dispatch=dispatch)
    assert celery.signatures == []


def test_workers_resolve_builtin_adapters_through_the_registry(monkeypatch) -> None:
    youtube = object()
    soundcloud = object()
    ftm = object()
    adapters = {
        "youtube": youtube,
        "soundcloud": soundcloud,
        "ftm": ftm,
    }
    registry = SimpleNamespace(adapter=lambda key: adapters[key])

    monkeypatch.setattr(youtube_poller, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(soundcloud_importer, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(ftm_scraper, "get_provider_registry", lambda: registry)

    assert youtube_poller.get_youtube_adapter() is youtube
    assert soundcloud_importer.get_soundcloud_adapter() is soundcloud
    assert ftm_scraper.get_ftm_adapter() is ftm


def test_builtin_registry_adapters_preserve_legacy_worker_methods() -> None:
    registry = build_provider_registry(_settings())

    youtube = registry.adapter("youtube")
    soundcloud = registry.adapter("soundcloud")
    ftm = registry.adapter("ftm")

    assert callable(youtube.fetch)
    assert callable(youtube.search)
    assert callable(soundcloud.fetch)
    assert callable(ftm.fetch)
    assert callable(ftm.crawl)
