import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.schemas.profile import SearchProfile
from app.services.normalizer import (
    RawSetPayload,
    normalize_raw_payload,
)
from app.services.provider import (
    ProviderPayloadError,
    ProviderQuotaError,
    ProviderTemporaryError,
)


YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
_DURATION = re.compile(
    r"P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?"
)


class YouTubeSearchBatch(BaseModel):
    payloads: list[RawSetPayload]
    next_page_token: str | None = None


def _duration_seconds(value: str) -> int:
    match = _DURATION.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        raise ProviderPayloadError("youtube_invalid_duration")
    total = (
        Decimal(match.group("days") or 0) * 86_400
        + Decimal(match.group("hours") or 0) * 3_600
        + Decimal(match.group("minutes") or 0) * 60
        + Decimal(match.group("seconds") or 0)
    )
    if total != total.to_integral_value():
        raise ProviderPayloadError("youtube_invalid_duration")
    return int(total)


def _video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host in {"youtube.com", "www.youtube.com"}:
        if parsed.path != "/watch":
            return None
        values = parse_qs(parsed.query).get("v", [])
        return values[0] if len(values) == 1 and values[0] else None
    if host == "youtu.be":
        value = parsed.path.removeprefix("/")
        if not value or "/" in value:
            return None
        return value
    return None


def _quota_exceeded(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    errors = error.get("errors", [])
    return any(
        isinstance(item, dict)
        and item.get("reason") == "quotaExceeded"
        for item in errors
    )


class YouTubeAdapter:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (
            settings.youtube_api_key
            if api_key is None
            else api_key
        )
        self.transport = transport
        self.timeout = (
            settings.provider_request_timeout_seconds
            if timeout is None
            else timeout
        )

    async def fetch(self, url: str) -> RawSetPayload:
        video_id = _video_id(url)
        if video_id is None:
            raise ProviderPayloadError("youtube_video_unavailable")
        payloads = await self._video_details([video_id])
        if not payloads:
            raise ProviderPayloadError("youtube_video_unavailable")
        return payloads[0]

    async def search(
        self,
        profile: SearchProfile,
    ) -> YouTubeSearchBatch:
        params: dict[str, str | int] = {
            "part": "snippet",
            "type": "video",
            "videoDuration": "long",
            "maxResults": 50,
            "q": profile.query,
            "key": self.api_key,
        }
        if profile.next_page_token:
            params["pageToken"] = profile.next_page_token
        response = await self._get("/search", params=params)
        data = self._response_object(response)
        items = data.get("items", [])
        if not isinstance(items, list):
            raise ProviderPayloadError("youtube_invalid_response")
        ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                raise ProviderPayloadError("youtube_invalid_response")
            identity = item.get("id")
            if (
                not isinstance(identity, dict)
                or not isinstance(identity.get("videoId"), str)
                or not identity["videoId"]
            ):
                raise ProviderPayloadError("youtube_invalid_response")
            ids.append(identity["videoId"])
        next_page_token = data.get("nextPageToken")
        if next_page_token is not None and not isinstance(
            next_page_token, str
        ):
            raise ProviderPayloadError("youtube_invalid_response")
        return YouTubeSearchBatch(
            payloads=await self._video_details(ids),
            next_page_token=next_page_token,
        )

    async def _video_details(
        self,
        video_ids: list[str],
    ) -> list[RawSetPayload]:
        items_by_id: dict[str, dict[str, Any]] = {}
        for start in range(0, len(video_ids), 50):
            batch = video_ids[start : start + 50]
            response = await self._get(
                "/videos",
                params={
                    "part": "snippet,contentDetails,status",
                    "id": ",".join(batch),
                    "key": self.api_key,
                },
            )
            data = self._response_object(response)
            items = data.get("items", [])
            if not isinstance(items, list):
                raise ProviderPayloadError("youtube_invalid_response")
            for item in items:
                if not isinstance(item, dict):
                    raise ProviderPayloadError(
                        "youtube_invalid_response"
                    )
                status = item.get("status")
                if not isinstance(status, dict):
                    raise ProviderPayloadError(
                        "youtube_invalid_response"
                    )
                video_id = item.get("id")
                if not isinstance(video_id, str) or not video_id:
                    raise ProviderPayloadError(
                        "youtube_invalid_response"
                    )
                privacy_status = status.get("privacyStatus")
                if privacy_status not in {
                    "public",
                    "private",
                    "unlisted",
                }:
                    raise ProviderPayloadError(
                        "youtube_invalid_response"
                    )
                if privacy_status == "public":
                    items_by_id[video_id] = item
        try:
            return [
                self._normalize_video(items_by_id[video_id])
                for video_id in video_ids
                if video_id in items_by_id
            ]
        except (
            AttributeError,
            TypeError,
            ValueError,
            ValidationError,
            ProviderPayloadError,
        ) as error:
            raise ProviderPayloadError(
                "youtube_invalid_response"
            ) from error

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int],
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=YOUTUBE_API_BASE_URL,
                transport=self.transport,
                timeout=self.timeout,
            ) as client:
                response = await client.get(path, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise ProviderTemporaryError(
                "youtube_temporary_error"
            ) from error
        if response.status_code == 403:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if _quota_exceeded(error_payload):
                raise ProviderQuotaError("youtube_quota_exceeded")
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderTemporaryError("youtube_temporary_error")
        if response.is_error:
            raise ProviderPayloadError("youtube_provider_error")
        return response

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderPayloadError(
                "youtube_invalid_response"
            ) from error
        if not isinstance(data, dict):
            raise ProviderPayloadError("youtube_invalid_response")
        return data

    @staticmethod
    def _normalize_video(item: dict[str, Any]) -> RawSetPayload:
        snippet = item.get("snippet")
        details = item.get("contentDetails")
        if not isinstance(snippet, dict) or not isinstance(
            details, dict
        ):
            raise ProviderPayloadError("youtube_invalid_response")
        title = snippet.get("title")
        published_at = snippet.get("publishedAt")
        thumbnails = snippet.get("thumbnails", {})
        duration = details.get("duration")
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(published_at, str)
            or not isinstance(thumbnails, dict)
            or not isinstance(duration, str)
        ):
            raise ProviderPayloadError("youtube_invalid_response")
        raw = {
            **item,
            "title": title,
            "description": snippet.get("description"),
            "published_at": published_at,
            "thumbnails": thumbnails,
            "duration_seconds": _duration_seconds(duration),
        }
        return normalize_raw_payload("youtube", raw)
