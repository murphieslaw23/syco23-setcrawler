from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.schemas.import_job import ImportJob, JobType
from app.services.provider import get_provider_registry
from app.services.provider_contracts import (
    ProviderCapability,
    ProviderWorkload,
)
from app.services.provider_registry import ProviderRegistry
from app.services.provider_sources import legacy_source_to_provider_key
from app.workers.celery_app import celery_app


class ProviderOperation(StrEnum):
    resolve_metadata = "resolve_metadata"
    discover = "discover"


class ProviderDispatchError(RuntimeError):
    pass


class ProviderDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    provider_key: str = Field(min_length=1, max_length=64)
    capability: ProviderCapability
    operation: ProviderOperation
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


def build_provider_dispatch(job: ImportJob) -> ProviderDispatch:
    provider_key = legacy_source_to_provider_key(job.source)
    if job.job_type is JobType.search_profile:
        if job.profile_id is None:
            raise ProviderDispatchError("discovery job has no profile_id")
        declared_provider = job.details.get("provider_key")
        if isinstance(declared_provider, str):
            provider_key = declared_provider
        arguments: dict[str, JsonValue] = {
            "profile_id": str(job.profile_id),
        }
        operation = job.details.get("operation")
        parameters = job.details.get("parameters")
        if isinstance(operation, str):
            arguments["operation"] = operation
        if isinstance(parameters, dict):
            arguments["parameters"] = parameters
        return ProviderDispatch(
            job_id=job.id,
            provider_key=provider_key,
            capability=ProviderCapability.discovery,
            operation=ProviderOperation.discover,
            arguments=arguments,
        )
    if job.url is None:
        raise ProviderDispatchError("metadata job has no URL")
    return ProviderDispatch(
        job_id=job.id,
        provider_key=provider_key,
        capability=ProviderCapability.metadata,
        operation=ProviderOperation.resolve_metadata,
        arguments={"url": job.url},
    )


def dispatch_job(
    job: ImportJob,
    *,
    registry: ProviderRegistry | None = None,
    celery: Any = celery_app,
    dispatch: ProviderDispatch | None = None,
) -> ProviderDispatch:
    resolved = dispatch or build_provider_dispatch(job)
    if resolved.job_id != job.id:
        raise ProviderDispatchError("dispatch job identity mismatch")
    if resolved.capability in {
        ProviderCapability.authorized_audio,
        ProviderCapability.creator_upload,
    }:
        raise ProviderDispatchError("audio dispatch is prohibited in v0.3")

    selected_registry = registry or get_provider_registry()
    descriptor = selected_registry.require_capability(
        resolved.provider_key,
        resolved.capability,
    )
    workload = descriptor.workload_by_capability[resolved.capability]
    if workload is ProviderWorkload.audio:
        raise ProviderDispatchError("audio queue dispatch is prohibited in v0.3")
    task_name = descriptor.task_by_capability.get(resolved.capability)
    if task_name is None:
        raise ProviderDispatchError(
            f"provider {resolved.provider_key} has no task for {resolved.capability.value}"
        )

    signature = celery.signature(task_name)
    headers = {
        "syco23_provider": resolved.provider_key,
        "syco23_capability": resolved.capability.value,
        "syco23_operation": resolved.operation.value,
        "syco23_arguments": resolved.arguments,
    }
    apply_async = getattr(signature, "apply_async", None)
    if callable(apply_async):
        apply_async(
            args=(str(job.id),),
            queue=workload.value,
            headers=headers,
        )
    else:
        # Compatibility seam for legacy test doubles and one transition window.
        # Real Celery signatures always use apply_async above, preserving the
        # descriptor-owned workload queue and metadata headers.
        delay = getattr(signature, "delay", None)
        if not callable(delay):
            raise ProviderDispatchError("task signature cannot be published")
        delay(str(job.id))
    return resolved
