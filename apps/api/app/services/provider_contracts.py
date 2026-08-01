from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Pattern

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


_PROVIDER_KEY = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_RESERVED_MEDIA_KEYS = {
    "downloaded_file",
    "file_path",
    "local_path",
    "media_bytes",
}


class ProviderCapability(StrEnum):
    discovery = "discovery"
    metadata = "metadata"
    embed = "embed"
    authorized_audio = "authorized_audio"
    creator_upload = "creator_upload"
    syndication = "syndication"
    license_evidence = "license_evidence"


class ProviderWorkload(StrEnum):
    provider_api = "provider-api"
    provider_scrape = "provider-scrape"
    process = "process"
    audio = "audio"


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and trimmed")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _provider_key(value: object) -> str:
    key = _bounded_text(value, field="provider_key", maximum=64)
    if _PROVIDER_KEY.fullmatch(key) is None:
        raise ValueError("provider_key must be a lowercase slug")
    return key


def _validate_json_tree(value: object, *, path: str = "metadata") -> object:
    if isinstance(value, (bytes, bytearray, memoryview, Path)):
        raise ValueError(f"{path} contains byte-bearing or local-path data")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            if key.casefold() in _RESERVED_MEDIA_KEYS:
                raise ValueError(f"{path} contains prohibited media field {key}")
            _validate_json_tree(item, path=f"{path}.{key}")
        return value
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_tree(item, path=f"{path}[{index}]")
        return value
    raise ValueError(f"{path} is not JSON-compatible")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderItemPayload(_FrozenModel):
    provider_key: str
    external_id: str
    canonical_url: AnyHttpUrl
    title: str | None = Field(default=None, max_length=500)
    published_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    creator_name: str | None = Field(default=None, max_length=300)
    embed_url: AnyHttpUrl | None = None
    artwork_candidates: tuple[AnyHttpUrl, ...] = Field(
        default=(),
        max_length=32,
    )
    raw_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    provenance: dict[str, JsonValue] = Field(default_factory=dict)
    license_evidence: dict[str, JsonValue] | None = None

    @field_validator("provider_key", mode="before")
    @classmethod
    def validate_provider_key(cls, value: object) -> str:
        return _provider_key(value)

    @field_validator("external_id", mode="before")
    @classmethod
    def validate_external_id(cls, value: object) -> str:
        return _bounded_text(value, field="external_id", maximum=512)

    @field_validator(
        "raw_metadata",
        "provenance",
        "license_evidence",
        mode="before",
    )
    @classmethod
    def validate_metadata(cls, value: object) -> object:
        if value is None:
            return value
        return _validate_json_tree(value)


class DiscoveryRequest(_FrozenModel):
    operation: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    cursor: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=25, ge=1, le=100)

    @field_validator("operation", mode="before")
    @classmethod
    def validate_operation(cls, value: object) -> str:
        return _bounded_text(value, field="operation", maximum=80)

    @field_validator("cursor", mode="before")
    @classmethod
    def validate_cursor(cls, value: object) -> object:
        if value is None:
            return value
        return _bounded_text(value, field="cursor", maximum=2048)

    @field_validator("parameters", mode="before")
    @classmethod
    def validate_parameters(cls, value: object) -> object:
        return _validate_json_tree(value, path="parameters")


class DiscoveryPage(_FrozenModel):
    items: tuple[ProviderItemPayload, ...]
    next_cursor: str | None = Field(default=None, max_length=2048)


class AuthorizedAudioCandidate(_FrozenModel):
    provider_key: str
    external_id: str
    source_url: AnyHttpUrl
    evidence_references: tuple[AnyHttpUrl, ...] = Field(
        min_length=1,
        max_length=32,
    )
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    evidence: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("provider_key", mode="before")
    @classmethod
    def validate_provider_key(cls, value: object) -> str:
        return _provider_key(value)

    @field_validator("external_id", mode="before")
    @classmethod
    def validate_external_id(cls, value: object) -> str:
        return _bounded_text(value, field="external_id", maximum=512)

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_evidence(cls, value: object) -> object:
        return _validate_json_tree(value, path="evidence")

    @model_validator(mode="after")
    def require_evidence(self) -> "AuthorizedAudioCandidate":
        if not self.evidence_references:
            raise ValueError("at least one rights-evidence reference is required")
        return self


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    key: str
    display_name: str
    capabilities: frozenset[ProviderCapability]
    workload_by_capability: Mapping[ProviderCapability, ProviderWorkload]
    adapter_factory: Callable[[], object]
    url_matchers: tuple[Pattern[str], ...]
    required_settings: tuple[str, ...] = ()
    enabled_by_default: bool = True
    enabled_setting: str | None = None
    public_health_key: str | None = None
    health_mode: str = "descriptor"
    descriptor_version: int = 1
    discovery_operations: Mapping[str, frozenset[str]] = field(default_factory=dict)
    task_by_capability: Mapping[ProviderCapability, str] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(
            self,
            "workload_by_capability",
            MappingProxyType(dict(self.workload_by_capability)),
        )
        object.__setattr__(
            self,
            "task_by_capability",
            MappingProxyType(dict(self.task_by_capability)),
        )
        object.__setattr__(self, "url_matchers", tuple(self.url_matchers))
        object.__setattr__(self, "required_settings", tuple(self.required_settings))
        if self.public_health_key is None:
            object.__setattr__(self, "public_health_key", self.key)
        object.__setattr__(
            self,
            "discovery_operations",
            MappingProxyType(
                {
                    operation: frozenset(parameters)
                    for operation, parameters in self.discovery_operations.items()
                }
            ),
        )
