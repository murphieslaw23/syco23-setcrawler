from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.services.normalizer import RawSetPayload
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    DiscoveryPage,
    DiscoveryRequest,
    ProviderCapability,
    ProviderDescriptor,
    ProviderItemPayload,
    ProviderWorkload,
)
from app.services.provider_registry import ProviderRegistry


class ProviderAdapter(Protocol):
    async def fetch(self, url: str) -> RawSetPayload: ...


class ProviderError(RuntimeError):
    code = "provider_error"
    retryable = False


class ProviderValidationError(ProviderError):
    code = "provider_validation"


class ProviderBlockedError(ProviderError):
    code = "provider_blocked"


class ProviderQuotaError(ProviderError):
    code = "provider_quota"


class ProviderTemporaryError(ProviderError):
    code = "provider_temporary"
    retryable = True


class ProviderPayloadError(ProviderError):
    code = "provider_payload"


def _item_payload(
    provider_key: str,
    payload: RawSetPayload,
    *,
    embed_url: str | None = None,
    license_evidence: dict[str, Any] | None = None,
) -> ProviderItemPayload:
    artwork = (payload.primary_image_url,) if payload.primary_image_url else ()
    creator = payload.raw_payload.get("channelTitle")
    if creator is None:
        creator = payload.raw_payload.get("uploader")
    provenance: dict[str, Any] = {"legacy_source": payload.source.value}
    download_candidates: tuple[AuthorizedAudioCandidate, ...] = ()
    if provider_key == "soundcloud":
        from urllib.parse import urlsplit

        from app.services.provider_adapter_support import permissive_license

        official_download = payload.raw_payload.get("downloadable") is True
        provenance["official_download_available"] = official_download
        download_url = payload.raw_payload.get("download_url")
        resolved_license = permissive_license(payload.raw_payload.get("license"))
        if (
            official_download
            and isinstance(download_url, str)
            and resolved_license is not None
        ):
            parsed_download = urlsplit(download_url)
            if (
                parsed_download.scheme.casefold() == "https"
                and (parsed_download.hostname or "").casefold()
                == "api.soundcloud.com"
                and parsed_download.username is None
                and parsed_download.password is None
                and parsed_download.port is None
                and not parsed_download.fragment
            ):
                license_name, license_url = resolved_license
                download_candidates = (
                    AuthorizedAudioCandidate(
                        provider_key="soundcloud",
                        external_id=payload.source_id,
                        source_url=download_url,
                        evidence_references=(payload.canonical_url, license_url),
                        evidence={
                            "license": license_name,
                            "license_url": license_url,
                            "official_download": True,
                            "policy": "reference_only_no_fetch",
                        },
                    ),
                )
                if license_evidence is None:
                    license_evidence = {
                        "license": license_name,
                        "license_url": license_url,
                        "policy": "explicit_permissive_license",
                    }
    return ProviderItemPayload(
        provider_key=provider_key,
        external_id=payload.source_id,
        canonical_url=payload.canonical_url,
        title=payload.title,
        published_at=payload.published_at,
        duration_seconds=payload.duration_seconds,
        creator_name=str(creator) if creator else None,
        embed_url=embed_url,
        artwork_candidates=artwork,
        download_candidates=download_candidates,
        raw_metadata=payload.raw_payload,
        provenance=provenance,
        license_evidence=license_evidence,
    )


class _YouTubeRegistryAdapter:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    async def fetch(self, reference: str) -> RawSetPayload:
        return await self._delegate.fetch(reference)

    async def search(self, profile: object) -> object:
        return await self._delegate.search(profile)

    async def discover(self, request: DiscoveryRequest) -> DiscoveryPage:
        from app.schemas.profile import SearchProfile

        query = request.parameters.get("query")
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise ProviderValidationError("youtube_search_query_invalid")
        profile = SearchProfile(
            name="Registry discovery",
            query=query.strip(),
            next_page_token=request.cursor,
        )
        batch = await self.search(profile)
        return DiscoveryPage(
            items=tuple(_item_payload("youtube", payload) for payload in batch.payloads),
            next_cursor=batch.next_page_token,
        )

    async def resolve_metadata(self, reference: str) -> ProviderItemPayload:
        payload = await self.fetch(reference)
        return _item_payload(
            "youtube",
            payload,
            embed_url=await self.resolve_embed(reference),
        )

    async def resolve_embed(self, reference: str) -> str:
        from app.services.youtube import _video_id

        video_id = _video_id(reference)
        if video_id is None:
            raise ProviderValidationError("youtube_invalid_url")
        return f"https://www.youtube.com/embed/{video_id}"


