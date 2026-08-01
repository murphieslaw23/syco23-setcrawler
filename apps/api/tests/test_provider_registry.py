import asyncio
import inspect
import re
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
import app.services.provider as provider_module
import app.services.provider_registry as registry_module
from app.core.config import Settings
from app.repositories.memory import InMemoryRepository
from app.services.provider import (
    ProviderAdapter,
    ProviderBlockedError,
    ProviderError,
    ProviderPayloadError,
    ProviderQuotaError,
    ProviderTemporaryError,
    ProviderValidationError,
    build_provider_descriptors,
    build_provider_registry,
    get_provider_registry,
)
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    DiscoveryRequest,
    ProviderCapability,
    ProviderDescriptor,
    ProviderItemPayload,
    ProviderWorkload,
)
from app.services.provider_registry import (
    ProviderCapabilityError,
    ProviderNotRegisteredError,
    ProviderRegistry,
    ProviderRegistryError,
    ProviderUrlMatchError,
)


class _MetadataAdapter:
    async def resolve_metadata(self, reference: str):
        return reference


class _MetadataEmbedAdapter(_MetadataAdapter):
    async def resolve_embed(self, reference: str):
        return reference


class _DiscoveryAdapter:
    async def discover(self, request):
        return request


class _AudioAdapter:
    async def decide_audio_rights(self, reference, evidence):
        return reference, evidence

    async def fetch_authorized_audio(self, candidate):
        return candidate


class _CreatorUploadAdapter:
    async def verify_creator_ownership(self, identity, evidence):
        return identity, evidence

    async def accept_creator_upload(self, request):
        return request


def _settings(**overrides) -> Settings:
    values = {
        "environment": "fixture",
        "repository_mode": "memory",
        "provider_mode": "fixture",
        "youtube_api_key": "",
        "ftm_scraper_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _descriptor(
    *,
    key: str = "fixture",
    adapter_factory=lambda: _MetadataAdapter(),
    capabilities=frozenset({ProviderCapability.metadata}),
    workloads=None,
    matcher: str = r"^https://fixture\.example/items/[^/]+$",
    required_settings=(),
) -> ProviderDescriptor:
    if workloads is None:
        workloads = {
            capability: ProviderWorkload.provider_api
            for capability in capabilities
        }
    return ProviderDescriptor(
        key=key,
        display_name=key.title(),
        capabilities=frozenset(capabilities),
        workload_by_capability=workloads,
        adapter_factory=adapter_factory,
        url_matchers=(re.compile(matcher),),
        required_settings=required_settings,
    )


def test_existing_provider_import_surface_remains_available() -> None:
    assert ProviderAdapter
    assert ProviderError.code == "provider_error"
    assert ProviderValidationError.code == "provider_validation"
    assert ProviderBlockedError.code == "provider_blocked"
    assert ProviderQuotaError.code == "provider_quota"
    assert ProviderTemporaryError.code == "provider_temporary"
    assert ProviderPayloadError.code == "provider_payload"


def test_provider_capability_vocabulary_is_stable() -> None:
    assert {item.value for item in ProviderCapability} == {
        "discovery",
        "metadata",
        "embed",
        "authorized_audio",
        "creator_upload",
        "syndication",
        "license_evidence",
    }


def test_provider_workload_vocabulary_is_stable() -> None:
    assert {item.value for item in ProviderWorkload} == {
        "provider-api",
        "provider-scrape",
        "process",
        "audio",
    }


@pytest.mark.parametrize("provider_key", ["", " YouTube", "YouTube", "you_tube", "a" * 65])
def test_provider_item_rejects_invalid_provider_keys(provider_key: str) -> None:
    with pytest.raises(ValidationError):
        ProviderItemPayload(
            provider_key=provider_key,
            external_id="item-23",
            canonical_url="https://fixture.example/items/23",
        )


@pytest.mark.parametrize("external_id", ["", " item", "item ", "x" * 513])
def test_provider_item_rejects_invalid_external_ids(external_id: str) -> None:
    with pytest.raises(ValidationError):
        ProviderItemPayload(
            provider_key="fixture",
            external_id=external_id,
            canonical_url="https://fixture.example/items/23",
        )


def test_provider_item_enforces_urls_duration_and_frozen_state() -> None:
    with pytest.raises(ValidationError):
        ProviderItemPayload(
            provider_key="fixture",
            external_id="item-23",
            canonical_url="file:///tmp/item.mp3",
        )
    with pytest.raises(ValidationError):
        ProviderItemPayload(
            provider_key="fixture",
            external_id="item-23",
            canonical_url="https://fixture.example/items/23",
            duration_seconds=-1,
        )
    item = ProviderItemPayload(
        provider_key="fixture",
        external_id="item-23",
        canonical_url="https://fixture.example/items/23",
    )
    with pytest.raises(ValidationError):
        item.title = "mutated"


