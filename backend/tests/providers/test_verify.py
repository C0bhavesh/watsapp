from app.providers.base import (
    CompletionResult,
    Message,
    ProviderError,
    ProviderErrorKind,
)
from app.providers.verify import verify_key


class FakeProvider:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.seen_extra: dict[str, object] | None = None

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.seen_extra = extra_params
        if self._exc:
            raise self._exc
        return CompletionResult(text="pong", model=model)


async def test_verify_ok() -> None:
    r = await verify_key(FakeProvider(), "gemini/x", "KEY123")
    assert r.ok is True and r.error is None


async def test_verify_auth_error_redacts_key() -> None:
    exc = ProviderError("bad key KEY123", ProviderErrorKind.AUTH)
    r = await verify_key(FakeProvider(exc), "gemini/x", "KEY123")
    assert r.ok is False and r.kind is ProviderErrorKind.AUTH
    assert "KEY123" not in (r.error or "")


async def test_verify_unexpected_exception_is_unknown() -> None:
    r = await verify_key(FakeProvider(RuntimeError("boom KEY123")), "gemini/x", "KEY123")
    assert r.ok is False and r.kind is ProviderErrorKind.UNKNOWN
    assert "KEY123" not in (r.error or "")


async def test_verify_passes_extra_params_through() -> None:
    provider = FakeProvider()
    params = {"temperature": 0.3, "reasoning_effort": "medium"}
    r = await verify_key(provider, "vertex_ai/gemini-3.5-flash", "", extra_params=params)
    assert r.ok is True
    assert provider.seen_extra == params
