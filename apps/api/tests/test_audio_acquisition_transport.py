from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest

from app.schemas.rights import RightsEvidenceInput, RightsEvidenceType
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    ProviderCapability,
)


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, payload: bytes = b"audio") -> None:
        self.payload = payload
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/mpeg",
                "content-length": str(len(self.payload)),
            },
            content=self.payload,
            request=request,
        )


class Registry:
    def get(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(
            capabilities=frozenset({ProviderCapability.authorized_audio})
        )


class ReadingStorage:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def put_stream(
        self,
        bucket: str,
        stream: object,
        *,
        length: int,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> SimpleNamespace:
        self.payloads.append(stream.read())
        return SimpleNamespace(bucket=bucket, size=length)


def _candidate() -> AuthorizedAudioCandidate:
    return AuthorizedAudioCandidate(
        provider_key="archive-org",
        external_id="set-23",
        source_url="https://media.example/audio/set23.mp3",
        evidence_references=("https://rights.example/evidence/23",),
        evidence={"official_download": True},
    )


def _evidence() -> tuple[RightsEvidenceInput, ...]:
    return (
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


def test_transport_connects_to_validated_ip_with_original_sni_and_host() -> None:
    from app.services.audio_acquisition import PinnedHTTPSNetworkTransport

    inner = RecordingTransport()
    transport = PinnedHTTPSNetworkTransport(
        resolver=lambda hostname: ("203.0.113.23",),
        inner=inner,
    )
    client = httpx.Client(transport=transport)

    response = client.get("https://media.example/audio/set23.mp3")

    assert response.status_code == 200
    assert len(inner.requests) == 1
    pinned = inner.requests[0]
    assert pinned.url.host == "203.0.113.23"
    assert pinned.url.path == "/audio/set23.mp3"
    assert pinned.headers["host"] == "media.example"
    assert pinned.extensions["sni_hostname"] == "media.example"


def test_transport_blocks_rebinding_before_inner_network_call() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionTargetBlocked,
        AuthorizedAudioAcquirer,
        PinnedHTTPSNetworkTransport,
    )

    answers: Iterator[tuple[str, ...]] = iter(
        (("203.0.113.23",), ("10.0.0.23",))
    )

    def rebinding_resolver(hostname: str) -> tuple[str, ...]:
        return next(answers)

    inner = RecordingTransport()
    client = httpx.Client(
        transport=PinnedHTTPSNetworkTransport(
            resolver=rebinding_resolver,
            inner=inner,
        )
    )
    acquirer = AuthorizedAudioAcquirer(
        registry=Registry(),
        storage=ReadingStorage(),
        client=client,
        resolver=rebinding_resolver,
        max_bytes=1024,
        max_redirects=2,
    )

    with pytest.raises(AudioAcquisitionTargetBlocked):
        acquirer.acquire(candidate=_candidate(), evidence=_evidence())

    assert inner.requests == []


def test_streaming_aborts_when_total_transfer_deadline_is_exceeded() -> None:
    from app.services.audio_acquisition import (
        AudioAcquisitionBoundsError,
        AuthorizedAudioAcquirer,
    )

    times = iter((0.0, 0.25, 2.0))
    storage = ReadingStorage()

    class ChunkedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"audio"
            yield b"-data"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/mpeg",
                "content-length": "10",
            },
            stream=ChunkedStream(),
            request=request,
        )

    acquirer = AuthorizedAudioAcquirer(
        registry=Registry(),
        storage=storage,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=lambda hostname: ("203.0.113.23",),
        max_bytes=1024,
        max_redirects=2,
        max_seconds=1.0,
        clock=lambda: next(times),
    )

    with pytest.raises(AudioAcquisitionBoundsError, match="deadline"):
        acquirer.acquire(candidate=_candidate(), evidence=_evidence())

    assert storage.payloads == []
