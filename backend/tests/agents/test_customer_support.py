from datetime import UTC, datetime, timedelta

from app.agents.base import AgentContext
from app.agents.customer_support import run
from app.providers.base import CompletionResult, Message, ProviderError, ProviderErrorKind
from app.store.memory import InMemoryConversationStore


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises
        self.called = False

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.called = True
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


def _context(provider: _FixedProvider, user_text: str) -> AgentContext:
    return AgentContext(
        wa_id="919999999999",
        phone_e164="+919999999999",
        user_text=user_text,
        history=[],
        orders=[],
        is_vip=False,
        knowledge={},
        provider=provider,
        model="m",
        api_key="k",
        extra_params=None,
    )


async def test_greeting_replies_normally_no_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(text='{"reply": "Hello! How can I help you today?"}')
    result = await run(_context(provider, "hi"), store, conversation_id, datetime.now(UTC))
    assert result.handoff is False
    assert await store.get_paused_until(conversation_id) is None


async def test_first_human_request_gets_one_attempt_not_immediate_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(text='{"reply": "I will do my best to help -- what do you need?"}')
    result = await run(
        _context(provider, "I want to talk to a human"), store, conversation_id, datetime.now(UTC)
    )
    assert result.handoff is False
    assert await store.get_handoff_attempted_at(conversation_id) is not None
    assert await store.get_paused_until(conversation_id) is None
    assert provider.called is True


async def test_second_human_request_triggers_immediate_handoff_no_llm_call() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    now = datetime.now(UTC)
    await store.mark_handoff_attempted(conversation_id, now - timedelta(minutes=5))
    provider = _FixedProvider(text="should not be called for a decisive second ask")
    result = await run(_context(provider, "I need a real person"), store, conversation_id, now)
    assert result.handoff is True
    paused = await store.get_paused_until(conversation_id)
    assert paused is not None and paused > now
    assert provider.called is False


async def test_provider_failure_triggers_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await run(_context(provider, "hi"), store, conversation_id, datetime.now(UTC))
    assert result.handoff is True
    assert await store.get_paused_until(conversation_id) is not None


async def test_handoff_attempt_outside_24h_window_resets() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    now = datetime.now(UTC)
    await store.mark_handoff_attempted(conversation_id, now - timedelta(hours=25))
    provider = _FixedProvider(text='{"reply": "Sure, let me help."}')
    result = await run(_context(provider, "talk to a human"), store, conversation_id, now)
    assert result.handoff is False
    assert provider.called is True


async def test_safe_fallback_degradation_also_triggers_handoff() -> None:
    """The LLM's own fallback (unparseable/broken JSON reply) is "could not help" too."""
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(text="```{not valid json")
    result = await run(_context(provider, "hi"), store, conversation_id, datetime.now(UTC))
    assert result.handoff is True
    assert await store.get_paused_until(conversation_id) is not None
