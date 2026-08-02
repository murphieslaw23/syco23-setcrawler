from celery import Celery

from app.core.config import get_settings
from app.core.observability import register_celery_observability


settings = get_settings()
beat_schedule = {
    "redrive-durable-import-jobs": {
        "task": "app.workers.normalize_worker.redrive_import_jobs",
        "schedule": settings.job_redrive_interval_seconds,
    },
    "schedule-enabled-provider-profiles": {
        "task": "app.workers.profile_scheduler.schedule_profiles",
        "schedule": 60,
        "options": {"queue": "process"},
    },
}
if settings.audio_lifecycle_executor_enabled:
    beat_schedule["execute-private-audio-lifecycle-jobs"] = {
        "task": (
            "app.workers.audio_lifecycle_worker."
            "execute_audio_lifecycle_jobs"
        ),
        "schedule": settings.audio_lifecycle_interval_seconds,
        "options": {"queue": "audio-lifecycle"},
    }

celery_app = Celery(
    "syco23_setcrawler",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=False,
    worker_prefetch_multiplier=1,
    beat_schedule=beat_schedule,
    # Compatibility window: legacy producers without an explicit queue still
    # land on aliases consumed by the workload-class workers. Registry-driven
    # dispatch always supplies provider-api or provider-scrape explicitly.
    task_routes={
        "app.workers.youtube_poller.*": {"queue": "youtube"},
        "app.workers.soundcloud_importer.*": {"queue": "soundcloud"},
        "app.workers.ftm_scraper.*": {"queue": "ftm"},
        "app.workers.normalize_worker.*": {"queue": "process"},
        "app.workers.audio_lifecycle_worker.*": {
            "queue": "audio-lifecycle"
        },
    },
)
celery_app.conf.imports = (
    "app.workers.audio_lifecycle_worker",
    "app.workers.ftm_scraper",
    "app.workers.normalize_worker",
    "app.workers.profile_scheduler",
    "app.workers.provider_discovery",
    "app.workers.soundcloud_importer",
    "app.workers.youtube_poller",
)
register_celery_observability(get_settings)
