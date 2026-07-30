"""LiteLLM adapter — minimal Phase 3.5 version (key verification only).

litellm is imported LAZILY inside complete(): the admin panel is the only Phase 3.5
caller and the webhook cold path must not pay litellm's import cost (rule F10).
"""

from app.providers.base import CompletionResult, Message, ProviderError, ProviderErrorKind

_STATUS_TO_KIND: dict[int, ProviderErrorKind] = {
    401: ProviderErrorKind.AUTH,
    403: ProviderErrorKind.AUTH,
    404: ProviderErrorKind.NOT_FOUND,
    408: ProviderErrorKind.TIMEOUT,
    429: ProviderErrorKind.RATE_LIMIT,
}


def _classify(exc: BaseException) -> ProviderErrorKind:
    status: object = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _STATUS_TO_KIND:
        return _STATUS_TO_KIND[status]
    return ProviderErrorKind.UNKNOWN


def _redact(msg: str, api_key: str) -> str:
    return msg.replace(api_key, "***") if api_key else msg


class LiteLLMProvider:
    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        import litellm  # lazy: never on the webhook cold path

        # httpx transport avoids stale-keepalive spurious timeouts on Vercel (cafe fix)
        litellm.disable_aiohttp_transport = True
        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
        call_kwargs: dict[str, object] = dict(extra_params or {})
        call_kwargs["api_key"] = api_key
        try:
            resp = await litellm.acompletion(
                model=model, messages=msg_dicts, timeout=timeout, **call_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — every upstream error becomes ProviderError
            raise ProviderError(_redact(str(exc), api_key), _classify(exc)) from exc
        try:
            text = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        return CompletionResult(text=text, model=model)
