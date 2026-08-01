from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.auth import CurrentUser, require_viewer
from app.services.provider_health import provider_runtime_states
from app.services.provider import build_provider_registry


router = APIRouter(prefix="/providers", tags=["providers"])
Viewer = Annotated[CurrentUser, Depends(require_viewer)]


@router.get("")
def provider_health(request: Request, _: Viewer) -> dict[str, dict[str, object]]:
    """Expose provider capability flags without returning operational secrets."""
    registry = request.app.state.provider_registry or build_provider_registry(
        request.app.state.settings
    )
    states = provider_runtime_states(
        registry,
        request.app.state.settings,
    )
    return {
        descriptor.public_health_key: {
            **states[descriptor.key],
            "mode": descriptor.health_mode,
        }
        for descriptor in registry.descriptors()
    }
