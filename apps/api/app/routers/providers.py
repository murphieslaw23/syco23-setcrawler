from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.auth import CurrentUser, require_viewer
from app.core.config import Settings


router = APIRouter(prefix="/providers", tags=["providers"])
Viewer = Annotated[CurrentUser, Depends(require_viewer)]


@router.get("")
def provider_health(request: Request, _: Viewer) -> dict[str, dict[str, bool | str]]:
    """Expose provider capability flags without returning operational secrets."""
    settings: Settings = request.app.state.settings
    live_mode = settings.provider_mode == "live"
    youtube_configured = bool(settings.youtube_api_key)
    soundcloud_configured = bool(settings.yt_dlp_bin)
    ftm_configured = (
        bool(settings.scraper_user_agent)
        and settings.scraper_request_delay_ms > 0
    )
    return {
        "youtube": {
            "configured": youtube_configured,
            "enabled": live_mode and youtube_configured,
            "mode": "official_api",
            "runtime_mode": settings.provider_mode,
        },
        "soundcloud": {
            "configured": soundcloud_configured,
            "enabled": live_mode and soundcloud_configured,
            "mode": "manual_url",
            "runtime_mode": settings.provider_mode,
        },
        "freeteknomusic": {
            "configured": ftm_configured,
            "enabled": live_mode and settings.ftm_scraper_enabled,
            "mode": "robots_crawl",
            "runtime_mode": settings.provider_mode,
        },
    }
