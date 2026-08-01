from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import CurrentUser, require_admin
from app.core.dependencies import get_repository
from app.repositories.base import Repository
from app.schemas import (
    RightsDecisionEvent,
    RightsReview,
    RightsReviewApproval,
    RightsReviewCreate,
    RightsReviewPage,
    RightsReviewResolution,
    RightsReviewStatus,
)


router = APIRouter(prefix="/rights-reviews", tags=["rights reviews"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
Admin = Annotated[CurrentUser, Depends(require_admin)]


@router.post("", response_model=RightsReview, status_code=status.HTTP_201_CREATED)
def create_rights_review(
    payload: RightsReviewCreate,
    repository: RepositoryDependency,
    current_user: Admin,
) -> RightsReview:
    try:
        return repository.create_rights_review(
            payload,
            actor=str(current_user.user_id),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@router.get("", response_model=RightsReviewPage)
def list_rights_reviews(
    repository: RepositoryDependency,
    _: Admin,
    status_filter: RightsReviewStatus | None = Query(
        default=None,
        alias="status",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RightsReviewPage:
    return repository.list_rights_reviews(
        status=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get("/{review_id}", response_model=RightsReview)
def get_rights_review(
    review_id: UUID,
    repository: RepositoryDependency,
    _: Admin,
) -> RightsReview:
    review = repository.get_rights_review(review_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rights review not found",
        )
    return review


@router.get(
    "/{review_id}/decisions",
    response_model=list[RightsDecisionEvent],
)
def list_rights_decisions(
    review_id: UUID,
    repository: RepositoryDependency,
    _: Admin,
) -> list[RightsDecisionEvent]:
    if repository.get_rights_review(review_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rights review not found",
        )
    return repository.list_rights_decisions(review_id)


def _decision_result(review: RightsReview | None) -> RightsReview:
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="rights_review_decision_invalid",
        )
    return review


@router.post("/{review_id}/approve", response_model=RightsReview)
def approve_rights_review(
    review_id: UUID,
    payload: RightsReviewApproval,
    repository: RepositoryDependency,
    current_user: Admin,
) -> RightsReview:
    try:
        return _decision_result(
            repository.approve_rights_review(
                review_id,
                actor=str(current_user.user_id),
                allow_stream=payload.allow_stream,
                allow_download=payload.allow_download,
                reason=payload.reason,
            )
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@router.post("/{review_id}/reject", response_model=RightsReview)
def reject_rights_review(
    review_id: UUID,
    payload: RightsReviewResolution,
    repository: RepositoryDependency,
    current_user: Admin,
) -> RightsReview:
    return _decision_result(
        repository.reject_rights_review(
            review_id,
            actor=str(current_user.user_id),
            reason=payload.reason,
        )
    )


@router.post("/{review_id}/expire", response_model=RightsReview)
def expire_rights_review(
    review_id: UUID,
    payload: RightsReviewResolution,
    repository: RepositoryDependency,
    current_user: Admin,
) -> RightsReview:
    return _decision_result(
        repository.expire_rights_review(
            review_id,
            actor=str(current_user.user_id),
            reason=payload.reason,
        )
    )
