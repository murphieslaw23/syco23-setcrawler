from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from app.core.config import get_settings
from app.core.database import create_pool
from app.repositories.audio_lifecycle_postgres import (
    PostgresAudioLifecycleRepository,
)
from app.services.audio_lifecycle import AudioLifecycleExecutor
from app.services.audio_storage import build_audio_storage
from app.services.operational_health import record_periodic_task_success
from app.workers.celery_app import celery_app


@lru_cache
def get_lifecycle_executor() -> AudioLifecycleExecutor:
    settings = get_settings()
    if not settings.audio_lifecycle_executor_enabled:
        raise RuntimeError("audio lifecycle executor is disabled")
    pool = create_pool(settings.database_url)
    pool.open()
    pool.wait()
    repository = PostgresAudioLifecycleRepository(pool)
    storage = build_audio_storage(settings)
    return AudioLifecycleExecutor(
        repository,
        storage,
        max_attempts=settings.audio_lifecycle_max_attempts,
        retry_delay=timedelta(
            seconds=settings.audio_lifecycle_retry_delay_seconds
        ),
        claim_timeout=timedelta(
            seconds=settings.audio_lifecycle_claim_timeout_seconds
        ),
    )


@celery_app.task(
    name="app.workers.audio_lifecycle_worker.execute_audio_lifecycle_jobs",
)
def execute_audio_lifecycle_jobs() -> int:
    settings = get_settings()
    if not settings.audio_lifecycle_executor_enabled:
        return 0
    processed = get_lifecycle_executor().run_once(
        limit=settings.audio_lifecycle_batch_size
    )
    record_periodic_task_success(
        settings,
        task_name="execute_audio_lifecycle_jobs",
    )
    return processed
