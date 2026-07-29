from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import CurrentUser, require_viewer

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=CurrentUser)
def me(
    current_user: Annotated[CurrentUser, Depends(require_viewer)],
) -> CurrentUser:
    return current_user
