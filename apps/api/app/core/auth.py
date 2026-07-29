from functools import lru_cache
from typing import Annotated, Callable
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from pydantic import BaseModel

from app.core.dependencies import get_repository
from app.repositories.base import Repository
from app.schemas.auth import UserRole


class CurrentUser(BaseModel):
    user_id: UUID
    role: UserRole


_bearer = HTTPBearer(auto_error=False)
_ROLE_RANK = {
    UserRole.viewer: 0,
    UserRole.editor: 1,
    UserRole.admin: 2,
}


@lru_cache(maxsize=8)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


def get_current_user(
    request: Request,
    repository: Annotated[Repository, Depends(get_repository)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    local_role: Annotated[str | None, Header(alias="X-Local-Role")] = None,
) -> CurrentUser:
    settings = request.app.state.settings
    if settings.auth_mode == "local":
        if settings.environment == "production":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Local authentication is disabled in production",
            )
        try:
            role = UserRole(local_role or settings.local_user_role)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid local role",
            ) from error
        return CurrentUser(user_id=settings.local_user_id, role=role)

    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    try:
        jwks_url = (
            f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        )
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(
            credentials.credentials
        )
        claims = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.supabase_jwt_audience,
            issuer=settings.supabase_jwt_issuer,
            options={"require": ["exp", "sub"]},
        )
        user_id = UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
        ) from error

    role = repository.get_user_role(user_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No application role assigned",
        )
    return CurrentUser(user_id=user_id, role=role)


def _require_role(required: UserRole) -> Callable[[CurrentUser], CurrentUser]:
    def dependency(
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        if _ROLE_RANK[current_user.role] < _ROLE_RANK[required]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{required.value} role required",
            )
        return current_user

    return dependency


require_viewer = _require_role(UserRole.viewer)
require_editor = _require_role(UserRole.editor)
require_admin = _require_role(UserRole.admin)
