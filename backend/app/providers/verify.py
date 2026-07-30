from dataclasses import dataclass, field

from app.providers.base import LLMProvider, Message, ProviderError, ProviderErrorKind


@dataclass
class VerifyResult:
    ok: bool
    error: str | None
    kind: ProviderErrorKind | None = field(default=None)


async def verify_key(
    provider: LLMProvider,
    model: str,
    api_key: str,
    timeout: float = 15.0,
) -> VerifyResult:
    try:
        await provider.complete(model, [Message("user", "ping")], api_key, timeout)
        return VerifyResult(ok=True, error=None, kind=None)
    except ProviderError as exc:
        raw = str(exc)
        safe = raw.replace(api_key, "***") if api_key else raw
        return VerifyResult(ok=False, error=safe, kind=exc.kind)
    except Exception as exc:  # noqa: BLE001 — safe admin-facing error, never the key
        raw = str(exc)
        safe = raw.replace(api_key, "***") if api_key else raw
        return VerifyResult(ok=False, error=safe, kind=ProviderErrorKind.UNKNOWN)
