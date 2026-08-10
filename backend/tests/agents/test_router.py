from app.agents.router import classify_intent
from app.providers.base import CompletionResult, ProviderError, ProviderErrorKind


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


async def test_classify_intent_order_tracking() -> None:
    provider = _FixedProvider(text='{"intent": "order_tracking"}')
    result = await classify_intent(provider, "m", "k", "where is my order")
    assert result == "order_tracking"


async def test_classify_intent_product_search() -> None:
    provider = _FixedProvider(text='{"intent": "product_search"}')
    result = await classify_intent(provider, "m", "k", "do you have this in blue")
    assert result == "product_search"


async def test_classify_intent_policy() -> None:
    provider = _FixedProvider(text='{"intent": "policy"}')
    result = await classify_intent(provider, "m", "k", "what is your return policy")
    assert result == "policy"


async def test_classify_intent_recommendations() -> None:
    provider = _FixedProvider(text='{"intent": "recommendations"}')
    result = await classify_intent(provider, "m", "k", "what goes well with a red kurti")
    assert result == "recommendations"


async def test_classify_intent_unknown_value_falls_back_to_customer_support() -> None:
    provider = _FixedProvider(text='{"intent": "make_me_a_sandwich"}')
    result = await classify_intent(provider, "m", "k", "hi")
    assert result == "customer_support"


async def test_classify_intent_unparseable_falls_back_to_customer_support() -> None:
    provider = _FixedProvider(text="not json")
    result = await classify_intent(provider, "m", "k", "hi")
    assert result == "customer_support"


async def test_classify_intent_provider_error_falls_back_to_customer_support() -> None:
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await classify_intent(provider, "m", "k", "hi")
    assert result == "customer_support"


async def test_classify_intent_uses_history_to_resolve_a_bare_number_reply() -> None:
    """A bare number carries no signal on its own -- with the prior assistant turn asking for
    an order number, that context must reach the provider so the model can use it."""
    from app.providers.base import Message

    seen: dict[str, object] = {}

    class _RecordingProvider:
        async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
            seen["messages"] = messages
            return CompletionResult(text='{"intent": "order_tracking"}', model=model)

    history = [
        Message(role="user", content="can u tell me my order detail"),
        Message(
            role="assistant",
            content=(
                "It looks like there isn't an order linked yet. Could you share your "
                "order number?"
            ),
        ),
    ]
    result = await classify_intent(_RecordingProvider(), "m", "k", "9652", history=history)

    assert result == "order_tracking"
    sent_contents = [m.content for m in seen["messages"]]
    assert any("order number" in c for c in sent_contents)


async def test_classify_intent_caps_history_to_the_configured_window() -> None:
    """Only the most recent few history messages are sent -- the router stays a fast, cheap
    classification call, not a full-context one."""
    from app.agents.router import _HISTORY_MESSAGES_FOR_ROUTING
    from app.providers.base import Message

    seen: dict[str, object] = {}

    class _RecordingProvider:
        async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
            seen["messages"] = messages
            return CompletionResult(text='{"intent": "customer_support"}', model=model)

    history = [Message(role="user", content=f"msg{i}") for i in range(10)]
    await classify_intent(_RecordingProvider(), "m", "k", "hi", history=history)

    # system prompt + capped history + the current message
    assert len(seen["messages"]) == 1 + _HISTORY_MESSAGES_FOR_ROUTING + 1


async def test_classify_intent_with_no_history_still_works() -> None:
    provider = _FixedProvider(text='{"intent": "order_tracking"}')
    result = await classify_intent(provider, "m", "k", "where is my order", history=None)
    assert result == "order_tracking"
