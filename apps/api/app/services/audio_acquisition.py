from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
import socket
from typing import Any, BinaryIO, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from app.schemas.rights import RightsEvidenceInput
from app.services.audio_storage import AUDIO_QUARANTINE_BUCKET
from app.services.provider_contracts import (
    AuthorizedAudioCandidate,
    ProviderCapability,
)
from app.services.rights_policy import rights_evidence_complete


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_AUDIO_TYPES = frozenset(
    {
        "application/ogg",
        "audio/aac",
        "audio/flac",
        "audio/m4a",
        "audio/mp3",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/x-flac",
        "audio/x-m4a",
        "audio/x-wav",
    }
)
_BLOCKED_IPV4_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_BLOCKED_IPV6_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class AudioAcquisitionError(RuntimeError):
    """Base error for rights-gated provider acquisition."""


class AudioAcquisitionDenied(AudioAcquisitionError):
    pass


class AudioAcquisitionTargetBlocked(AudioAcquisitionError):
    pass


class AudioAcquisitionBoundsError(AudioAcquisitionError):
    pass


class AudioAcquisitionTypeError(AudioAcquisitionError):
    pass


class AudioAcquisitionNetworkError(AudioAcquisitionError):
    pass


class AudioAcquisitionHTTPError(AudioAcquisitionError):
    pass


class AudioStorageWriter(Protocol):
    def put_stream(
        self,
        bucket: str,
        stream: BinaryIO,
        *,
        length: int,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> Any: ...


Resolver = Callable[[str], tuple[str, ...]]


def _default_resolver(hostname: str) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(
            hostname,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise AudioAcquisitionTargetBlocked(
            "audio acquisition target could not be resolved"
        ) from error
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise AudioAcquisitionTargetBlocked(
            "audio acquisition target has no addresses"
        )
    return addresses


def _address_is_blocked(address: IPv4Address | IPv6Address) -> bool:
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return _address_is_blocked(address.ipv4_mapped)
    networks = (
        _BLOCKED_IPV4_NETWORKS
        if isinstance(address, IPv4Address)
        else _BLOCKED_IPV6_NETWORKS
    )
    return any(address in network for network in networks)


def _validate_target(url: str, resolver: Resolver) -> str:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise AudioAcquisitionTargetBlocked(
            "audio acquisition target has an invalid port"
        ) from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise AudioAcquisitionTargetBlocked(
            "audio acquisition target must be credential-free HTTPS on port 443"
        )

    hostname = parsed.hostname.rstrip(".").casefold()
    try:
        literal = ip_address(hostname)
    except ValueError:
        addresses = resolver(hostname)
    else:
        addresses = (str(literal),)

    if not addresses:
        raise AudioAcquisitionTargetBlocked(
            "audio acquisition target has no addresses"
        )
    for value in addresses:
        try:
            address = ip_address(value)
        except ValueError as error:
            raise AudioAcquisitionTargetBlocked(
                "audio acquisition target resolved to an invalid address"
            ) from error
        if _address_is_blocked(address):
            raise AudioAcquisitionTargetBlocked(
                "audio acquisition target resolves to a blocked address"
            )
    return url


class _IteratorReader:
    """Minimal binary reader around an HTTP response byte iterator."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks: Iterator[bytes] = iter(chunks)
        self._buffer = bytearray()
        self._finished = False

    def _pull(self) -> None:
        if self._finished:
            return
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._finished = True
            return
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        self._buffer.extend(chunk)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._finished:
                self._pull()
            value = bytes(self._buffer)
            self._buffer.clear()
            return value
        while len(self._buffer) < size and not self._finished:
            self._pull()
        value = bytes(self._buffer[:size])
        del self._buffer[:size]
        return value


class AuthorizedAudioAcquirer:
    def __init__(
        self,
        *,
        registry: Any,
        storage: AudioStorageWriter,
        client: httpx.Client,
        resolver: Resolver = _default_resolver,
        max_bytes: int,
        max_redirects: int,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_redirects < 0 or max_redirects > 10:
            raise ValueError("max_redirects must be between 0 and 10")
        self._registry = registry
        self._storage = storage
        self._client = client
        self._resolver = resolver
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects

    def _authorize(
        self,
        candidate: AuthorizedAudioCandidate,
        evidence: tuple[RightsEvidenceInput, ...],
    ) -> None:
        try:
            descriptor = self._registry.get(candidate.provider_key)
        except Exception as error:
            raise AudioAcquisitionDenied(
                "provider audio capability is unavailable"
            ) from error
        if ProviderCapability.authorized_audio not in descriptor.capabilities:
            raise AudioAcquisitionDenied(
                "provider audio capability is missing"
            )
        if not rights_evidence_complete(evidence):
            raise AudioAcquisitionDenied(
                "rights evidence is incomplete"
            )
        submitted_references = {
            str(item.reference_url) for item in evidence
        }
        candidate_references = {
            str(reference) for reference in candidate.evidence_references
        }
        if not candidate_references.issubset(submitted_references):
            raise AudioAcquisitionDenied(
                "rights evidence does not match the provider candidate"
            )

    def _open_response(self, initial_url: str) -> httpx.Response:
        current_url = initial_url
        for redirect_count in range(self._max_redirects + 1):
            _validate_target(current_url, self._resolver)
            request = self._client.build_request(
                "GET",
                current_url,
                headers={
                    "accept": "audio/*,application/ogg;q=0.9",
                    "accept-encoding": "identity",
                },
            )
            try:
                response = self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except httpx.HTTPError as error:
                raise AudioAcquisitionNetworkError(
                    "audio acquisition request failed"
                ) from error

            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            response.close()
            if not location:
                raise AudioAcquisitionHTTPError(
                    "audio acquisition redirect has no location"
                )
            if redirect_count >= self._max_redirects:
                raise AudioAcquisitionHTTPError(
                    "audio acquisition redirect limit exceeded"
                )
            current_url = urljoin(current_url, location)
        raise AudioAcquisitionHTTPError(
            "audio acquisition redirect limit exceeded"
        )

    def acquire(
        self,
        *,
        candidate: AuthorizedAudioCandidate,
        evidence: tuple[RightsEvidenceInput, ...],
    ) -> Any:
        self._authorize(candidate, evidence)
        response = self._open_response(str(candidate.source_url))
        try:
            if response.status_code < 200 or response.status_code >= 300:
                raise AudioAcquisitionHTTPError(
                    f"audio acquisition returned HTTP {response.status_code}"
                )

            raw_content_type = response.headers.get("content-type", "")
            content_type = raw_content_type.split(";", 1)[0].strip().casefold()
            if content_type not in _ALLOWED_AUDIO_TYPES:
                raise AudioAcquisitionTypeError(
                    "audio acquisition response type is not allowed"
                )

            raw_length = response.headers.get("content-length")
            try:
                length = int(raw_length or "")
            except ValueError as error:
                raise AudioAcquisitionBoundsError(
                    "audio acquisition requires a valid content length"
                ) from error
            if length < 1 or length > self._max_bytes:
                raise AudioAcquisitionBoundsError(
                    "audio acquisition content length exceeds configured bounds"
                )

            reader = _IteratorReader(response.iter_raw())
            return self._storage.put_stream(
                AUDIO_QUARANTINE_BUCKET,
                reader,
                length=length,
                content_type=content_type,
                expected_sha256=candidate.expected_sha256,
            )
        finally:
            response.close()
