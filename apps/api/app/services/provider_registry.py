from __future__ import annotations

import re
from collections import defaultdict
from types import MappingProxyType
from typing import Iterable, Mapping

from app.services.provider_contracts import (
    ProviderCapability,
    ProviderDescriptor,
    ProviderWorkload,
)


_PROVIDER_KEY = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SETTING_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CAPABILITY_METHODS: Mapping[ProviderCapability, tuple[str, ...]] = MappingProxyType(
    {
        ProviderCapability.discovery: ("discover",),
        ProviderCapability.metadata: ("resolve_metadata",),
        ProviderCapability.embed: ("resolve_embed",),
        ProviderCapability.authorized_audio: (
            "decide_audio_rights",
            "fetch_authorized_audio",
        ),
        ProviderCapability.creator_upload: (
            "verify_creator_ownership",
            "accept_creator_upload",
        ),
        ProviderCapability.syndication: ("syndicate",),
        ProviderCapability.license_evidence: ("resolve_license_evidence",),
    }
)
_AUDIO_CAPABILITIES = {
    ProviderCapability.authorized_audio,
    ProviderCapability.creator_upload,
}


class ProviderRegistryError(RuntimeError):
    def __init__(self, problems: Iterable[str] | str) -> None:
        normalized = (
            (problems,)
            if isinstance(problems, str)
            else tuple(sorted(set(problems)))
        )
        self.problems = normalized
        super().__init__("invalid provider registry: " + "; ".join(normalized))


class ProviderNotRegisteredError(ProviderRegistryError):
    pass


class ProviderCapabilityError(ProviderRegistryError):
    pass


class ProviderUrlMatchError(ProviderRegistryError):
    pass


def _descriptor_problems(descriptor: ProviderDescriptor) -> list[str]:
    label = descriptor.key if isinstance(descriptor.key, str) and descriptor.key else "<unknown>"
    problems: list[str] = []
    if not isinstance(descriptor.key, str) or _PROVIDER_KEY.fullmatch(descriptor.key) is None:
        problems.append(f"provider {label}: key must be a lowercase slug")
    if (
        not isinstance(descriptor.display_name, str)
        or not descriptor.display_name.strip()
        or descriptor.display_name != descriptor.display_name.strip()
        or len(descriptor.display_name) > 128
    ):
        problems.append(f"provider {label}: display_name must be non-empty and trimmed")
    if not descriptor.capabilities:
        problems.append(f"provider {label}: at least one capability is required")
    if not callable(descriptor.adapter_factory):
        problems.append(f"provider {label}: adapter_factory must be callable")
    if not isinstance(descriptor.descriptor_version, int) or descriptor.descriptor_version < 1:
        problems.append(f"provider {label}: descriptor_version must be positive")

    capability_keys = set(descriptor.capabilities)
    workload_keys = set(descriptor.workload_by_capability)
    for capability in sorted(capability_keys - workload_keys, key=str):
        problems.append(f"provider {label}: capability {capability.value} has no workload")
    for capability in sorted(workload_keys - capability_keys, key=str):
        name = getattr(capability, "value", str(capability))
        problems.append(f"provider {label}: workload declared for absent capability {name}")
    for capability, workload in sorted(
        descriptor.workload_by_capability.items(),
        key=lambda item: str(item[0]),
    ):
        if not isinstance(capability, ProviderCapability):
            problems.append(f"provider {label}: unknown capability in workload mapping")
            continue
        if not isinstance(workload, ProviderWorkload):
            problems.append(f"provider {label}: unsupported workload for {capability.value}")
            continue
        if workload is ProviderWorkload.audio and capability not in _AUDIO_CAPABILITIES:
            problems.append(
                f"provider {label}: metadata capability {capability.value} cannot use audio workload"
            )

    patterns: list[str] = []
    for matcher in descriptor.url_matchers:
        pattern = getattr(matcher, "pattern", None)
        if not isinstance(pattern, str) or not callable(getattr(matcher, "search", None)):
            problems.append(f"provider {label}: URL matcher must be a compiled regex")
            continue
        patterns.append(pattern)
    duplicates = sorted(pattern for pattern in set(patterns) if patterns.count(pattern) > 1)
    for pattern in duplicates:
        problems.append(f"provider {label}: duplicate URL matcher {pattern}")
    if not patterns:
        problems.append(f"provider {label}: at least one URL matcher is required")

    for setting in sorted(descriptor.required_settings):
        if not isinstance(setting, str) or _SETTING_NAME.fullmatch(setting) is None:
            problems.append(
                f"provider {label}: required setting declarations must be variable names"
            )
    if len(set(descriptor.required_settings)) != len(descriptor.required_settings):
        problems.append(f"provider {label}: duplicate required setting declaration")
    return problems