class _SoundCloudRegistryAdapter:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    async def fetch(self, reference: str) -> RawSetPayload:
        return await self._delegate.fetch(reference)

    async def resolve_metadata(self, reference: str) -> ProviderItemPayload:
        payload = await self.fetch(reference)
        return _item_payload(
            "soundcloud",
            payload,
            embed_url=await self.resolve_embed(reference),
        )

    async def resolve_embed(self, reference: str) -> str:
        from app.services.soundcloud import validate_soundcloud_url

        return validate_soundcloud_url(reference)


class _FTMRegistryAdapter:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    async def fetch(self, reference: str) -> RawSetPayload:
        return await self._delegate.fetch(reference)

    async def crawl(self, start_url: str, *, max_pages: int | None = None) -> list[RawSetPayload]:
        return await self._delegate.crawl(start_url, max_pages=max_pages)

    async def discover(self, request: DiscoveryRequest) -> DiscoveryPage:
        start_url = request.parameters.get("start_url")
        if not isinstance(start_url, str):
            raise ProviderValidationError("ftm_start_url_invalid")
        payloads = await self.crawl(start_url, max_pages=request.limit)
        return DiscoveryPage(
            items=tuple(
                _item_payload(
                    "ftm",
                    payload,
                    license_evidence={
                        "source": payload.canonical_url,
                        "policy": "robots-aware metadata crawl",
                    },
                )
                for payload in payloads
            )
        )

    async def resolve_metadata(self, reference: str) -> ProviderItemPayload:
        payload = await self.fetch(reference)
        return _item_payload(
            "ftm",
            payload,
            license_evidence=await self.resolve_license_evidence(reference),
        )

    async def resolve_license_evidence(self, reference: str) -> dict[str, str]:
        from app.services.ftm import validate_ftm_url

        return {
            "source": validate_ftm_url(reference),
            "policy": "robots-aware metadata crawl",
        }


