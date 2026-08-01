from __future__ import annotations

import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.repositories.base import Repository
from app.schemas import JobStatus
from app.services.provider import (
    ProviderError,
    ProviderTemporaryError,
    ProviderValidationError,
    get_provider_registry,
)
from app.services.provider_contracts import (
    DiscoveryPage,
    DiscoveryRequest,
    ProviderCapability,
)
from app.services.provider_health import descriptor_runtime_state
from app.workers.celery_app import celery_app
from app.workers.normalize_worker import _record_retry, get_worker_repository
from app.workers.profile_jobs import claim_profile_job, finalize_profile_failure


def _request_from_job(repository: Repository, job) -> tuple[str, DiscoveryRequest]:
    provider_key = job.details.get("provider_key")
    operation = job.details.get("operation")
    parameters = job.details.get("parameters")
    if (
        not isinstance(provider_key, str)
        or not isinstance(operation, str)
        or not isinstance(parameters, dict)
    ):
        raise ProviderValidationError("provider_discovery_job_invalid")
    if job.profile_id is None:
        raise ProviderValidationError("provider_discovery_profile_missing")
    profile = repository.get_profile(job.profile_id)
    if profile is None or profile.source != provider_key:
        raise ProviderValidationError("provider_discovery_profile_invalid")
    return provider_key, DiscoveryRequest(
        operation=operation,
        parameters=parameters,
        cursor=profile.next_page_token,
    )


@celery_app.task(
    bind=True,
    name="app.workers.provider_discovery.discover_profile",
)
def discover_profile(self, job_id: str) -> dict[str, int] | None:
    repository = get_worker_repository()
    parsed_job_id = UUID(job_id)
    settings = get_settings()
    claimed = claim_profile_job(
        self,
        repository,
        parsed_job_id,
        claim_ttl_seconds=settings.job_claim_ttl_seconds,
    )
    if claimed is None:
        current = repository.get_job(parsed_job_id)
        if current is not None and current.status is JobStatus.completed:
            return {
                "provider_item_count": int(
                    current.details.get("provider_item_count", 0)
                )
            }
        return None
    try:
        if settings.provider_mode != "live":
            finalize_profile_failure(
                repository,
                claimed,
                error_code="provider_mode_fixture",
                status=JobStatus.blocked,
            )
            return None
        provider_key, request = _request_from_job(repository, claimed)
        registry = get_provider_registry()
        descriptor = registry.require_capability(
            provider_key,
            ProviderCapability.discovery,
        )
        runtime = descriptor_runtime_state(descriptor, settings)
        if not runtime["enabled"]:
            finalize_profile_failure(
                repository,
                claimed,
                error_code=str(runtime["reason"]),
                status=JobStatus.blocked,
            )
            return None
        required = descriptor.discovery_operations.get(request.operation)
        if required is None or not required.issubset(request.parameters):
            raise ProviderValidationError("provider_discovery_operation_invalid")
        adapter = registry.adapter(provider_key)
        page = asyncio.run(adapter.discover(request))
        if not isinstance(page, DiscoveryPage):
            raise ProviderValidationError("provider_discovery_page_invalid")
        if any(item.provider_key != provider_key for item in page.items):
            raise ProviderValidationError("provider_discovery_item_mismatch")
        if claimed.started_at is None:
            raise ProviderValidationError("provider_discovery_claim_invalid")
        completed = repository.complete_provider_discovery(
            claimed.id,
            claimed.started_at,
            provider_key=provider_key,
            items=page.items,
            next_cursor=page.next_cursor,
        )
        if completed is None:
            return None
        return {"provider_item_count": len(page.items)}
    except ProviderTemporaryError as error:
        if claimed.started_at is None:
            raise
        delay = _record_retry(
            repository,
            parsed_job_id,
            error,
            self.request.retries,
            claim_started_at=claimed.started_at,
        )
        if delay is None:
            raise
        raise self.retry(
            exc=error,
            countdown=delay,
            max_retries=3,
        )
    except ProviderError as error:
        finalize_profile_failure(
            repository,
            claimed,
            error_code=str(error),
        )
        raise
    except Exception:
        finalize_profile_failure(
            repository,
            claimed,
            error_code="provider_discovery_worker_error",
        )
        raise
