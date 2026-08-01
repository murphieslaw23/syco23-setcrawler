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


ARCHIVE_BASE_URL = "https://archive.org"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_AUDIO_FORMATS = {
    "64kbps mp3",
    "128kbps mp3",
    "flac",
    "ogg vorbis",
    "vbr mp3",
    "wave",
}


def _archive_identifier(reference: str) -> str:
    if _IDENTIFIER.fullmatch(reference):
        return reference
    try:
        parsed = urlsplit(reference)
    except (TypeError, ValueError) as error:
        raise ProviderValidationError("archive_invalid_reference") from error
    parts = parsed.path.rstrip("/").split("/")
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in {"archive.org", "www.archive.org"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 3
        or parts[1] != "details"
        or _IDENTIFIER.fullmatch(parts[2]) is None
    ):
        raise ProviderValidationError("archive_invalid_reference")
    return parts[2]


def _creator(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        names = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return ", ".join(names) or None
    return None


class ArchiveOrgAdapter:
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
            raise ProviderValidationError("archive_operation_unsupported")
        query = request.parameters.get("query")
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise ProviderValidationError("archive_search_query_invalid")
        start = 0
        if request.cursor is not None:
            try:
                start = int(request.cursor)
            except ValueError as error:
                raise ProviderValidationError("archive_cursor_invalid") from error
            if start < 0:
                raise ProviderValidationError("archive_cursor_invalid")
        response = await provider_get(
            base_url=ARCHIVE_BASE_URL,
            path="/advancedsearch.php",
            transport=self.transport,
            timeout=self.timeout,
            error_code="archive_temporary_error",
            params={
                "q": query.strip(),
                "output": "json",
                "rows": request.limit,
                "page": start // request.limit + 1,
                "fl[]": [
                    "identifier",
                    "title",
                    "creator",
                    "date",
                    "licenseurl",
                ],
            },
        )
        payload = json_object(response, error_code="archive_invalid_response")
        result = payload.get("response")
        if not isinstance(result, dict) or not isinstance(result.get("docs"), list):
            raise ProviderPayloadError("archive_invalid_response")
        items = tuple(self._normalize_document(item) for item in result["docs"])
        total = result.get("numFound")
        next_cursor = None
        if isinstance(total, int) and start + len(items) < total:
            next_cursor = str(start + len(items))
        return DiscoveryPage(items=items, next_cursor=next_cursor)

    async def resolve_metadata(self, reference: str) -> ProviderItemPayload:
        identifier = _archive_identifier(reference)
        response = await provider_get(
            base_url=ARCHIVE_BASE_URL,
            path=f"/metadata/{quote(identifier, safe='')}",
            transport=self.transport,
            timeout=self.timeout,
            error_code="archive_temporary_error",
        )
        payload = json_object(response, error_code="archive_invalid_response")
        return self._normalize_metadata(payload, identifier=identifier)

    async def resolve_embed(self, reference: str) -> str:
        identifier = _archive_identifier(reference)
        return f"https://archive.org/embed/{quote(identifier, safe='')}"

    async def resolve_license_evidence(self, reference: str) -> dict[str, str]:
        item = await self.resolve_metadata(reference)
        if item.license_evidence is None:
            return {"policy": "no_explicit_permissive_license"}
        return {key: str(value) for key, value in item.license_evidence.items()}

    def _normalize_document(self, value: object) -> ProviderItemPayload:
        if not isinstance(value, dict):
            raise ProviderPayloadError("archive_invalid_response")
        identifier = value.get("identifier")
        if not isinstance(identifier, str) or _IDENTIFIER.fullmatch(identifier) is None:
            raise ProviderPayloadError("archive_invalid_response")
        return self._item(value, identifier=identifier)

    def _normalize_metadata(
        self,
        value: dict[str, Any],
        *,
        identifier: str,
    ) -> ProviderItemPayload:
        metadata = value.get("metadata")
        if not isinstance(metadata, dict):
            raise ProviderPayloadError("archive_invalid_response")
        metadata_identifier = metadata.get("identifier")
        if metadata_identifier is not None and metadata_identifier != identifier:
            raise ProviderPayloadError("archive_invalid_response")
        files = value.get("files", [])
        if not isinstance(files, list):
            raise ProviderPayloadError("archive_invalid_response")
        return self._item(metadata, identifier=identifier, files=files)

    def _item(
        self,
        metadata: dict[str, Any],
        *,
        identifier: str,
        files: list[object] | None = None,
    ) -> ProviderItemPayload:
        details_url = f"https://archive.org/details/{quote(identifier, safe='')}"
        resolved_license = permissive_license(metadata.get("licenseurl"))
        evidence = None
        candidates: list[AuthorizedAudioCandidate] = []
        if resolved_license is not None:
            license_name, license_url = resolved_license
            evidence = {
                "license_url": license_url,
                "policy": "explicit_permissive_license",
            }
            for file_value in files or []:
                if not isinstance(file_value, dict):
                    continue
                name = file_value.get("name")
                format_name = file_value.get("format")
                if (
                    not isinstance(name, str)
                    or not name
                    or "/" in name
                    or "\\" in name
                    or file_value.get("source") != "original"
                    or not isinstance(format_name, str)
                    or format_name.casefold() not in _AUDIO_FORMATS
                ):
                    continue
                candidates.append(
                    AuthorizedAudioCandidate(
                        provider_key="archive-org",
                        external_id=f"{identifier}:{name}",
                        source_url=(
                            f"https://archive.org/download/{quote(identifier, safe='')}/"
                            f"{quote(name, safe='')}"
                        ),
                        evidence_references=(license_url, details_url),
                        evidence={
                            "file": name,
                            "format": format_name,
                            "license": license_name,
                            "license_url": license_url,
                            "policy": "reference_only_no_fetch",
                        },
                    )
                )
        title = metadata.get("title")
        return ProviderItemPayload(
            provider_key="archive-org",
            external_id=identifier,
            canonical_url=details_url,
            title=title if isinstance(title, str) else None,
            published_at=optional_datetime(metadata.get("date")),
            creator_name=_creator(metadata.get("creator")),
            embed_url=f"https://archive.org/embed/{quote(identifier, safe='')}",
            download_candidates=tuple(candidates),
            raw_metadata=metadata,
            provenance={"source": "archive_org_metadata_api"},
            license_evidence=evidence,
        )
