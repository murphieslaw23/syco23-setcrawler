from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.auth import CurrentUser, require_viewer
from app.core.dependencies import get_repository
from app.repositories.base import Repository

router = APIRouter(tags=["stats"])
RepositoryDependency = Annotated[Repository, Depends(get_repository)]
Viewer = Annotated[CurrentUser, Depends(require_viewer)]


@router.get("/stats")
def stats(
    repository: RepositoryDependency,
    _: Viewer,
) -> dict[str, Any]:
    return repository.stats()
