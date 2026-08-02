from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_lifecycle_worker_is_configurable_but_disabled_by_default() -> None:
    local = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    process_environment = local["services"]["worker-process"]["environment"]
    assert process_environment["AUDIO_LIFECYCLE_EXECUTOR_ENABLED"] == (
        "${AUDIO_LIFECYCLE_EXECUTOR_ENABLED:-false}"
    )
    assert process_environment["AUDIO_STORAGE_ENABLED"] == (
        "${AUDIO_STORAGE_ENABLED:-false}"
    )
    assert process_environment["MINIO_ENDPOINT"] == "minio:9000"
    assert "MINIO_ACCESS_KEY" in process_environment
    assert "MINIO_SECRET_KEY" in process_environment

    production_text = (ROOT / "docker-compose.production.yml").read_text()
    assert (
        'AUDIO_LIFECYCLE_EXECUTOR_ENABLED: '
        '"${AUDIO_LIFECYCLE_EXECUTOR_ENABLED:-false}"'
    ) in production_text
    assert (
        'AUDIO_LIFECYCLE_INTERVAL_SECONDS: '
        '"${AUDIO_LIFECYCLE_INTERVAL_SECONDS:-60}"'
    ) in production_text
