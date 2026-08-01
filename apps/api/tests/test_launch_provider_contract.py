import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.set import SetSource
from app.services.normalizer import RawSetPayload
from app.services.provider import _item_payload
from app.services.archive_org import ArchiveOrgAdapter
from app.services.audius import AudiusAdapter
from app.services.mixcloud import MixcloudAdapter
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    DiscoveryPage,
    DiscoveryRequest,
    ProviderItemPayload,
)
from app.services.rss import RSSAdapter, TrustedFeed


FIXTURES = Path(__file__).parent / "fixtures"


def _json_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _xml_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _archive_transport(request: httpx.Request) -> httpx.Response:
    fixture = (
        "archive_search.json"
        if request.url.path == "/advancedsearch.php"
        else "archive_metadata.json"
    )
    return httpx.Response(200, json=_json_fixture(fixture))


def _mixcloud_transport(request: httpx.Request) -> httpx.Response:
    fixture = (
        "mixcloud_search.json"
        if request.url.path == "/search/"
        else "mixcloud_item.json"
    )
    return httpx.Response(200, json=_json_fixture(fixture))


def _audius_transport(request: httpx.Request) -> httpx.Response:
    fixture = (
        "audius_search.json"
        if request.url.path == "/v1/tracks/search"
        else "audius_track.json"
    )
    return httpx.Response(200, json=_json_fixture(fixture))


def _rss_transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=_xml_fixture("rss_feed.xml"),
        headers={"content-type": "application/rss+xml"},
    )


@pytest.mark.parametrize(
    ("provider_key", "adapter", "discovery_request"),
    [
        (
            "archive-org",
            ArchiveOrgAdapter(transport=httpx.MockTransport(_archive_transport)),
            DiscoveryRequest(
                operation="search",
                parameters={"query": "warehouse set"},
            ),
        ),
        (
            "mixcloud",
            MixcloudAdapter(transport=httpx.MockTransport(_mixcloud_transport)),
            DiscoveryRequest(
                operation="search",
                parameters={"query": "warehouse set"},
            ),
        ),
        (
            "audius",
            AudiusAdapter(transport=httpx.MockTransport(_audius_transport)),
            DiscoveryRequest(
                operation="search",
                parameters={"query": "warehouse set"},
            ),
        ),
        (
            "rss",
            RSSAdapter(
                trusted_feeds={
                    "https://feeds.example/sets.xml": TrustedFeed(
                        evidence_url="https://feeds.example/rights",
                        license="CC-BY-4.0",
                    )
                },
                transport=httpx.MockTransport(_rss_transport),
            ),
            DiscoveryRequest(
                operation="feed",
                parameters={"feed_url": "https://feeds.example/sets.xml"},
            ),
        ),
    ],
)
def test_launch_adapters_share_the_offline_discovery_contract(
    provider_key: str,
    adapter: object,
    discovery_request: DiscoveryRequest,
) -> None:
    page = asyncio.run(adapter.discover(discovery_request))

    assert isinstance(page, DiscoveryPage)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.provider_key == provider_key
    assert item.external_id
    assert str(item.canonical_url).startswith("https://")
    assert item.title == "Warehouse Set 23"
    assert "media_bytes" not in item.model_dump(mode="json")
    for candidate in item.download_candidates:
        assert str(candidate.source_url).startswith("https://")
        assert candidate.evidence_references
        assert not hasattr(candidate, "media_bytes")


def test_provider_item_rejects_cross_provider_download_candidate() -> None:
    candidate = AuthorizedAudioCandidate(
        provider_key="audius",
        external_id="track-23",
        source_url="https://api.audius.co/v1/tracks/track-23/download",
        evidence_references=("https://audius.co/dj/track-23",),
    )

    with pytest.raises(ValidationError, match="candidate provider must match item"):
        ProviderItemPayload(
            provider_key="archive-org",
            external_id="track-23",
            canonical_url="https://archive.org/details/track-23",
            download_candidates=(candidate,),
        )


@pytest.mark.parametrize(
    ("source_url", "evidence_url"),
    [
        (
            "http://provider.example/items/23/audio",
            "https://provider.example/items/23/rights",
        ),
        (
            "https://provider.example/items/23/audio",
            "http://provider.example/items/23/rights",
        ),
    ],
)
def test_download_candidate_requires_https_references(
    source_url: str,
    evidence_url: str,
) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        AuthorizedAudioCandidate(
            provider_key="fixture",
            external_id="item-23",
            source_url=source_url,
            evidence_references=(evidence_url,),
        )


def test_archive_org_exposes_only_original_audio_with_permissive_license() -> None:
    adapter = ArchiveOrgAdapter(transport=httpx.MockTransport(_archive_transport))

    item = asyncio.run(
        adapter.resolve_metadata("https://archive.org/details/warehouse-set-23")
    )

    assert len(item.download_candidates) == 1
    candidate = item.download_candidates[0]
    assert str(candidate.source_url).endswith(
        "/download/warehouse-set-23/warehouse-set-23.mp3"
    )
    assert candidate.evidence["license"] == "CC-BY-4.0"
    assert item.license_evidence == {
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "policy": "explicit_permissive_license",
    }


