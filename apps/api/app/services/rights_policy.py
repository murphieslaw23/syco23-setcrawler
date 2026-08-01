from pydantic import BaseModel, ConfigDict

from app.schemas.rights import RightsEvidenceInput
from app.services.provider_contracts import ProviderCapability
from app.services.provider_registry import ProviderRegistry, ProviderRegistryError


class ProviderAudioEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: bool
    reason: str


def rights_evidence_complete(
    evidence: tuple[RightsEvidenceInput, ...],
) -> bool:
    for item in evidence:
        assertions = item.assertions
        if (
            assertions.get("rights_holder") is True
            and assertions.get("allows_distribution") is True
            and assertions.get("allows_derivatives") is True
        ):
            return True
    return False


def evaluate_provider_audio_eligibility(
    registry: ProviderRegistry,
    *,
    provider_key: str,
    evidence: tuple[RightsEvidenceInput, ...],
) -> ProviderAudioEligibility:
    try:
        descriptor = registry.get(provider_key)
    except ProviderRegistryError:
        return ProviderAudioEligibility(
            eligible=False,
            reason="provider_not_registered",
        )
    if ProviderCapability.authorized_audio not in descriptor.capabilities:
        return ProviderAudioEligibility(
            eligible=False,
            reason="provider_audio_capability_missing",
        )
    if not rights_evidence_complete(evidence):
        return ProviderAudioEligibility(
            eligible=False,
            reason="rights_evidence_incomplete",
        )
    return ProviderAudioEligibility(
        eligible=True,
        reason="rights_evidence_eligible",
    )
