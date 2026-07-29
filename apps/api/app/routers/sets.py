from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import CurrentUser, require_admin, require_editor, require_viewer
from app.core.dependencies import get_repository
from app.repositories.base import Repository
from app.schemas import ReviewStatus, SetDetail, SetPage, SetPatch, SetSource

router = APIRouter(prefix="/sets", tags=["sets"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
Viewer = Annotated[CurrentUser, Depends(require_viewer)]
Editor = Annotated[CurrentUser, Depends(require_editor)]
Admin = Annotated[CurrentUser, Depends(require_admin)]


@router.get("", response_model=SetPage)
def list_sets(
    repository: RepositoryDependency,
    _: Viewer,
    source: SetSource | None = None,
    status_filter: ReviewStatus | None = Query(default=None, alias="status"),
    min_score: float | None = Query(default=None, ge=0, le=1),
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SetPage:
    return repository.list_sets(
        source=source,
        status=status_filter,
        min_score=min_score,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/{set_id}", response_model=SetDetail)
def get_set(
    set_id: UUID,
    repository: RepositoryDependency,
    _: Viewer,
) -> SetDetail:
    record = repository.get_set(set_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Set not found")
    return record


@router.patch("/{set_id}", response_model=SetDetail)
def patch_set(
    set_id: UUID,
    patch: SetPatch,
    repository: RepositoryDependency,
    current_user: Editor,
) -> SetDetail:
    record = repository.update_set(set_id, patch, actor=str(current_user.user_id))
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Set not found")
    return record


@router.post("/{set_id}/publish", response_model=SetDetail)
def publish_set(
    set_id: UUID,
    repository: RepositoryDependency,
    current_user: Admin,
) -> SetDetail:
    record = repository.get_set(set_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Set not found")
    if record.review_status not in {ReviewStatus.accepted, ReviewStatus.reviewing}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Set must be reviewed before publishing")
    return repository.update_set(  # type: ignore[return-value]
        set_id,
        SetPatch(review_status=ReviewStatus.published),
        actor=str(current_user.user_id),
    )


@router.post("/{set_id}/reject", response_model=SetDetail)
def reject_set(
    set_id: UUID,
    repository: RepositoryDependency,
    current_user: Editor,
) -> SetDetail:
    record = repository.update_set(
        set_id,
        SetPatch(review_status=ReviewStatus.rejected),
        actor=str(current_user.user_id),
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Set not found")
    return record
