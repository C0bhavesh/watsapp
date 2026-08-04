"""LiteLLM adapter — minimal Phase 3.5 version (key verification only).

litellm is imported LAZILY inside complete(): the admin panel is the only Phase 3.5
caller and the webhook cold path must not pay litellm's import cost (rule F10).
"""

from dataclasses import dataclass

from app.providers.base import CompletionResult, Message, ProviderError, ProviderErrorKind

_STATUS_TO_KIND: dict[int, ProviderErrorKind] = {
    401: ProviderErrorKind.AUTH,
    403: ProviderErrorKind.AUTH,
    404: ProviderErrorKind.NOT_FOUND,
    408: ProviderErrorKind.TIMEOUT,
    429: ProviderErrorKind.RATE_LIMIT,
}


@dataclass(frozen=True)
class VertexConfig:
    """Vertex AI credentials sourced from env settings (never the config DB)."""

    credentials_json: str | None
    project: str | None
    location: str


def _classify(exc: BaseException) -> ProviderErrorKind:
    status: object = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _STATUS_TO_KIND:
        return _STATUS_TO_KIND[status]
    return ProviderErrorKind.UNKNOWN


def _redact(msg: str, api_key: str, model: str, vertex: "VertexConfig | None") -> str:
    """Scrub both the api_key and (for vertex models) the service-account JSON from an error."""
    if model.startswith("vertex_ai/") and vertex and vertex.credentials_json:
        msg = msg.replace(vertex.credentials_json, "***")
    if api_key:
        msg = msg.replace(api_key, "***")
    return msg


class LiteLLMProvider:
    def __init__(self, vertex: VertexConfig | None = None) -> None:
        self._vertex = vertex

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
        if model.startswith("vertex_ai/"):
            v = self._vertex
            if v is None or not v.credentials_json or not v.project:
                raise ProviderError(
                    "Vertex AI credentials are not configured", ProviderErrorKind.AUTH
                )
            # Vertex authenticates with the service-account JSON + project + location,
            # NOT an api_key — omit api_key entirely for vertex_ai/* models.
            call_kwargs["vertex_credentials"] = v.credentials_json
            call_kwargs["vertex_project"] = v.project
            call_kwargs["vertex_location"] = v.location
        else:
            call_kwargs["api_key"] = api_key
        try:
            resp = await litellm.acompletion(
                model=model, messages=msg_dicts, timeout=timeout, **call_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — every upstream error becomes ProviderError
            raise ProviderError(
                _redact(str(exc), api_key, model, self._vertex), _classify(exc)
            ) from exc
        try:
            text = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        return CompletionResult(text=text, model=model)