def build_provider_descriptors(settings: Settings) -> tuple[ProviderDescriptor, ...]:
    """Build behavior, workload, and task descriptors for current providers."""

    def youtube_factory() -> object:
        from app.services.youtube import YouTubeAdapter

        return _YouTubeRegistryAdapter(
            YouTubeAdapter(
                api_key=settings.youtube_api_key,
                timeout=settings.provider_request_timeout_seconds,
            )
        )

    def soundcloud_factory() -> object:
        from app.services.soundcloud import SoundCloudAdapter

        return _SoundCloudRegistryAdapter(
            SoundCloudAdapter(
                yt_dlp_bin=settings.yt_dlp_bin,
                output_limit_bytes=settings.provider_output_limit_bytes,
            )
        )

    def ftm_factory() -> object:
        from app.services.ftm import FTMAdapter

        return _FTMRegistryAdapter(
            FTMAdapter(
                enabled=settings.ftm_scraper_enabled,
                scraper_user_agent=settings.scraper_user_agent,
                scraper_request_delay_ms=settings.scraper_request_delay_ms,
                ftm_max_pages_per_run=settings.ftm_max_pages_per_run,
                timeout=settings.provider_request_timeout_seconds,
            )
        )

    def archive_factory() -> object:
        from app.services.archive_org import ArchiveOrgAdapter

        return ArchiveOrgAdapter(
            timeout=settings.provider_request_timeout_seconds,
        )

    def mixcloud_factory() -> object:
        from app.services.mixcloud import MixcloudAdapter

        return MixcloudAdapter(
            timeout=settings.provider_request_timeout_seconds,
        )

    def audius_factory() -> object:
        from app.services.audius import AudiusAdapter

        return AudiusAdapter(
            bearer_token=settings.audius_api_bearer_token,
            timeout=settings.provider_request_timeout_seconds,
        )

    def rss_factory() -> object:
        from app.services.rss import RSSAdapter, TrustedFeed

        raw = settings.rss_trusted_feeds_json
        values = json.loads(raw) if raw else {}
        if not isinstance(values, dict):
            raise ValueError("RSS_TRUSTED_FEEDS_JSON must be an object")
        feeds = {
            str(url): TrustedFeed.model_validate(trust)
            for url, trust in values.items()
        }
        return RSSAdapter(
            trusted_feeds=feeds,
            timeout=settings.provider_request_timeout_seconds,
        )

    return (
        ProviderDescriptor(
            key="youtube",
            display_name="YouTube",
            capabilities=frozenset(
                {
                    ProviderCapability.discovery,
                    ProviderCapability.metadata,
                    ProviderCapability.embed,
                }
            ),
            workload_by_capability={
                ProviderCapability.discovery: ProviderWorkload.provider_api,
                ProviderCapability.metadata: ProviderWorkload.provider_api,
                ProviderCapability.embed: ProviderWorkload.provider_api,
            },
            task_by_capability={
                ProviderCapability.discovery: "app.workers.youtube_poller.poll_profile",
                ProviderCapability.metadata: "app.workers.youtube_poller.import_url",
            },
            adapter_factory=youtube_factory,
            url_matchers=(
                re.compile(
                    r"^https://(?:www\.)?youtube\.com/watch\?(?:[^#]*&)?v=[A-Za-z0-9_-]+(?:&[^#]*)?$"
                ),
                re.compile(r"^https://youtu\.be/[A-Za-z0-9_-]+$"),
            ),
            required_settings=("YOUTUBE_API_KEY",),
            discovery_operations={"search": frozenset({"query"})},
            health_mode="official_api",
        ),
        ProviderDescriptor(
            key="soundcloud",
            display_name="SoundCloud",
            capabilities=frozenset(
                {
                    ProviderCapability.metadata,
                    ProviderCapability.embed,
                }
            ),
            workload_by_capability={
                ProviderCapability.metadata: ProviderWorkload.provider_scrape,
                ProviderCapability.embed: ProviderWorkload.provider_scrape,
            },
            task_by_capability={
                ProviderCapability.metadata: "app.workers.soundcloud_importer.import_url",
            },
            adapter_factory=soundcloud_factory,
            url_matchers=(
                re.compile(
                    r"^https://(?:www\.)?soundcloud\.com/[^/?#]+/[^/?#]+(?:\?[^#]*)?$"
                ),
            ),
            required_settings=("YT_DLP_BIN", "PROVIDER_OUTPUT_LIMIT_BYTES"),
            health_mode="manual_url",
        ),
        ProviderDescriptor(
            key="ftm",
            display_name="freeteknomusic.org",
            capabilities=frozenset(
                {
                    ProviderCapability.discovery,
                    ProviderCapability.metadata,
                    ProviderCapability.license_evidence,
                }
            ),
            workload_by_capability={
                ProviderCapability.discovery: ProviderWorkload.provider_scrape,
                ProviderCapability.metadata: ProviderWorkload.provider_scrape,
                ProviderCapability.license_evidence: ProviderWorkload.provider_scrape,
            },
            task_by_capability={
                ProviderCapability.discovery: "app.workers.ftm_scraper.crawl_profile",
                ProviderCapability.metadata: "app.workers.ftm_scraper.import_url",
            },
            adapter_factory=ftm_factory,
            url_matchers=(
                re.compile(
                    r"^https://(?:www\.)?freeteknomusic\.org/sets/[A-Za-z0-9][A-Za-z0-9_-]*$"
                ),
            ),
            required_settings=(
                "SCRAPER_USER_AGENT",
                "SCRAPER_REQUEST_DELAY_MS",
                "FTM_MAX_PAGES_PER_RUN",
            ),
            enabled_by_default=False,
            enabled_setting="FTM_SCRAPER_ENABLED",
            discovery_operations={"crawl": frozenset({"start_url"})},
            public_health_key="freeteknomusic",
            health_mode="robots_crawl",
        ),
        ProviderDescriptor(
            key="archive-org",
            display_name="Internet Archive",
            capabilities=frozenset(
                {
                    ProviderCapability.discovery,
                    ProviderCapability.metadata,
                    ProviderCapability.embed,
                    ProviderCapability.license_evidence,
                }
            ),
            workload_by_capability={
                ProviderCapability.discovery: ProviderWorkload.provider_api,
                ProviderCapability.metadata: ProviderWorkload.provider_api,
                ProviderCapability.embed: ProviderWorkload.provider_api,
                ProviderCapability.license_evidence: (
                    ProviderWorkload.provider_api
                ),
            },
            task_by_capability={
                ProviderCapability.discovery: (
                    "app.workers.provider_discovery.discover_profile"
                ),
            },
            adapter_factory=archive_factory,
            url_matchers=(
                re.compile(
                    r"^https://(?:www\.)?archive\.org/details/"
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}/?$"
                ),
            ),
            enabled_by_default=False,
            enabled_setting="ARCHIVE_ORG_ENABLED",
            discovery_operations={"search": frozenset({"query"})},
            health_mode="official_api",
        ),
        ProviderDescriptor(
            key="mixcloud",
            display_name="Mixcloud",
            capabilities=frozenset(
                {
                    ProviderCapability.discovery,
                    ProviderCapability.metadata,
                    ProviderCapability.embed,
                    ProviderCapability.syndication,
                }
            ),
            workload_by_capability={
                ProviderCapability.discovery: ProviderWorkload.provider_api,
                ProviderCapability.metadata: ProviderWorkload.provider_api,
                ProviderCapability.embed: ProviderWorkload.provider_api,
                ProviderCapability.syndication: (
                    ProviderWorkload.provider_api
                ),
            },
            task_by_capability={
                ProviderCapability.discovery: (
                    "app.workers.provider_discovery.discover_profile"
                ),
            },
            adapter_factory=mixcloud_factory,
            url_matchers=(
                re.compile(
                    r"^https://(?:www\.)?mixcloud\.com/"
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}/"
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}/?$"
                ),
            ),
            enabled_by_default=False,
            enabled_setting="MIXCLOUD_ENABLED",
            discovery_operations={
                "search": frozenset({"query"}),
                "user-cloudcasts": frozenset({"user"}),
            },
            health_mode="official_api",
        ),
        ProviderDescriptor(
            key="audius",
            display_name="Audius",
            capabilities=frozenset(
                {
                    ProviderCapability.discovery,
                    ProviderCapability.metadata,
                    ProviderCapability.embed,
                    ProviderCapability.license_evidence,
                }
            ),
            workload_by_capability={
                ProviderCapability.discovery: ProviderWorkload.provider_api,
                ProviderCapability.metadata: ProviderWorkload.provider_api,
                ProviderCapability.embed: ProviderWorkload.provider_api,
                ProviderCapability.license_evidence: (
                    ProviderWorkload.provider_api
                ),
            },
            task_by_capability={
                ProviderCapability.discovery: (
                    "app.workers.provider_discovery.discover_profile"
                ),
            },
            adapter_factory=audius_factory,
            url_matchers=(
                re.compile(
                    r"^https://api\.audius\.co/v1/tracks/"
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
                ),
                re.compile(
                    r"^https://(?:www\.)?audius\.co/"
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}/"
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}/?$"
                ),
            ),
            required_settings=("AUDIUS_API_BEARER_TOKEN",),
            enabled_by_default=False,
            enabled_setting="AUDIUS_ENABLED",
            discovery_operations={"search": frozenset({"query"})},
            health_mode="official_api",
        ),
        ProviderDescriptor(
            key="rss",
            display_name="RSS / Atom",
            capabilities=frozenset(
                {
                    ProviderCapability.discovery,
                    ProviderCapability.syndication,
                }
            ),
            workload_by_capability={
                ProviderCapability.discovery: ProviderWorkload.provider_api,
                ProviderCapability.syndication: ProviderWorkload.provider_api,
            },
            task_by_capability={
                ProviderCapability.discovery: (
                    "app.workers.provider_discovery.discover_profile"
                ),
            },
            adapter_factory=rss_factory,
            url_matchers=(),
            required_settings=("RSS_TRUSTED_FEEDS_JSON",),
            enabled_by_default=False,
            enabled_setting="RSS_ENABLED",
            discovery_operations={"feed": frozenset({"feed_url"})},
            health_mode="trusted_feed",
        ),
    )


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry.build(build_provider_descriptors(settings))


@lru_cache(maxsize=1)
def get_provider_registry() -> ProviderRegistry:
    return build_provider_registry(get_settings())
