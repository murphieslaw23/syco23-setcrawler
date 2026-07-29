from typing import Protocol

from app.services.normalizer import RawSetPayload


class ProviderAdapter(Protocol):
    async def fetch(self, url: str) -> RawSetPayload: ...


class ProviderError(RuntimeError):
    code = "provider_error"
    retryable = False


class ProviderValidationError(ProviderError):
    code = "provider_validation"


class ProviderBlockedError(ProviderError):
    code = "provider_blocked"


class ProviderQuotaError(ProviderError):
    code = "provider_quota"


class ProviderTemporaryError(ProviderError):
    code = "provider_temporary"
    retryable = True


class ProviderPayloadError(ProviderError):
    code = "provider_payload"
