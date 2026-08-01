from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.provider import (
    ProviderPayloadError,
    ProviderTemporaryError,
    ProviderValidationError,
)


_PERMISSIVE_LICENSES = {
    "cc-by-4.0": (
        "CC-BY-4.0",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
    "cc-by-sa-4.0": (
        "CC-BY-SA-4.0",
        "https://creativecommons.org/licenses/by-sa/4.0/",
    ),
    "cc0-1.0": (
        "CC0-1.0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
    ),
    "https://creativecommons.org/licenses/by/4.0/": (
        "CC-BY-4.0",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
    "https://creativecommons.org/licenses/by-sa/4.0/": (
        "CC-BY-SA-4.0",
        "https://creativecommons.org/licenses/by-sa/4.0/",
    ),
    "https://creativecommons.org/publicdomain/zero/1.0/": (
        "CC0-1.0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
    ),
    "https://creativecommons.org/publicdomain/mark/1.0/": (
        "PUBLIC-DOMAIN-1.0",
        "https://creativecommons.org/publicdomain/mark/1.0/",
    ),
}


def permissive_license(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    return _PERMISSIVE_LICENSES.get(value.strip().casefold())


def https_url(value: object, *, error_code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ProviderValidationError(error_code)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ProviderValidationError(error_code) from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ProviderValidationError(error_code)
    return value


def optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        if "," in candidate:
            return parsedate_to_datetime(candidate)
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def json_object(response: httpx.Response, *, error_code: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as error:
        raise ProviderPayloadError(error_code) from error
    if not isinstance(value, dict):
        raise ProviderPayloadError(error_code)
    return value


def raise_for_provider_status(response: httpx.Response, *, error_code: str) -> None:
    if response.status_code >= 500 or response.status_code == 429:
        raise ProviderTemporaryError(error_code)
    if response.status_code >= 400:
        raise ProviderPayloadError(error_code)


async def provider_get(
    *,
    base_url: str,
    path: str,
    transport: httpx.AsyncBaseTransport | None,
    timeout: float,
    error_code: str,
    params: dict[str, str | int | list[str]] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            response = await client.get(path, params=params, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as error:
        raise ProviderTemporaryError(error_code) from error
    raise_for_provider_status(response, error_code=error_code)
    return response
