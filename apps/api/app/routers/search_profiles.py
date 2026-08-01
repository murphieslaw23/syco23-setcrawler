from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.auth import CurrentUser, require_admin, require_viewer
from app.core.dependencies import get_job_dispatcher, get_repository
from app.repositories.base import ActiveProfileJobsError, Repository
from app.schemas import ImportJob, SearchProfile, SearchProfileCreate, SearchProfileUpdate
from app.workers.dispatch import JobDispatcher
from app.routers.dispatching import dispatch_or_terminalize
from app.services.provider import build_provider_registry
from app.services.provider_contracts import ProviderCapability
from app.services.provider_health import descriptor_runtime_state
from app.services.provider_registry import ProviderRegistryError

router = APIRouter(prefix="/search-profiles", tags=["search profiles"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
DispatcherDependency = Annotated[
    JobDispatcher,
    Depends(get_job_dispatcher),
]
Viewer = Annotated[CurrentUser, Depends(require_viewer)]
Admin = Annotated[CurrentUser, Depends(require_admin)]


def _registry(request: Request):
    return request.app.state.provider_registry or build_provider_registry(
        request.app.state.settings
    )


def _validate_profile_descriptor(request: Request, *, source: str, operation: str):
    try:
        descriptor = _registry(request).require_capability(
            source,
            ProviderCapability.discovery,
        )
    except ProviderRegistryError as error:
        detail = (
            "provider_not_registered"
            if "not registered" in str(error)
            else "capability_not_supported"
        )
        raise HTTPException(status_code=422, detail=detail) from error
    if operation not in descriptor.discovery_operations:
        raise HTTPException(status_code=422, detail="provider_operation_invalid")
    return descriptor


@router.get("", response_model=list[SearchProfile])
def list_profiles(
    repository: RepositoryDependency,
    _: Viewer,
) -> list[SearchProfile]:
    return repository.list_profiles()


@router.post("", response_model=SearchProfile, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: SearchProfileCreate,
    request: Request,
    repository: RepositoryDependency,
    _: Admin,
) -> SearchProfile:
    _validate_profile_descriptor(
        request,
        source=payload.source,
        operation=payload.operation,
    )
    return repository.create_profile(payload)


@router.patch("/{profile_id}", response_model=SearchProfile)
def update_profile(
    profile_id: UUID,
    payload: SearchProfileUpdate,
    request: Request,
    repository: RepositoryDependency,
    _: Admin,
) -> SearchProfile:
    existing = repository.get_profile(profile_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search profile not found")
    _validate_profile_descriptor(
        request,
        source=payload.source or existing.source,
        operation=payload.operation or existing.operation,
    )
    profile = repository.update_profile(profile_id, payload)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search profile not found")
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: UUID,
    repository: RepositoryDependency,
    _: Admin,
) -> Response:
    try:
        deleted = repository.delete_profile(profile_id)
    except ActiveProfileJobsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Search profile has an active import job",
        ) from error
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search profile not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{profile_id}/run", response_model=ImportJob, status_code=status.HTTP_202_ACCEPTED)
def run_profile(
    profile_id: UUID,
    request: Request,
    repository: RepositoryDependency,
    dispatcher: DispatcherDependency,
    _: Admin,
) -> ImportJob:
    if request.app.state.settings.provider_mode != "live":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Provider imports are disabled in fixture mode",
        )
    profile = repository.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search profile not found")
    descriptor = _validate_profile_descriptor(
        request,
        source=profile.source,
        operation=profile.operation,
    )
    runtime = descriptor_runtime_state(descriptor, request.app.state.settings)
    if not runtime["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=runtime["reason"],
        )
    queued = repository.queue_profile_with_creation(profile_id)
    if not queued:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search profile not found")
    job, created = queued
    if created:
        dispatch_or_terminalize(
            repository,
            job,
            dispatcher.dispatch_profile,
        )
    return job