@pytest.mark.parametrize(
    "payload",
    [
        {"media_bytes": b"audio"},
        {"nested": {"local_path": "/tmp/audio.mp3"}},
        {"file_path": Path("/tmp/audio.mp3")},
        {"bytes": bytearray(b"audio")},
        {"value": float("inf")},
        {"value": object()},
    ],
)
def test_metadata_models_reject_media_or_non_json_payloads(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ProviderItemPayload(
            provider_key="fixture",
            external_id="item-23",
            canonical_url="https://fixture.example/items/23",
            raw_metadata=payload,
        )


def test_discovery_request_bounds_operation_cursor_limit_and_parameters() -> None:
    assert DiscoveryRequest(operation="search").limit == 25
    for value in (0, 101):
        with pytest.raises(ValidationError):
            DiscoveryRequest(operation="search", limit=value)
    with pytest.raises(ValidationError):
        DiscoveryRequest(operation="x" * 81)
    with pytest.raises(ValidationError):
        DiscoveryRequest(operation="search", cursor="x" * 2049)
    with pytest.raises(ValidationError):
        DiscoveryRequest(operation="search", parameters={"downloaded_file": "/tmp/a"})


def test_authorized_audio_candidate_is_reference_only_and_requires_evidence() -> None:
    candidate = AuthorizedAudioCandidate(
        provider_key="fixture",
        external_id="item-23",
        source_url="https://fixture.example/items/23/original",
        evidence_references=("https://fixture.example/evidence/23",),
        expected_sha256="a" * 64,
        evidence={"license": "creator supplied"},
    )
    assert candidate.expected_sha256 == "a" * 64
    with pytest.raises(ValidationError):
        AuthorizedAudioCandidate(
            provider_key="fixture",
            external_id="item-23",
            source_url="https://fixture.example/items/23/original",
            evidence_references=(),
        )
    with pytest.raises(ValidationError):
        AuthorizedAudioCandidate(
            provider_key="fixture",
            external_id="item-23",
            source_url="https://fixture.example/items/23/original",
            evidence_references=("https://fixture.example/evidence/23",),
            evidence={"media_bytes": b"audio"},
        )
    with pytest.raises(ValidationError):
        AuthorizedAudioCandidate(
            provider_key="fixture",
            external_id="item-23",
            source_url="https://fixture.example/items/23/original",
            evidence_references=("https://fixture.example/evidence/23",),
            media_bytes=b"audio",
        )


@pytest.mark.parametrize("key", ["", "Fixture", "fixture_provider", "a" * 65])
def test_registry_rejects_malformed_descriptor_keys(key: str) -> None:
    with pytest.raises(ProviderRegistryError, match="lowercase slug"):
        ProviderRegistry.build((_descriptor(key=key),))


def test_registry_rejects_missing_or_extra_workload_mappings() -> None:
    with pytest.raises(ProviderRegistryError, match="has no workload"):
        ProviderRegistry.build(
            (
                _descriptor(
                    capabilities=frozenset({ProviderCapability.metadata}),
                    workloads={},
                ),
            )
        )
    with pytest.raises(ProviderRegistryError, match="absent capability"):
        ProviderRegistry.build(
            (
                _descriptor(
                    workloads={
                        ProviderCapability.metadata: ProviderWorkload.provider_api,
                        ProviderCapability.embed: ProviderWorkload.provider_api,
                    }
                ),
            )
        )


def test_registry_rejects_audio_workload_for_metadata() -> None:
    with pytest.raises(ProviderRegistryError, match="cannot use audio workload"):
        ProviderRegistry.build(
            (
                _descriptor(
                    workloads={ProviderCapability.metadata: ProviderWorkload.audio}
                ),
            )
        )


def test_registry_requires_declared_capability_methods() -> None:
    with pytest.raises(ProviderRegistryError, match="requires resolve_embed"):
        ProviderRegistry.build(
            (
                _descriptor(
                    capabilities=frozenset(
                        {ProviderCapability.metadata, ProviderCapability.embed}
                    ),
                    adapter_factory=lambda: _MetadataAdapter(),
                ),
            )
        )
    with pytest.raises(ProviderRegistryError, match="without declaring embed"):
        ProviderRegistry.build(
            (
                _descriptor(adapter_factory=lambda: _MetadataEmbedAdapter()),
            )
        )


def test_registry_requires_complete_rights_and_creator_upload_contracts() -> None:
    with pytest.raises(ProviderRegistryError, match="fetch_authorized_audio"):
        ProviderRegistry.build(
            (
                _descriptor(
                    capabilities=frozenset({ProviderCapability.authorized_audio}),
                    workloads={
                        ProviderCapability.authorized_audio: ProviderWorkload.audio
                    },
                    adapter_factory=lambda: type(
                        "IncompleteAudio",
                        (),
                        {"decide_audio_rights": lambda self, reference, evidence: None},
                    )(),
                ),
            )
        )
    assert ProviderRegistry.build(
        (
            _descriptor(
                capabilities=frozenset({ProviderCapability.authorized_audio}),
                workloads={ProviderCapability.authorized_audio: ProviderWorkload.audio},
                adapter_factory=lambda: _AudioAdapter(),
            ),
        )
    )
    assert ProviderRegistry.build(
        (
            _descriptor(
                capabilities=frozenset({ProviderCapability.creator_upload}),
                workloads={ProviderCapability.creator_upload: ProviderWorkload.audio},
                adapter_factory=lambda: _CreatorUploadAdapter(),
            ),
        )
    )


def test_registry_rejects_setting_values_and_duplicate_matchers() -> None:
    with pytest.raises(ProviderRegistryError, match="variable names"):
        ProviderRegistry.build(
            (_descriptor(required_settings=("API_KEY=secret",)),)
        )
    descriptor = ProviderDescriptor(
        key="fixture",
        display_name="Fixture",
        capabilities=frozenset({ProviderCapability.metadata}),
        workload_by_capability={
            ProviderCapability.metadata: ProviderWorkload.provider_api
        },
        adapter_factory=lambda: _MetadataAdapter(),
        url_matchers=(re.compile(r"^https://fixture\.example/"),) * 2,
    )
    with pytest.raises(ProviderRegistryError, match="duplicate URL matcher"):
        ProviderRegistry.build((descriptor,))


def test_registry_is_deterministic_immutable_and_extensible() -> None:
    alpha = _descriptor(key="alpha", matcher=r"^https://alpha\.example/items/")
    zulu = _descriptor(key="zulu", matcher=r"^https://zulu\.example/items/")
    registry = ProviderRegistry.build((zulu, alpha))

    assert tuple(item.key for item in registry.descriptors()) == ("alpha", "zulu")
    assert registry.get("alpha") is alpha
    assert isinstance(alpha.workload_by_capability, MappingProxyType)
    with pytest.raises(TypeError):
        alpha.workload_by_capability[ProviderCapability.embed] = ProviderWorkload.provider_api

    fixture = _descriptor(key="third", matcher=r"^https://third\.example/items/")
    extended = ProviderRegistry.build((*registry.descriptors(), fixture))
    assert tuple(item.key for item in extended.descriptors()) == (
        "alpha",
        "third",
        "zulu",
    )


def test_registry_rejects_duplicate_keys_and_ambiguous_matchers() -> None:
    with pytest.raises(ProviderRegistryError, match="duplicate provider key"):
        ProviderRegistry.build((_descriptor(), _descriptor()))
    shared = r"^https://shared\.example/items/"
    with pytest.raises(ProviderRegistryError, match="ambiguous URL matcher"):
        ProviderRegistry.build(
            (
                _descriptor(key="one", matcher=shared),
                _descriptor(key="two", matcher=shared),
            )
        )


def test_registry_factory_errors_are_sanitized() -> None:
    secret = "do-not-render-this-secret"

    def fail():
        raise RuntimeError(secret)

    with pytest.raises(ProviderRegistryError) as captured:
        ProviderRegistry.build((_descriptor(adapter_factory=fail),))
    assert "adapter factory failed" in str(captured.value)
    assert secret not in str(captured.value)


def test_registry_resolution_and_capability_errors_are_controlled() -> None:
    registry = ProviderRegistry.build((_descriptor(),))
    assert isinstance(registry.adapter("fixture"), _MetadataAdapter)
    assert registry.match_url("https://fixture.example/items/23").key == "fixture"
    with pytest.raises(ProviderNotRegisteredError):
        registry.get("missing")
    with pytest.raises(ProviderCapabilityError):
        registry.require_capability("fixture", ProviderCapability.embed)
    with pytest.raises(ProviderUrlMatchError):
        registry.match_url("https://unrelated.example/items/23")


def test_registry_module_has_no_legacy_or_concrete_provider_dependency() -> None:
    source = inspect.getsource(registry_module)
    assert "SetSource" not in source
    assert "YouTubeAdapter" not in source
    assert "SoundCloudAdapter" not in source
    assert "FTMAdapter" not in source
    assert "celery" not in source.casefold()


def test_builtin_descriptors_are_conservative_and_match_valid_urls() -> None:
    descriptors = build_provider_descriptors(_settings())
    by_key = {descriptor.key: descriptor for descriptor in descriptors}
    assert set(by_key) == {
        "archive-org",
        "audius",
        "ftm",
        "mixcloud",
        "rss",
        "soundcloud",
        "youtube",
    }
    assert by_key["youtube"].capabilities == {
        ProviderCapability.discovery,
        ProviderCapability.metadata,
        ProviderCapability.embed,
    }
    assert by_key["soundcloud"].capabilities == {
        ProviderCapability.metadata,
        ProviderCapability.embed,
    }
    assert by_key["ftm"].capabilities == {
        ProviderCapability.discovery,
        ProviderCapability.metadata,
        ProviderCapability.license_evidence,
    }
    assert by_key["archive-org"].capabilities == {
        ProviderCapability.discovery,
        ProviderCapability.metadata,
        ProviderCapability.embed,
        ProviderCapability.license_evidence,
    }
    assert by_key["mixcloud"].capabilities == {
        ProviderCapability.discovery,
        ProviderCapability.metadata,
        ProviderCapability.embed,
        ProviderCapability.syndication,
    }
    assert by_key["audius"].capabilities == {
        ProviderCapability.discovery,
        ProviderCapability.metadata,
        ProviderCapability.embed,
        ProviderCapability.license_evidence,
    }
    assert by_key["rss"].capabilities == {
        ProviderCapability.discovery,
        ProviderCapability.syndication,
    }
    for key in ("archive-org", "mixcloud", "audius", "rss"):
        descriptor = by_key[key]
        assert descriptor.enabled_by_default is False
        assert descriptor.task_by_capability[ProviderCapability.discovery] == (
            "app.workers.provider_discovery.discover_profile"
        )
    for descriptor in descriptors:
        assert ProviderCapability.authorized_audio not in descriptor.capabilities
        assert ProviderCapability.creator_upload not in descriptor.capabilities
        assert ProviderWorkload.audio not in descriptor.workload_by_capability.values()

    registry = build_provider_registry(_settings())
    assert registry.match_url("https://www.youtube.com/watch?v=abc_23").key == "youtube"
    assert registry.match_url("https://youtu.be/abc-23").key == "youtube"
    assert registry.match_url("https://soundcloud.com/syco23/live-set").key == "soundcloud"
    assert registry.match_url("https://freeteknomusic.org/sets/23hz").key == "ftm"
    assert registry.match_url("https://archive.org/details/warehouse-set-23").key == (
        "archive-org"
    )
    assert registry.match_url(
        "https://www.mixcloud.com/syco23/warehouse-set-23/"
    ).key == "mixcloud"
    assert registry.match_url("https://api.audius.co/v1/tracks/audius23").key == (
        "audius"
    )
    assert registry.match_url("https://audius.co/dj-fixture/warehouse-set-23").key == (
        "audius"
    )
    with pytest.raises(ProviderUrlMatchError):
        registry.match_url("https://example.com/watch?v=abc_23")


def test_startup_builds_one_registry_and_exposes_the_same_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderRegistry.build((_descriptor(),))
    calls = 0

    def build(settings: Settings) -> ProviderRegistry:
        nonlocal calls
        calls += 1
        return registry

    monkeypatch.setattr(main_module, "build_provider_registry", build)
    app = main_module.create_app(
        InMemoryRepository.seeded(),
        settings=_settings(),
        dispatcher=object(),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.provider_registry is registry
    assert calls == 1


def test_invalid_registry_prevents_startup_without_rendering_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-setting-value"

    def fail(settings: Settings) -> ProviderRegistry:
        raise ProviderRegistryError("provider fixture: missing metadata method")

    monkeypatch.setattr(main_module, "build_provider_registry", fail)
    app = main_module.create_app(
        InMemoryRepository.seeded(),
        settings=_settings(youtube_api_key=secret),
        dispatcher=object(),
    )
    with pytest.raises(ProviderRegistryError) as captured:
        with TestClient(app):
            pass
    assert secret not in str(captured.value)


def test_fixture_startup_does_not_call_network_or_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_network(*args, **kwargs):
        raise AssertionError("network call during startup")

    async def forbidden_subprocess(*args, **kwargs):
        raise AssertionError("subprocess call during startup")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbidden_network)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_subprocess)
    app = main_module.create_app(
        InMemoryRepository.seeded(),
        settings=_settings(),
        dispatcher=object(),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.provider_registry is not None


def test_default_registry_helper_is_cached_and_resettable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderRegistry.build((_descriptor(),))
    calls = 0

    def build(settings: Settings) -> ProviderRegistry:
        nonlocal calls
        calls += 1
        return registry

    get_provider_registry.cache_clear()
    monkeypatch.setattr(provider_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(provider_module, "build_provider_registry", build)
    try:
        assert get_provider_registry() is registry
        assert get_provider_registry() is registry
        assert calls == 1
    finally:
        get_provider_registry.cache_clear()
