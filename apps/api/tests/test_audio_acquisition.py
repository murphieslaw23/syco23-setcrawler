from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest

from app.schemas.rights import RightsEvidenceInput, RightsEvidenceType
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    ProviderCapability,
    ProviderWorkload,
)


COMPLETE_EVIDENCE = (
    RightsEvidenceInput(
        evidence_type=RightsEvidenceType.provider_permission,
        reference_url="https://rights.example/evidence/23",
        assertions={
            "rights_holder": True,
            "allows_distribution": True,
            "allows_derivatives": True,
        },
    ),
)


class FakeDescriptor:
    def __init__(self, capabilities: set[ProviderCapability]) -> None:
        self.capabilities = frozenset(capabilities)
        self.workload_by_capability = {
            capability: ProviderWorkload.audio for capability in capabilities
        }


class FakeRegistry:
    def __init__(self, capabilities: dict[str, set[ProviderCapability]]) -> None:
        self._capabilities = capabilities

    def get(self, key: str) -> FakeDescriptor:
        if key not in self._capabilities:
            raise RuntimeError("provider not registered")
        return FakeDescriptor(self._capabilities[key])


class RecordingStorage:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_stream(
        self,
        bucket: str,
        stream: object,
        *,
        length: int,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> SimpleNamespace:
        payload = stream.read()
        self.calls.append(
            {
                "bucket": bucket,
                "payload": payload,
                "length": length,
                "content_type": content_type,
                "expected_sha256": expected_sha256,
            }
        )
        return SimpleNamespace(
            bucket=bucket,
            key="objects/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            size=length,
            sha256=expected_sha256 or "a" * 64,
            etag="etag",
            version_id=None,
            content_type=content_type,
        )


def _candidate(
    *,
    provider_key: str = "archive-org",
    source_url: str = "https://media.example/audio/set23.mp3",
    expected_sha256: str | None = None,
) -> AuthorizedAudioCandidate:
    return AuthorizedAudioCandidate(
        provider_key=provider_key,
        external_id="set-23",
        source_url=source_url,
        evidence_references=("https://rights.example/evidence/23",),
        expected_sha256=expected_sha256,
        evidence={"official_download": True},
    )


def _public_resolver(hostname: str) -> tuple[str, ...]:
    mapping = {
        "media.example": ("203.0.113.23",),
        "cdn.example": ("198.51.100.23",),
        "rights.example": ("192.0.2.23",),
    }
    return mapping.get(hostname, ("203.0.113.24",))


def test_metadata_only_providers_are_rejected_before_network_access() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionDenied,
        AuthorizedAudioAcquirer,
    )

    network_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        network_calls.append(request)
        raise AssertionError("network must not be reached")

    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {
                "youtube": {ProviderCapability.metadata},
                "mixcloud": {ProviderCapability.metadata, ProviderCapability.embed},
            }
        ),
        storage=RecordingStorage(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=_public_resolver,
        max_bytes=1024,
        max_redirects=2,
    )

    for provider in ("youtube", "mixcloud"):
        with pytest.raises(AudioAcquisitionDenied, match="capability"):
            acquirer.acquire(
                candidate=_candidate(provider_key=provider),
                evidence=COMPLETE_EVIDENCE,
            )

    assert network_calls == []


def test_incomplete_evidence_is_rejected_before_network_access() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionDenied,
        AuthorizedAudioAcquirer,
    )

    network_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        network_calls.append(request)
        raise AssertionError("network must not be reached")

    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {"archive-org": {ProviderCapability.authorized_audio}}
        ),
        storage=RecordingStorage(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=_public_resolver,
        max_bytes=1024,
        max_redirects=2,
    )
    incomplete = (
        RightsEvidenceInput(
            evidence_type=RightsEvidenceType.provider_permission,
            reference_url="https://rights.example/evidence/23",
            assertions={"rights_holder": True},
        ),
    )

    with pytest.raises(AudioAcquisitionDenied, match="evidence"):
        acquirer.acquire(candidate=_candidate(), evidence=incomplete)

    assert network_calls == []


