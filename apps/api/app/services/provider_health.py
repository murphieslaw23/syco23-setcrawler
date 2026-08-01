from __future__ import annotations

from app.core.config import Settings
from app.services.provider_contracts import ProviderDescriptor
from app.services.provider_registry import ProviderRegistry


def _setting_value(settings: Settings, environment_name: str) -> object:
    return getattr(settings, environment_name.casefold(), None)


def descriptor_runtime_state(
    descriptor: ProviderDescriptor,
    settings: Settings,
) -> dict[str, object]:
    missing = [
        name
        for name in descriptor.required_settings
        if not _setting_value(settings, name)
    ]
    configuration_complete = not missing
    explicitly_enabled = (
        bool(_setting_value(settings, descriptor.enabled_setting))
        if descriptor.enabled_setting
        else descriptor.enabled_by_default
    )
    enabled = (
        settings.provider_mode == "live"
        and configuration_complete
        and explicitly_enabled
    )
    reason = None
    if settings.provider_mode != "live":
        reason = "provider_runtime_disabled"
    elif not explicitly_enabled:
        reason = "provider_disabled"
    elif not configuration_complete:
        reason = "provider_configuration_missing"
    return {
        "key": descriptor.key,
        "display_name": descriptor.display_name,
        "configured": configuration_complete,
        "configuration_complete": configuration_complete,
        "enabled": enabled,
        "effective_enabled": enabled,
        "database_enabled": explicitly_enabled,
        "runtime_mode": settings.provider_mode,
        "capabilities": sorted(item.value for item in descriptor.capabilities),
        "workloads": {
            capability.value: workload.value
            for capability, workload in sorted(
                descriptor.workload_by_capability.items(),
                key=lambda item: item[0].value,
            )
        },
        "reason": reason,
    }


def provider_runtime_states(
    registry: ProviderRegistry,
    settings: Settings,
) -> dict[str, dict[str, object]]:
    return {
        descriptor.key: descriptor_runtime_state(descriptor, settings)
        for descriptor in registry.descriptors()
    }