def _adapter_problems(descriptor: ProviderDescriptor, adapter: object) -> list[str]:
    problems: list[str] = []
    for capability in sorted(descriptor.capabilities, key=str):
        for method_name in _CAPABILITY_METHODS[capability]:
            if not callable(getattr(adapter, method_name, None)):
                problems.append(
                    f"provider {descriptor.key}: capability {capability.value} requires {method_name}"
                )
    for capability in ProviderCapability:
        if capability in descriptor.capabilities:
            continue
        for method_name in _CAPABILITY_METHODS[capability]:
            if callable(getattr(adapter, method_name, None)):
                problems.append(
                    f"provider {descriptor.key}: adapter exposes {method_name} without declaring {capability.value}"
                )
    return problems


def validate_registry(descriptors: Iterable[ProviderDescriptor]) -> tuple[str, ...]:
    items = tuple(descriptors)
    problems: list[str] = []
    for descriptor in sorted(items, key=lambda item: str(getattr(item, "key", ""))):
        if not isinstance(descriptor, ProviderDescriptor):
            problems.append("registry entry is not a ProviderDescriptor")
            continue
        problems.extend(_descriptor_problems(descriptor))

    keys = [item.key for item in items if isinstance(item, ProviderDescriptor)]
    for key in sorted(set(keys)):
        if keys.count(key) > 1:
            problems.append(f"duplicate provider key {key}")

    pattern_owners: dict[str, set[str]] = defaultdict(set)
    for descriptor in items:
        if not isinstance(descriptor, ProviderDescriptor):
            continue
        for matcher in descriptor.url_matchers:
            pattern = getattr(matcher, "pattern", None)
            if isinstance(pattern, str):
                pattern_owners[pattern].add(descriptor.key)
    for pattern, owners in sorted(pattern_owners.items()):
        if len(owners) > 1:
            problems.append(
                f"ambiguous URL matcher {pattern} shared by {', '.join(sorted(owners))}"
            )
    return tuple(sorted(set(problems)))


class ProviderRegistry:
    __slots__ = ("_adapters", "_descriptors")

    def __init__(
        self,
        descriptors: Mapping[str, ProviderDescriptor],
        adapters: Mapping[str, object],
    ) -> None:
        self._descriptors = MappingProxyType(dict(descriptors))
        self._adapters = MappingProxyType(dict(adapters))

    @classmethod
    def build(cls, descriptors: Iterable[ProviderDescriptor]) -> "ProviderRegistry":
        items = tuple(descriptors)
        problems = list(validate_registry(items))
        adapters: dict[str, object] = {}
        for descriptor in sorted(items, key=lambda item: str(getattr(item, "key", ""))):
            if not isinstance(descriptor, ProviderDescriptor):
                continue
            if not callable(descriptor.adapter_factory):
                continue
            try:
                adapter = descriptor.adapter_factory()
            except Exception:
                problems.append(
                    f"provider {descriptor.key}: adapter factory failed"
                )
                continue
            adapters[descriptor.key] = adapter
            problems.extend(_adapter_problems(descriptor, adapter))
        if problems:
            raise ProviderRegistryError(problems)
        descriptor_map = {
            descriptor.key: descriptor
            for descriptor in sorted(items, key=lambda item: item.key)
        }
        return cls(descriptor_map, adapters)

    def get(self, key: str) -> ProviderDescriptor:
        try:
            return self._descriptors[key]
        except KeyError as error:
            raise ProviderNotRegisteredError(f"provider {key}: not registered") from error

    def adapter(self, key: str) -> object:
        try:
            return self._adapters[key]
        except KeyError as error:
            raise ProviderNotRegisteredError(f"provider {key}: not registered") from error

    def require_capability(
        self,
        key: str,
        capability: ProviderCapability,
    ) -> ProviderDescriptor:
        descriptor = self.get(key)
        if capability not in descriptor.capabilities:
            raise ProviderCapabilityError(
                f"provider {key}: capability {capability.value} is not supported"
            )
        return descriptor

    def match_url(self, url: str) -> ProviderDescriptor:
        matches = [
            descriptor
            for descriptor in self._descriptors.values()
            if any(matcher.search(url) is not None for matcher in descriptor.url_matchers)
        ]
        if len(matches) != 1:
            if not matches:
                raise ProviderUrlMatchError("URL does not match a registered provider")
            raise ProviderUrlMatchError(
                "URL matches multiple registered providers: "
                + ", ".join(sorted(item.key for item in matches))
            )
        return matches[0]

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(self._descriptors.values())
