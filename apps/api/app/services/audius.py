from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import get_settings
from app.services.provider import ProviderPayloadError, ProviderValidationError
from app.services.provider_adapter_support import (
    json_object,
    optional_datetime,
    permissive_license,
    provider_get,
)
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    DiscoveryPage,
    DiscoveryRequest,
    ProviderItemPayload,
)


AUDIUS_API_BASE_URL = "https://api.audius.co"
_TRACK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _audius_reference(reference: str) -> tuple[str | None, str | None]:
    if _TRACK_ID.fullmatch(reference):
        return reference, None
    try:
        parsed = urlsplit(reference)
    except (TypeError, ValueError) as error:
        raise ProviderValidationError("audius_invalid_reference") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderValidationError("audius_invalid_reference")
    parts = parsed.path.strip("/").split("/")
    host = (parsed.hostname or "").casefold()
    if (
        host == "api.audius.co"
        and len(parts) == 3
        and parts[:2] == ["v1", "tracks"]
        and _TRACK_ID.fullmatch(parts[2]) is not None
    ):
        return parts[2], None
    if (
        host in {"audius.co", "www.audius.co"}
        and len(parts) == 2
        and all(_TRACK_ID.fullmatch(part) is not None for part in parts)
    ):
        return None, "/" + "/".join(parts)
    raise ProviderValidationError("audius_invalid_reference")


class AudiusAdapter:
    def __init__(
        self,
        *,
        bearer_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self.bearer_token = (
            settings.audius_api_bearer_token
            if bearer_token is None
            else bearer_token
        )
        self.transport = transport
        self.timeout = (
            settings.provider_request_timeout_seconds
            if timeout is None
            else timeout
        )

    async def discover(self, request: DiscoveryRequest) -> DiscoveryPage:
        if request.operation != "search":
            raise ProviderValidationError("audius_operation_unsupported")
        query = request.parameters.get("query")
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise ProviderValidationError("audius_search_query_invalid")
        offset = 0
        if request.cursor is not None:
            if not request.cursor.isdigit():
                raise ProviderValidationError("audius_cursor_invalid")
            offset = int(request.cursor)
        response = await provider_get(
            base_url=AUDIUS_API_BASE_URL,
            path="/v1/tracks/search",
            transport=self.transport,
            timeout=self.timeout,
            error_code="audius_temporary_error",
            params={"query": query.strip(), "limit": request.limit, "offset": offset},
            headers=self._headers(),
        )
        payload = json_object(response, error_code="audius_invalid_response")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderPayloadError("audius_invalid_response")
        items = tuple(self._item(value) for value in data)
        next_cursor = str(offset + len(items)) if len(items) == request.limit else None
        return DiscoveryPage(items=items, next_cursor=next_cursor)

    async def resolve_metadata(self, reference: str) -> ProviderItemPayload:
        track_id, permalink = _audius_reference(reference)
        path = (
            f"/v1/tracks/{quote(track_id, safe='')}"
            if track_id is not None
            else "/v1/tracks"
        )
        response = await provider_get(
            base_url=AUDIUS_API_BASE_URL,
            path=path,
            transport=self.transport,
            timeout=self.timeout,
            error_code="audius_temporary_error",
            params={"permalink[]": permalink} if permalink is not None else None,
            headers=self._headers(),
        )
        payload = json_object(response, error_code="audius_invalid_response")
        data = payload.get("data")
        if permalink is not None:
            if not isinstance(data, list) or len(data) != 1:
                raise ProviderPayloadError("audius_invalid_response")
            data = data[0]
        return self._item(data, expected_id=track_id)

    async def resolve_embed(self, reference: str) -> str:
        track_id, _ = _audius_reference(reference)
        if track_id is None:
            item = await self.resolve_metadata(reference)
            if item.embed_url is None:
                raise ProviderPayloadError("audius_invalid_response")
            return str(item.embed_url)
        return f"https://audius.co/embed/track/{quote(track_id, safe='')}?flavor=card"

    async def resolve_license_evidence(self, reference: str) -> dict[str, str]:
        item = await self.resolve_metadata(reference)
        if item.license_evidence is None:
            return {"policy": "no_explicit_permissive_license"}
        return {key: str(value) for key, value in item.license_evidence.items()}

    def _headers(self) -> dict[str, str] | None:
        if not self.bearer_token:
            return None
        return {"Authorization": f"Bearer {self.bearer_token}"}

    def _item(
        self,
        value: object,
        *,
        expected_id: str | None = None,
    ) -> ProviderItemPayload:
        if not isinstance(value, dict):
            raise ProviderPayloadError("audius_invalid_response")
        track_id = value.get("id")
        if (
            not isinstance(track_id, str)
            or _TRACK_ID.fullmatch(track_id) is None
            or (expected_id is not None and track_id != expected_id)
        ):
            raise ProviderPayloadError("audius_invalid_response")
        permalink = value.get("permalink")
        if not isinstance(permalink, str) or not permalink.startswith("/"):
            raise ProviderPayloadError("audius_invalid_response")
        canonical = f"https://audius.co{permalink}"
        resolved_license = permissive_license(value.get("license"))
        evidence = None
        candidates: tuple[AuthorizedAudioCandidate, ...] = ()
        if resolved_license is not None:
            license_name, license_url = resolved_license
            evidence = {
                "license": license_name,
                "license_url": license_url,
                "policy": "explicit_permissive_license",
            }
            downloadable = (
                value.get("is_downloadable") is True
                or value.get("downloadable") is True
            )
            if downloadable and value.get("download_conditions") is None:
                candidates = (
                    AuthorizedAudioCandidate(
                        provider_key="audius",
                        external_id=track_id,
                        source_url=(
                            f"https://api.audius.co/v1/tracks/"
                            f"{quote(track_id, safe='')}/download"
                        ),
                        evidence_references=(canonical, license_url),
                        evidence={
                            "downloadable": True,
                            "download_conditions": None,
                            "license": license_name,
                            "license_url": license_url,
                            "policy": "reference_only_no_fetch",
                        },
                    ),
                )
        user = value.get("user")
        creator = user.get("name") if isinstance(user, dict) else None
        artwork = value.get("artwork")
        artwork_candidates = ()
        if isinstance(artwork, dict):
            image = artwork.get("480x480") or artwork.get("_480x480")
            if isinstance(image, str):
                artwork_candidates = (image,)
        title = value.get("title")
        duration = value.get("duration")
        return ProviderItemPayload(
            provider_key="audius",
            external_id=track_id,
            canonical_url=canonical,
            title=title if isinstance(title, str) else None,
            published_at=optional_datetime(
                value.get("release_date") or value.get("releaseDate")
            ),
            duration_seconds=duration if isinstance(duration, int) else None,
            creator_name=creator if isinstance(creator, str) else None,
            embed_url=f"https://audius.co/embed/track/{track_id}?flavor=card",
            artwork_candidates=artwork_candidates,
            download_candidates=candidates,
            raw_metadata=value,
            provenance={"source": "audius_api"},
            license_evidence=evidence,
        )
