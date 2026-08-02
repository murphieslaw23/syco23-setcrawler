from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest

from app.core.config import Settings


def test_lifecycle_executor_requires_postgres_and_private_storage() -> None:
    with pytest.raises(ValueError, match="audio lifecycle executor"):
        Settings(
            environment="fixture",
            repository_mode="memory",
            audio_lifecycle_executor_enabled=True,
            audio_storage_enabled=True,
            minio_access_key="fixture",
            minio_secret_key="fixture-secret",
        )

    with pytest.raises(ValueError, match="audio lifecycle executor"):
        Settings(
            environment="fixture",
            repository_mode="postgres",
            audio_lifecycle_executor_enabled=True,
            audio_storage_enabled=False,
        )


def test_disabled_lifecycle_worker_is_a_strict_noop(monkeypatch) -> None:
    worker = import_module("app.workers.audio_lifecycle_worker")
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(audio_lifecycle_executor_enabled=False),
    )
    monkeypatch.setattr(
        worker,
        "get_lifecycle_executor",
        lambda: (_ for _ in ()).throw(
            AssertionError("runtime should not initialize")
        ),
    )

    assert worker.execute_audio_lifecycle_jobs() == 0


def test_enabled_lifecycle_worker_uses_the_configured_batch(monkeypatch) -> None:
    worker = import_module("app.workers.audio_lifecycle_worker")
    calls: list[int] = []
    executor = SimpleNamespace(run_once=lambda *, limit: calls.append(limit) or 3)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            environment="fixture",
            audio_lifecycle_executor_enabled=True,
            audio_lifecycle_batch_size=7,
        ),
    )
    monkeypatch.setattr(worker, "get_lifecycle_executor", lambda: executor)

    assert worker.execute_audio_lifecycle_jobs() == 3
    assert calls == [7]
