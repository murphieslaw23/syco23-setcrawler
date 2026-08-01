"""Manual live metadata smoke tests for the protected provider-smoke environment.

This module intentionally imports adapters and immutable contracts only. It
does not persist, publish, stream, or acquire any provider media.
"""

import asyncio
import os

import pytest

from app.services.archive_org import ArchiveOrgAdapter
from app.services.audius import AudiusAdapter
from app.services.mixcloud import MixcloudAdapter
from app.services.provider_contracts import DiscoveryPage, DiscoveryRequest
from app.services.rss import RSSAdapter, TrustedFeed


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.fail(f"protected provider smoke setting is missing: {name}")
    return value


def _adapter_and_request(provider: str) -> tuple[object, DiscoveryRequest]:
    if provider == "archive-org":
        return ArchiveOrgAdapter(), DiscoveryRequest(
            operation="search",
            parameters={"query": "DJ set"},
            limit=3,
        )
    if provider == "mixcloud":
        return MixcloudAdapter(), DiscoveryRequest(
            operation="search",
            parameters={"query": "DJ set"},
            limit=3,
        )
    if provider == "audius":
        return AudiusAdapter(
            bearer_token=_required("AUDIUS_API_BEARER_TOKEN")
        ), DiscoveryRequest(
            operation="search",
            parameters={"query": "DJ set"},
            limit=3,
        )
    if provider == "rss":
        feed_url = _required("RSS_SMOKE_FEED_URL")
        trust = TrustedFeed(
            evidence_url=_required("RSS_SMOKE_EVIDENCE_URL"),
            license=_required("RSS_SMOKE_LICENSE"),
        )
        return RSSAdapter(trusted_feeds={feed_url: trust}), DiscoveryRequest(
            operation="feed",
            parameters={"feed_url": feed_url},
            limit=3,
        )
    pytest.fail(f"unsupported provider smoke matrix entry: {provider}")


@pytest.mark.skipif(
    not os.getenv("PROVIDER_SMOKE_PROVIDER"),
    reason="protected manual provider smoke only",
)
def test_live_provider_metadata_is_reference_only() -> None:
    _required("PROVIDER_SMOKE_GUARD")
    provider = _required("PROVIDER_SMOKE_PROVIDER")
    adapter, request = _adapter_and_request(provider)

    page = asyncio.run(adapter.discover(request))

    assert isinstance(page, DiscoveryPage)
    assert len(page.items) <= request.limit
    assert not hasattr(adapter, "accept_creator_upload")
    for item in page.items:
        assert item.provider_key == provider
        for candidate in item.download_candidates:
            assert str(candidate.source_url).startswith("https://")
            assert candidate.evidence_references
            assert candidate.evidence["policy"] == "reference_only_no_fetch"
    if provider == "mixcloud":
        assert all(not item.download_candidates for item in page.items)
