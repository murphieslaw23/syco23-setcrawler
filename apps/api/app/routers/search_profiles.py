from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.auth import CurrentUser, require_admin, require_viewer
from app.core.dependencies import get_job_dispatcher, get_repository
from app.repositories.base import ActiveProfileJobsError, Repository
from app.schemas import ImportJob, SearchProfile, SearchProfileCreate, SearchProfileUpdate
from app.workers.dispatch import JobDispatcher
from app.routers.dispatching import dispatch_or_terminalize

router = APIRouter(prefix="/search-profiles", tags=["search profiles"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
DispatcherDependency = Annotated[
    JobDispatcher,
    Depends(get_job_dispatcher),
]
Viewer = Annotated[CurrentUser, Depends(require_viewer)]
Admin = Annotated[CurrentUser, Depends(require_admin)]


@router.get("", response_model=list[SearchProfile])
def list_profiles(
    repository: RepositoryDependency,
    _: Viewer,
) -> list[SearchProfile]:
    return repository.list_profiles()


@router.post("", response_model=SearchProfile, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: SearchProfileCreate,
    repository: RepositoryDependency,
    _: Admin,
) -> SearchProfile:
    return repository.create_profile(payload)


@router.patch("/{profile_id}", response_model=SearchProfile)
def update_profile(
    profile_id: UUID,
    payload: SearchProfileUpdate,
    repository: RepositoryDependency,
    _: Admin,
) -> SearchProfile:
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
