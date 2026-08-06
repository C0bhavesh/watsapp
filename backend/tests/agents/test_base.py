from typing import Any

from app.agents.base import AgentContext, extract_json_blob, extract_reply_text
from app.providers.base import CompletionResult, Message


class _StubProvider:
    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        return CompletionResult(text="", model=model)


def _context(**overrides: Any) -> AgentContext:
    kwargs: dict[str, Any] = {
        "wa_id": "919999999999",
        "phone_e164": "+919999999999",
        "user_text": "hi",
        "history": [],
        "orders": [],
        "is_vip": False,
        "knowledge": {},
        "provider": _StubProvider(),
        "model": "m",
        "api_key": "k",
        "extra_params": None,
    }
    kwargs.update(overrides)
    return AgentContext(**kwargs)


def test_agent_context_repr_never_leaks_the_api_key() -> None:
    """A traceback/exception-logger locals capture must never print a plaintext key."""
    provider = _StubProvider()
    context = _context(provider=provider, api_key="super-secret-llm-key")
    assert "super-secret-llm-key" not in repr(context)
    # repr=False must preserve value equality (same guarantee as WhatsAppConfig).
    assert context == _context(provider=provider, api_key="super-secret-llm-key")
    assert context != _context(provider=provider, api_key="another-key")


def test_extract_json_blob_direct() -> None:
    assert extract_json_blob('{"reply": "hi"}') == {"reply": "hi"}


def test_extract_json_blob_strips_think_and_fence() -> None:
    text = '<think>reasoning</think>```json\n{"reply": "hi there"}\n```'
    assert extract_json_blob(text) == {"reply": "hi there"}


def test_extract_json_blob_extracts_outer_braces_from_prose() -> None:
    text = 'Sure, here is my answer: {"reply": "ok"} -- hope that helps.'
    assert extract_json_blob(text) == {"reply": "ok"}


def test_extract_json_blob_non_json_returns_none() -> None:
    assert extract_json_blob("just plain text, no JSON here") is None


def test_extract_json_blob_json_array_returns_none() -> None:
    assert extract_json_blob("[1, 2, 3]") is None


def test_extract_reply_text_from_valid_json() -> None:
    assert extract_reply_text('{"reply": "Your order is confirmed."}', "fallback") == (
        "Your order is confirmed."
    )


def test_extract_reply_text_falls_back_on_missing_reply_key() -> None:
    assert extract_reply_text('{"other": "x"}', "fallback") == "fallback"


def test_extract_reply_text_tolerates_plain_text_completion() -> None:
    assert extract_reply_text("Sure, happy to help!", "fallback") == "Sure, happy to help!"


def test_extract_reply_text_empty_completion_falls_back() -> None:
    assert extract_reply_text("   ", "fallback") == "fallback"


def test_extract_reply_text_falls_back_on_broken_fenced_json() -> None:
    text = '```json\n{reply: "broken}\n```'
    assert extract_reply_text(text, "FALLBACK") == "FALLBACK"


def test_extract_reply_text_falls_back_on_trailing_comma_json() -> None:
    assert extract_reply_text('{"reply": "hi",}', "FALLBACK") == "FALLBACK"


def test_extract_reply_text_falls_back_on_unquoted_key_json() -> None:
    text = '{reply: "hi there, unquoted key"}'
    assert extract_reply_text(text, "FALLBACK") == "FALLBACK"
