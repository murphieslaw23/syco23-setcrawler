from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.repositories.base import Repository
from app.services.provider_contracts import ProviderCapability
from app.services.provider_health import descriptor_runtime_state
from app.services.provider_registry import ProviderRegistry, ProviderRegistryError
from app.services.cron_schedule import cron_matches, next_cron_time, previous_cron_time
from app.workers.dispatch import JobDispatcher
from app.workers.celery_app import celery_app


def schedule_due_profiles(
    repository: Repository,
    dispatcher: JobDispatcher,
    registry: ProviderRegistry,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    counts = {"due": 0, "created": 0, "dispatched": 0}
    for profile in repository.list_profiles():
        if not profile.enabled:
            continue
        if profile.next_scheduled_at is not None:
            due = profile.next_scheduled_at <= current
        elif cron_matches(
            profile.schedule_cron,
            current,
            timezone=profile.schedule_timezone,
        ):
            due = True
        else:
            previous = previous_cron_time(
                profile.schedule_cron,
                current,
                timezone=profile.schedule_timezone,
            )
            due = profile.created_at.astimezone(UTC) <= previous
        if not due:
            continue
        counts["due"] += 1
        try:
            descriptor = registry.require_capability(
                profile.source,
                ProviderCapability.discovery,
            )
        except ProviderRegistryError:
            continue
        if not descriptor_runtime_state(descriptor, settings)["enabled"]:
            continue
        required = descriptor.discovery_operations.get(profile.operation)
        parameters = profile.parameters
        if required is None or not required.issubset(parameters):
            continue
        queued = repository.queue_profile_with_creation(profile.id)
        if queued is None:
            continue
        job, created = queued
        if not created:
            continue
        counts["created"] += 1
        dispatcher.dispatch_profile(job)
        counts["dispatched"] += 1
        repository.mark_profile_scheduled(
            profile.id,
            scheduled_at=current,
            next_scheduled_at=next_cron_time(
                profile.schedule_cron,
                current,
                timezone=profile.schedule_timezone,
            ),
        )
    return counts


@celery_app.task(name="app.workers.profile_scheduler.schedule_profiles")
def schedule_profiles() -> dict[str, int]:
    from app.services.provider import get_provider_registry
    from app.workers.normalize_worker import get_worker_repository

    return schedule_due_profiles(
        get_worker_repository(),
        JobDispatcher(),
        get_provider_registry(),
        get_settings(),
    )
