from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from app.core.config import get_settings
from app.services.provider import ProviderPayloadError, ProviderValidationError
from app.services.provider_adapter_support import json_object, optional_datetime, provider_get
from app.services.provider_contracts import DiscoveryPage, DiscoveryRequest, ProviderItemPayload


MIXCLOUD_API_BASE_URL = "https://api.mixcloud.com"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _mixcloud_parts(reference: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(reference)
    except (TypeError, ValueError) as error:
        raise ProviderValidationError("mixcloud_invalid_reference") from error
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold()
        not in {"mixcloud.com", "www.mixcloud.com", "api.mixcloud.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(_SLUG.fullmatch(part) is None for part in parts)
    ):
        raise ProviderValidationError("mixcloud_invalid_reference")
    return parts[0], parts[1]


class MixcloudAdapter:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        self.transport = transport
        self.timeout = (
            get_settings().provider_request_timeout_seconds
            if timeout is None
            else timeout
        )

    async def discover(self, request: DiscoveryRequest) -> DiscoveryPage:
        if request.operation != "search":
            raise ProviderValidationError("mixcloud_operation_unsupported")
        query = request.parameters.get("query")
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise ProviderValidationError("mixcloud_search_query_invalid")
        offset = self._cursor(request.cursor)
        response = await provider_get(
            base_url=MIXCLOUD_API_BASE_URL,
            path="/search/",
            transport=self.transport,
            timeout=self.timeout,
            error_code="mixcloud_temporary_error",
            params={
                "q": query.strip(),
                "type": "cloudcast",
                "limit": request.limit,
                "offset": offset,
            },
        )
        return self._page(json_object(response, error_code="mixcloud_invalid_response"))

    async def syndicate(self, request: DiscoveryRequest) -> DiscoveryPage:
        if request.operation != "user-cloudcasts":
            raise ProviderValidationError("mixcloud_operation_unsupported")
        user = request.parameters.get("user")
        if not isinstance(user, str) or _SLUG.fullmatch(user) is None:
            raise ProviderValidationError("mixcloud_user_invalid")
        response = await provider_get(
            base_url=MIXCLOUD_API_BASE_URL,
            path=f"/{quote(user, safe='')}/cloudcasts/",
            transport=self.transport,
            timeout=self.timeout,
            error_code="mixcloud_temporary_error",
            params={"limit": request.limit, "offset": self._cursor(request.cursor)},
        )
        return self._page(json_object(response, error_code="mixcloud_invalid_response"))

    async def resolve_metadata(self, reference: str) -> ProviderItemPayload:
        user, slug = _mixcloud_parts(reference)
        response = await provider_get(
            base_url=MIXCLOUD_API_BASE_URL,
            path=f"/{quote(user, safe='')}/{quote(slug, safe='')}/",
            transport=self.transport,
            timeout=self.timeout,
            error_code="mixcloud_temporary_error",
        )
        return self._item(json_object(response, error_code="mixcloud_invalid_response"))

    async def resolve_embed(self, reference: str) -> str:
        user, slug = _mixcloud_parts(reference)
        canonical = f"https://www.mixcloud.com/{user}/{slug}/"
        return "https://www.mixcloud.com/widget/iframe/?" + urlencode(
            {"hide_cover": "1", "feed": canonical}
        )

    def _page(self, payload: dict[str, Any]) -> DiscoveryPage:
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderPayloadError("mixcloud_invalid_response")
        next_cursor = None
        paging = payload.get("paging")
        if isinstance(paging, dict) and isinstance(paging.get("next"), str):
            next_url = urlsplit(paging["next"])
            if (next_url.hostname or "").casefold() != "api.mixcloud.com":
                raise ProviderPayloadError("mixcloud_invalid_response")
            offsets = parse_qs(next_url.query).get("offset", [])
            if len(offsets) == 1 and offsets[0].isdigit():
                next_cursor = offsets[0]
        return DiscoveryPage(
            items=tuple(self._item(value) for value in data),
            next_cursor=next_cursor,
        )

    def _item(self, value: object) -> ProviderItemPayload:
        if not isinstance(value, dict):
            raise ProviderPayloadError("mixcloud_invalid_response")
        url = value.get("url")
        key = value.get("key")
        if not isinstance(url, str) or not isinstance(key, str):
            raise ProviderPayloadError("mixcloud_invalid_response")
        user, slug = _mixcloud_parts(url)
        expected_key = f"/{user}/{slug}/"
        if key != expected_key:
            raise ProviderPayloadError("mixcloud_invalid_response")
        creator = value.get("user")
        creator_name = creator.get("name") if isinstance(creator, dict) else None
        pictures = value.get("pictures")
        artwork = ()
        if isinstance(pictures, dict) and isinstance(pictures.get("large"), str):
            artwork = (pictures["large"],)
        duration = value.get("audio_length")
        title = value.get("name")
        return ProviderItemPayload(
            provider_key="mixcloud",
            external_id=f"{user}/{slug}",
            canonical_url=f"https://www.mixcloud.com/{user}/{slug}/",
            title=title if isinstance(title, str) else None,
            published_at=optional_datetime(value.get("created_time")),
            duration_seconds=duration if isinstance(duration, int) else None,
            creator_name=creator_name if isinstance(creator_name, str) else None,
            embed_url="https://www.mixcloud.com/widget/iframe/?"
            + urlencode(
                {
                    "hide_cover": "1",
                    "feed": f"https://www.mixcloud.com/{user}/{slug}/",
                }
            ),
            artwork_candidates=artwork,
            raw_metadata=value,
            provenance={
                "source": "mixcloud_api",
                "audio_stream_api_available": False,
            },
        )

    @staticmethod
    def _cursor(value: str | None) -> int:
        if value is None:
            return 0
        if not value.isdigit():
            raise ProviderValidationError("mixcloud_cursor_invalid")
        return int(value)
