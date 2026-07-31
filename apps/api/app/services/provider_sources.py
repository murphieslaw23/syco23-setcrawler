from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.set import SetSource


_LEGACY_PROVIDER_KEYS: dict[SetSource, str] = {
    SetSource.youtube: "youtube",
    SetSource.soundcloud: "soundcloud",
    SetSource.freeteknomusic: "ftm",
}
_PROHIBITED_METADATA_KEYS = {
    "downloaded_file",
    "file_path",
    "local_path",
    "media_bytes",
}
_OMIT = object()


class SourceIntegrityError(RuntimeError):
    """Raised when legacy and provider source representations diverge."""


class ProviderSourceProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_key: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=512)
    canonical_url: str = Field(min_length=8, max_length=4096)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    is_primary: bool = True


def legacy_source_to_provider_key(source: SetSource | str) -> str:
    try:
        normalized = source if isinstance(source, SetSource) else SetSource(source)
        return _LEGACY_PROVIDER_KEYS[normalized]
    except (KeyError, ValueError) as error:
        raise SourceIntegrityError("source projection has an unknown legacy source") from error


def validate_source_projection(
    *,
    legacy_source: SetSource | str,
    legacy_external_id: str,
    provider_key: str | None,
    provider_external_id: str | None,
    is_primary: bool | None,
) -> None:
    expected_provider = legacy_source_to_provider_key(legacy_source)
    if (
        provider_key != expected_provider
        or provider_external_id != legacy_external_id
        or is_primary is not True
    ):
        raise SourceIntegrityError(
            "source projection mismatch between legacy fields and primary provider link"
        )


def sanitize_provider_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_value(raw)
    if not isinstance(sanitized, dict):
        return {}
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview, Path)):
        return _OMIT
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _OMIT
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if key.casefold() in _PROHIBITED_METADATA_KEYS:
                continue
            sanitized = _sanitize_value(item)
            if sanitized is not _OMIT:
                result[key] = sanitized
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            sanitized = _sanitize_value(item)
            if sanitized is not _OMIT:
                result.append(sanitized)
        return result
    return _OMIT
