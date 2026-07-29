from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import CurrentUser, require_editor, require_viewer
from app.core.dependencies import get_repository
from app.repositories.base import Repository
from app.schemas import Candidate

router = APIRouter(prefix="/sets/{set_id}/candidates", tags=["candidates"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
Viewer = Annotated[CurrentUser, Depends(require_viewer)]
Editor = Annotated[CurrentUser, Depends(require_editor)]


@router.get("", response_model=list[Candidate])
def list_candidates(
    set_id: UUID,
    repository: RepositoryDependency,
    _: Viewer,
) -> list[Candidate]:
    record = repository.get_set(set_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Set not found")
    return record.candidates


def _decide(
    set_id: UUID,
    candidate_id: UUID,
    accepted: bool,
    repository: Repository,
) -> Candidate:
    candidate = repository.decide_candidate(set_id, candidate_id, accepted)
    if not candidate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


@router.post("/{candidate_id}/accept", response_model=Candidate)
def accept_candidate(
    set_id: UUID,
    candidate_id: UUID,
    repository: RepositoryDependency,
    _: Editor,
) -> Candidate:
    return _decide(set_id, candidate_id, True, repository)


@router.post("/{candidate_id}/reject", response_model=Candidate)
def reject_candidate(
    set_id: UUID,
    candidate_id: UUID,
    repository: RepositoryDependency,
    _: Editor,
) -> Candidate:
    return _decide(set_id, candidate_id, False, repository)