@pytest.mark.parametrize(
    "url",
    (
        "http://media.example/audio.mp3",
        "https://127.0.0.1/audio.mp3",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/audio.mp3",
        "https://user:pass@media.example/audio.mp3",
        "https://media.example:444/audio.mp3",
    ),
)
def test_unsafe_source_urls_are_rejected_before_network(url: str) -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionTargetBlocked,
        AuthorizedAudioAcquirer,
    )

    network_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        network_calls.append(request)
        raise AssertionError("network must not be reached")

    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {"archive-org": {ProviderCapability.authorized_audio}}
        ),
        storage=RecordingStorage(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=_public_resolver,
        max_bytes=1024,
        max_redirects=2,
    )

    with pytest.raises(AudioAcquisitionTargetBlocked):
        acquirer.acquire(
            candidate=_candidate(source_url=url),
            evidence=COMPLETE_EVIDENCE,
        )

    assert network_calls == []


def test_dns_resolution_to_private_address_is_rejected_before_network() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionTargetBlocked,
        AuthorizedAudioAcquirer,
    )

    network_calls: list[httpx.Request] = []

    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {"archive-org": {ProviderCapability.authorized_audio}}
        ),
        storage=RecordingStorage(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: network_calls.append(request)
                or httpx.Response(200, content=b"audio")
            )
        ),
        resolver=lambda hostname: ("10.0.0.23",),
        max_bytes=1024,
        max_redirects=2,
    )

    with pytest.raises(AudioAcquisitionTargetBlocked):
        acquirer.acquire(candidate=_candidate(), evidence=COMPLETE_EVIDENCE)

    assert network_calls == []


def test_every_redirect_target_is_revalidated() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionTargetBlocked,
        AuthorizedAudioAcquirer,
    )

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/private.mp3"},
            request=request,
        )

    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {"archive-org": {ProviderCapability.authorized_audio}}
        ),
        storage=RecordingStorage(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=_public_resolver,
        max_bytes=1024,
        max_redirects=2,
    )

    with pytest.raises(AudioAcquisitionTargetBlocked):
        acquirer.acquire(candidate=_candidate(), evidence=COMPLETE_EVIDENCE)

    assert requests == ["https://media.example/audio/set23.mp3"]


def test_content_type_and_declared_size_are_checked_before_storage() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionBoundsError,
        AudioAcquisitionTypeError,
        AuthorizedAudioAcquirer,
    )

    storage = RecordingStorage()
    responses = iter(
        (
            httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "5"},
                content=b"error",
            ),
            httpx.Response(
                200,
                headers={"content-type": "audio/mpeg", "content-length": "2048"},
                content=b"x",
            ),
        )
    )
    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {"archive-org": {ProviderCapability.authorized_audio}}
        ),
        storage=storage,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: next(responses))
        ),
        resolver=_public_resolver,
        max_bytes=1024,
        max_redirects=2,
    )

    with pytest.raises(AudioAcquisitionTypeError):
        acquirer.acquire(candidate=_candidate(), evidence=COMPLETE_EVIDENCE)
    with pytest.raises(AudioAcquisitionBoundsError):
        acquirer.acquire(candidate=_candidate(), evidence=COMPLETE_EVIDENCE)

    assert storage.calls == []


def test_authorized_audio_streams_directly_to_quarantine_with_checksum() -> None:
    from app.services.audio_acquisition import AuthorizedAudioAcquirer
    from app.services.audio_storage import AUDIO_QUARANTINE_BUCKET

    payload = b"authorized-audio"
    checksum = "b" * 64
    storage = RecordingStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/mpeg",
                "content-length": str(len(payload)),
            },
            stream=httpx.ByteStream(payload),
            request=request,
        )

    acquirer = AuthorizedAudioAcquirer(
        registry=FakeRegistry(
            {"archive-org": {ProviderCapability.authorized_audio}}
        ),
        storage=storage,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=_public_resolver,
        max_bytes=1024,
        max_redirects=2,
    )

    stored = acquirer.acquire(
        candidate=_candidate(expected_sha256=checksum),
        evidence=COMPLETE_EVIDENCE,
    )

    assert stored.bucket == AUDIO_QUARANTINE_BUCKET
    assert storage.calls == [
        {
            "bucket": AUDIO_QUARANTINE_BUCKET,
            "payload": payload,
            "length": len(payload),
            "content_type": "audio/mpeg",
            "expected_sha256": checksum,
        }
    ]
