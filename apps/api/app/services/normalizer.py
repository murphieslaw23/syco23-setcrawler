import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.set import SetSource


class RawSetPayload(BaseModel):
    source: SetSource
    source_id: str
    canonical_url: str
    title: str
    description: str | None = None
    duration_seconds: int | None = None
    published_at: datetime | None = None
    primary_image_url: str | None = None
    raw_payload: dict[str, Any]


def _best_thumbnail(thumbnails: dict[str, Any]) -> str | None:
    for tier in ("maxres", "standard", "high", "medium", "default"):
        value = thumbnails.get(tier)
        if isinstance(value, dict) and value.get("url"):
            return str(value["url"])
    return None


def normalize_raw_payload(source: str, raw: dict[str, Any]) -> RawSetPayload:
    provider = SetSource(source)
    source_id = str(raw.get("id") or raw.get("source_id") or "")
    if not source_id:
        raise ValueError("Provider payload is missing an id")
    if provider == SetSource.youtube:
        canonical_url = f"https://www.youtube.com/watch?v={source_id}"
        image = _best_thumbnail(raw.get("thumbnails", {}))
    else:
        canonical_url = str(raw.get("webpage_url") or raw.get("canonical_url") or "")
        image = raw.get("thumbnail")
    duration = raw.get("duration_seconds")
    if duration is None:
        duration = raw.get("duration")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "Provider payload has invalid duration"
            ) from error
    published_at = raw.get("published_at")
    if published_at is None and provider == SetSource.soundcloud:
        published_at = raw.get("timestamp")
    return RawSetPayload(
        source=provider,
        source_id=source_id,
        canonical_url=canonical_url,
        title=str(raw.get("title") or "Untitled set"),
        description=raw.get("description"),
        duration_seconds=duration,
        published_at=published_at,
        primary_image_url=image,
        raw_payload=raw,
    )


def duplicate_fingerprint(title: str, duration_seconds: int) -> str:
    normalized = unicodedata.normalize("NFKD", title.casefold())
    normalized = normalized.replace("@", " at ").replace("—", " ").replace("-", " ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = " ".join(normalized.split())
    duration_bucket = round(duration_seconds / 60)
    return hashlib.sha256(f"{normalized}:{duration_bucket}".encode()).hexdigest()
