from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def _queue(command: list[str]) -> str:
    return command[command.index("-Q") + 1]


def test_lifecycle_worker_is_isolated_and_opt_in_locally() -> None:
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    process_environment = base["services"]["worker-process"]["environment"]
    assert "MINIO_ACCESS_KEY" not in process_environment
    assert "MINIO_SECRET_KEY" not in process_environment
    assert "AUDIO_LIFECYCLE_EXECUTOR_ENABLED" not in process_environment

    overlay = yaml.safe_load(
        (ROOT / "docker-compose.audio-lifecycle.yml").read_text()
    )
    worker = overlay["services"]["worker-audio-lifecycle"]
    assert worker["profiles"] == ["audio-lifecycle"]
    assert _queue(worker["command"]) == "audio-lifecycle"
    assert worker["environment"]["AUDIO_STORAGE_ENABLED"] == "true"
    assert worker["environment"]["AUDIO_LIFECYCLE_EXECUTOR_ENABLED"] == "true"
    assert worker["environment"]["MINIO_ENDPOINT"] == "minio:9000"
    assert "MINIO_ACCESS_KEY" in worker["environment"]
    assert "MINIO_SECRET_KEY" in worker["environment"]
    assert worker["read_only"] is True
    assert overlay["services"]["worker-beat"]["profiles"] == [
        "audio-lifecycle"
    ]


def test_production_lifecycle_overlay_requires_private_credentials() -> None:
    overlay = yaml.safe_load(
        (ROOT / "docker-compose.audio-lifecycle.production.yml").read_text()
    )
    worker = overlay["services"]["worker-audio-lifecycle"]
    environment = worker["environment"]
    assert worker["profiles"] == ["audio-lifecycle"]
    assert _queue(worker["command"]) == "audio-lifecycle"
    assert environment["ENVIRONMENT"] == "production"
    assert environment["REPOSITORY_MODE"] == "postgres"
    assert environment["AUDIO_STORAGE_ENABLED"] == "true"
    assert environment["AUDIO_LIFECYCLE_EXECUTOR_ENABLED"] == "true"
    assert "?MINIO_ACCESS_KEY is required" in environment["MINIO_ACCESS_KEY"]
    assert "?MINIO_SECRET_KEY is required" in environment["MINIO_SECRET_KEY"]
    assert worker["read_only"] is True
    assert overlay["services"]["worker-beat"]["profiles"] == [
        "audio-lifecycle"
    ]


def test_celery_only_schedules_enabled_lifecycle_execution() -> None:
    text = (
        ROOT / "apps/api/app/workers/celery_app.py"
    ).read_text()
    assert "if settings.audio_lifecycle_executor_enabled:" in text
    assert '"queue": "audio-lifecycle"' in text
    assert '"execute-private-audio-lifecycle-jobs"' in text
