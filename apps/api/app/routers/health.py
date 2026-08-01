from fastapi import APIRouter, Request

from app.services.provider_health import provider_runtime_states
from app.services.provider import build_provider_registry

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    registry = request.app.state.provider_registry or build_provider_registry(
        request.app.state.settings
    )
    providers = provider_runtime_states(
        registry,
        request.app.state.settings,
    )
    operational = request.app.state.operational_probe(registry)
    return {
        "status": "ok",
        "service": "syco23-setcrawler-api",
        "ready": (
            all(item["configuration_complete"] for item in providers.values())
            and bool(operational["ready"])
        ),
        "providers": providers,
        "dependencies": operational["dependencies"],
        "alerts": operational["alerts"],
    }
