import re

import pytest

from app.services.provider_contracts import ProviderDescriptor, ProviderWorkload
from app.services.provider_registry import ProviderRegistry, ProviderRegistryError


class _MetadataAdapter:
    async def resolve_metadata(self, reference: str):
        return reference


def test_unknown_capability_declaration_is_a_controlled_registry_error() -> None:
    descriptor = ProviderDescriptor(
        key="fixture",
        display_name="Fixture",
        capabilities=frozenset({"unknown-capability"}),  # type: ignore[arg-type]
        workload_by_capability={
            "unknown-capability": ProviderWorkload.provider_api,  # type: ignore[dict-item]
        },
        adapter_factory=lambda: _MetadataAdapter(),
        url_matchers=(re.compile(r"^https://fixture\.example/items/"),),
    )

    with pytest.raises(ProviderRegistryError, match="unknown capability"):
        ProviderRegistry.build((descriptor,))