def test_mixcloud_metadata_and_embed_never_produce_download_candidates() -> None:
    adapter = MixcloudAdapter(transport=httpx.MockTransport(_mixcloud_transport))

    item = asyncio.run(
        adapter.resolve_metadata(
            "https://www.mixcloud.com/dj-fixture/warehouse-set-23/"
        )
    )

    assert str(item.embed_url).startswith("https://www.mixcloud.com/widget/iframe/")
    assert item.download_candidates == ()
    assert not hasattr(adapter, "fetch_authorized_audio")


def test_audius_requires_unconditional_download_and_permissive_license() -> None:
    permitted = AudiusAdapter(transport=httpx.MockTransport(_audius_transport))
    item = asyncio.run(permitted.resolve_metadata("audius-track-23"))
    assert len(item.download_candidates) == 1

    payload = _json_fixture("audius_track.json")
    payload["data"]["download_conditions"] = {"follow_user_id": "artist-23"}

    def conditioned_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    conditioned = AudiusAdapter(
        transport=httpx.MockTransport(conditioned_transport)
    )
    conditioned_item = asyncio.run(
        conditioned.resolve_metadata("audius-track-23")
    )
    assert conditioned_item.download_candidates == ()

    sdk_payload = _json_fixture("audius_track.json")
    sdk_payload["data"]["downloadable"] = sdk_payload["data"].pop(
        "is_downloadable"
    )

    def sdk_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sdk_payload)

    sdk_item = asyncio.run(
        AudiusAdapter(transport=httpx.MockTransport(sdk_transport)).resolve_metadata(
            "audius-track-23"
        )
    )
    assert len(sdk_item.download_candidates) == 1


def test_audius_sends_server_side_bearer_token_without_serializing_it() -> None:
    def authorized_transport(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture-token"
        return httpx.Response(200, json=_json_fixture("audius_track.json"))

    adapter = AudiusAdapter(
        bearer_token="fixture-token",
        transport=httpx.MockTransport(authorized_transport),
    )

    item = asyncio.run(adapter.resolve_metadata("audius-track-23"))

    assert "fixture-token" not in str(item.model_dump(mode="json"))


def test_soundcloud_retains_official_download_distinction_without_fetching() -> None:
    payload = RawSetPayload(
        source=SetSource.soundcloud,
        source_id="soundcloud-track-23",
        canonical_url="https://soundcloud.com/dj-fixture/warehouse-set-23",
        title="Warehouse Set 23",
        raw_payload={
            "downloadable": True,
            "download_url": "https://api.soundcloud.com/tracks/23/download",
            "license": "CC-BY-4.0",
        },
    )

    item = _item_payload("soundcloud", payload)

    assert item.provenance["official_download_available"] is True
    assert len(item.download_candidates) == 1
    assert item.download_candidates[0].evidence["policy"] == "reference_only_no_fetch"

    youtube = payload.model_copy(update={"source": SetSource.youtube})
    assert _item_payload("youtube", youtube).download_candidates == ()


def test_rss_enclosure_without_explicit_trust_never_grants_rights() -> None:
    adapter = RSSAdapter()

    page = adapter.parse_feed(
        _xml_fixture("rss_feed.xml"),
        feed_url="https://feeds.example/sets.xml",
    )

    assert len(page.items) == 1
    assert page.items[0].raw_metadata["enclosure"]["type"] == "audio/mpeg"
    assert page.items[0].download_candidates == ()


def test_atom_enclosure_uses_the_same_normalized_contract_without_implied_rights() -> None:
    page = RSSAdapter().parse_feed(
        _xml_fixture("atom_feed.xml"),
        feed_url="https://feeds.example/atom.xml",
    )

    assert len(page.items) == 1
    item = page.items[0]
    assert item.external_id == "tag:feeds.example,2026:warehouse-set-24"
    assert item.title == "Warehouse Set 24"
    assert item.creator_name == "DJ Fixture"
    assert item.raw_metadata["enclosure"]["type"] == "audio/ogg"
    assert item.download_candidates == ()


def test_rss_rejects_active_xml_and_untrusted_network_targets() -> None:
    adapter = RSSAdapter()

    with pytest.raises(Exception, match="rss_unsafe_xml"):
        adapter.parse_feed(
            b"<!DOCTYPE rss [<!ENTITY x 'unsafe'>]><rss>&x;</rss>",
            feed_url="https://feeds.example/sets.xml",
        )
    with pytest.raises(Exception, match="rss_feed_not_trusted"):
        asyncio.run(
            adapter.discover(
                DiscoveryRequest(
                    operation="feed",
                    parameters={"feed_url": "https://untrusted.example/feed.xml"},
                )
            )
        )
