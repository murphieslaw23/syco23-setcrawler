from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, field_validator

from app.core.config import get_settings
from app.services.provider import ProviderPayloadError, ProviderValidationError
from app.services.provider_adapter_support import (
    https_url,
    optional_datetime,
    permissive_license,
    raise_for_provider_status,
)
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    DiscoveryPage,
    DiscoveryRequest,
    ProviderItemPayload,
)


_MAX_FEED_BYTES = 1_048_576
_ATOM = "{http://www.w3.org/2005/Atom}"


class TrustedFeed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_url: AnyHttpUrl
    license: str

    @field_validator("license")
    @classmethod
    def validate_license(cls, value: str) -> str:
        if permissive_license(value) is None:
            raise ValueError("trusted feed license must be explicitly permissive")
        return value


class RSSAdapter:
    def __init__(
        self,
        *,
        trusted_feeds: dict[str, TrustedFeed] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        validated: dict[str, TrustedFeed] = {}
        for url, trust in (trusted_feeds or {}).items():
            validated[https_url(url, error_code="rss_feed_url_invalid")] = trust
        self.trusted_feeds = MappingProxyType(validated)
        self.transport = transport
        self.timeout = (
            get_settings().provider_request_timeout_seconds
            if timeout is None
            else timeout
        )

    async def discover(self, request: DiscoveryRequest) -> DiscoveryPage:
        if request.operation != "feed":
            raise ProviderValidationError("rss_operation_unsupported")
        feed_url = request.parameters.get("feed_url")
        if not isinstance(feed_url, str) or feed_url not in self.trusted_feeds:
            raise ProviderValidationError("rss_feed_not_trusted")
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", feed_url) as response:
                    raise_for_provider_status(
                        response,
                        error_code="rss_temporary_error",
                    )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_FEED_BYTES:
                            raise ProviderPayloadError("rss_feed_too_large")
                        chunks.append(chunk)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            from app.services.provider import ProviderTemporaryError

            raise ProviderTemporaryError("rss_temporary_error") from error
        return self.parse_feed(
            b"".join(chunks),
            feed_url=feed_url,
            trust=self.trusted_feeds[feed_url],
            limit=request.limit,
        )

    async def syndicate(self, request: DiscoveryRequest) -> DiscoveryPage:
        return await self.discover(request)

    def parse_feed(
        self,
        content: bytes,
        *,
        feed_url: str,
        trust: TrustedFeed | None = None,
        limit: int = 100,
    ) -> DiscoveryPage:
        https_url(feed_url, error_code="rss_feed_url_invalid")
        if len(content) > _MAX_FEED_BYTES:
            raise ProviderPayloadError("rss_feed_too_large")
        upper = content.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ProviderPayloadError("rss_unsafe_xml")
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as error:
            raise ProviderPayloadError("rss_invalid_feed") from error
        if root.tag == "rss":
            items = [
                self._rss_item(node, feed_url=feed_url, trust=trust)
                for node in root.findall("./channel/item")[:limit]
            ]
        elif root.tag == f"{_ATOM}feed":
            items = [
                self._atom_item(node, feed_url=feed_url, trust=trust)
                for node in root.findall(f"{_ATOM}entry")[:limit]
            ]
        else:
            raise ProviderPayloadError("rss_invalid_feed")
        return DiscoveryPage(items=tuple(items))

    def _rss_item(
        self,
        node: ElementTree.Element,
        *,
        feed_url: str,
        trust: TrustedFeed | None,
    ) -> ProviderItemPayload:
        link = self._absolute_url(feed_url, node.findtext("link"))
        guid = (node.findtext("guid") or link).strip()
        enclosure = node.find("enclosure")
        enclosure_data = self._enclosure(enclosure, feed_url=feed_url)
        return self._item(
            external_id=guid,
            canonical_url=link,
            title=node.findtext("title"),
            creator=node.findtext("author"),
            published_at=optional_datetime(node.findtext("pubDate")),
            enclosure=enclosure_data,
            feed_url=feed_url,
            trust=trust,
        )

    def _atom_item(
        self,
        node: ElementTree.Element,
        *,
        feed_url: str,
        trust: TrustedFeed | None,
    ) -> ProviderItemPayload:
        alternate = None
        enclosure_node = None
        for link in node.findall(f"{_ATOM}link"):
            relation = link.attrib.get("rel", "alternate")
            if relation == "alternate" and alternate is None:
                alternate = link.attrib.get("href")
            elif relation == "enclosure" and enclosure_node is None:
                enclosure_node = link
        canonical = self._absolute_url(feed_url, alternate)
        external_id = (node.findtext(f"{_ATOM}id") or canonical).strip()
        author = node.find(f"{_ATOM}author/{_ATOM}name")
        return self._item(
            external_id=external_id,
            canonical_url=canonical,
            title=node.findtext(f"{_ATOM}title"),
            creator=author.text if author is not None else None,
            published_at=optional_datetime(
                node.findtext(f"{_ATOM}published")
                or node.findtext(f"{_ATOM}updated")
            ),
            enclosure=self._enclosure(enclosure_node, feed_url=feed_url),
            feed_url=feed_url,
            trust=trust,
        )

    def _item(
        self,
        *,
        external_id: str,
        canonical_url: str,
        title: str | None,
        creator: str | None,
        published_at: datetime | None,
        enclosure: dict[str, str] | None,
        feed_url: str,
        trust: TrustedFeed | None,
    ) -> ProviderItemPayload:
        evidence = None
        candidates: tuple[AuthorizedAudioCandidate, ...] = ()
        if trust is not None:
            resolved = permissive_license(trust.license)
            if resolved is None:
                raise ProviderPayloadError("rss_trust_invalid")
            license_name, license_url = resolved
            evidence = {
                "evidence_url": str(trust.evidence_url),
                "license": license_name,
                "license_url": license_url,
                "policy": "trusted_feed_explicit_license",
            }
            if enclosure is not None and enclosure.get("type", "").startswith("audio/"):
                candidates = (
                    AuthorizedAudioCandidate(
                        provider_key="rss",
                        external_id=external_id,
                        source_url=enclosure["url"],
                        evidence_references=(str(trust.evidence_url), license_url),
                        evidence={
                            **evidence,
                            "policy": "reference_only_no_fetch",
                        },
                    ),
                )
        return ProviderItemPayload(
            provider_key="rss",
            external_id=external_id,
            canonical_url=canonical_url,
            title=title.strip() if isinstance(title, str) and title.strip() else None,
            creator_name=(
                creator.strip()
                if isinstance(creator, str) and creator.strip()
                else None
            ),
            published_at=published_at,
            download_candidates=candidates,
            raw_metadata={"enclosure": enclosure} if enclosure is not None else {},
            provenance={"feed_url": feed_url, "trusted": trust is not None},
            license_evidence=evidence,
        )

    @staticmethod
    def _absolute_url(feed_url: str, value: str | None) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProviderPayloadError("rss_item_url_missing")
        return https_url(
            urljoin(feed_url, value.strip()),
            error_code="rss_item_url_invalid",
        )

    @classmethod
    def _enclosure(
        cls,
        node: ElementTree.Element | None,
        *,
        feed_url: str,
    ) -> dict[str, str] | None:
        if node is None:
            return None
        raw_url = node.attrib.get("url") or node.attrib.get("href")
        if raw_url is None:
            return None
        data = {
            "url": cls._absolute_url(feed_url, raw_url),
            "type": node.attrib.get("type", "application/octet-stream"),
        }
        length = node.attrib.get("length")
        if length is not None:
            data["length"] = length
        return data
