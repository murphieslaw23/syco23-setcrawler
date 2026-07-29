from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.core.auth import (
    CurrentUser,
    require_admin,
    require_editor,
    require_viewer,
)
from app.core.dependencies import get_job_dispatcher, get_repository
from app.repositories.base import Repository
from app.schemas import (
    ImportJob,
    ImportJobPage,
    ImportRequest,
    JobStatus,
    JobType,
    SetSource,
)
from app.services.provider import ProviderValidationError
from app.services.ftm import validate_ftm_url
from app.services.soundcloud import validate_soundcloud_url
from app.workers.dispatch import JobDispatcher
from app.routers.dispatching import dispatch_or_terminalize

router = APIRouter(prefix="/imports", tags=["imports"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
DispatcherDependency = Annotated[
    JobDispatcher,
    Depends(get_job_dispatcher),
]
Viewer = Annotated[CurrentUser, Depends(require_viewer)]
Editor = Annotated[CurrentUser, Depends(require_editor)]
Admin = Annotated[CurrentUser, Depends(require_admin)]


def _source_for_url(url: str) -> SetSource | None:
    host = (urlparse(url).hostname or "").casefold()
    if host in {"youtube.com", "www.youtube.com", "youtu.be"}:
        return SetSource.youtube
    if host in {"soundcloud.com", "www.soundcloud.com"}:
        return SetSource.soundcloud
    if host in {"freeteknomusic.org", "www.freeteknomusic.org"}:
        return SetSource.freeteknomusic
    return None


@router.post("/url", response_model=ImportJob, status_code=status.HTTP_202_ACCEPTED)
def import_url(
    payload: ImportRequest,
    request: Request,
    repository: RepositoryDependency,
    dispatcher: DispatcherDependency,
    _: Editor,
) -> ImportJob:
    if request.app.state.settings.provider_mode != "live":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Provider imports are disabled in fixture mode",
        )
    url = str(payload.url)
    detected = _source_for_url(url)
    if not detected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported provider URL")
    if payload.source and payload.source != detected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source does not match URL")
    if detected is SetSource.soundcloud:
        try:
            url = validate_soundcloud_url(url)
        except ProviderValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid SoundCloud track URL",
            ) from error
    if detected is SetSource.freeteknomusic:
        try:
            url = validate_ftm_url(url)
        except ProviderValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid FreeTeknoMusic URL",
            ) from error
    job = repository.create_job(
        url=url,
        source=detected,
        job_type=JobType.url_import,
    )
    dispatch_or_terminalize(repository, job, dispatcher.dispatch_url)
    return job


@router.get("/queue", response_model=ImportJobPage)
def list_queue(
    repository: RepositoryDependency,
    _: Viewer,
    source: SetSource | None = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ImportJobPage:
    return repository.list_jobs(
        source=source,
        status=job_status,
        limit=limit,
        offset=offset,
    )


@router.get("/queue/{job_id}", response_model=ImportJob)
def get_job(
    job_id: UUID,
    repository: RepositoryDependency,
    _: Viewer,
) -> ImportJob:
    job = repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found")
    return job


@router.post(
    "/queue/{job_id}/retry",
    response_model=ImportJob,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(
    job_id: UUID,
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
    previous = repository.get_job(job_id)
    if previous is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Import job not found",
        )
    if previous.status not in {
        JobStatus.failed,
        JobStatus.dead_letter,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed or dead-letter jobs can be retried",
        )
    retried = repository.create_retry_job(job_id)
    if retried is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed or dead-letter jobs can be retried",
        )
    job, created = retried
    if created:
        dispatch_or_terminalize(repository, job, dispatcher.retry)
    return job
