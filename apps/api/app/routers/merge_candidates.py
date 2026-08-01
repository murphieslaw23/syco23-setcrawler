from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import CurrentUser, require_admin
from app.core.dependencies import get_repository
from app.repositories.base import Repository
from app.schemas import (
    MergeCandidate,
    MergeCandidatePage,
    MergeCandidateStatus,
    MergeDecision,
)


router = APIRouter(prefix="/merge-candidates", tags=["merge candidates"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
Admin = Annotated[CurrentUser, Depends(require_admin)]


@router.get("", response_model=MergeCandidatePage)
def list_merge_candidates(
    repository: RepositoryDependency,
    _: Admin,
    status_filter: MergeCandidateStatus | None = Query(
        default=None,
        alias="status",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> MergeCandidatePage:
    return repository.list_merge_candidates(
        status=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get("/{candidate_id}/decisions", response_model=list[MergeDecision])
def list_merge_decisions(
    candidate_id: UUID,
    repository: RepositoryDependency,
    _: Admin,
) -> list[MergeDecision]:
    decisions = repository.list_merge_decisions(candidate_id)
    if repository.get_merge_candidate(candidate_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merge candidate not found",
        )
    return decisions


def _decide(
    candidate_id: UUID,
    action: str,
    repository: Repository,
    current_user: CurrentUser,
) -> MergeCandidate:
    if repository.get_merge_candidate(candidate_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merge candidate not found",
        )
    handler = getattr(repository, f"{action}_merge_candidate")
    candidate = handler(candidate_id, actor=str(current_user.user_id))
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"merge_candidate_{action}_invalid",
        )
    return candidate


@router.post("/{candidate_id}/approve", response_model=MergeCandidate)
def approve_merge_candidate(
    candidate_id: UUID,
    repository: RepositoryDependency,
    current_user: Admin,
) -> MergeCandidate:
    return _decide(candidate_id, "approve", repository, current_user)


@router.post("/{candidate_id}/reject", response_model=MergeCandidate)
def reject_merge_candidate(
    candidate_id: UUID,
    repository: RepositoryDependency,
    current_user: Admin,
) -> MergeCandidate:
    return _decide(candidate_id, "reject", repository, current_user)


@router.post("/{candidate_id}/restore", response_model=MergeCandidate)
def restore_merge_candidate(
    candidate_id: UUID,
    repository: RepositoryDependency,
    current_user: Admin,
) -> MergeCandidate:
    return _decide(candidate_id, "restore", repository, current_user)
