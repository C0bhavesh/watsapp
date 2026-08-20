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
    assert history[0].content in sent_contents
    assert history[1].content in sent_contents


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


async def test_router_prompt_assigns_order_delivery_timing_to_order_tracking() -> None:
    """A bare delivery-timing question or short follow-up about an order the customer has
    already placed ("Delivery", "when will it arrive", "any time", "how long") must route to
    order_tracking, not policy -- otherwise conversation.py never resolves the order and the
    delivery-date estimate never fires (2026-08-20 misroute bug). The classification is a live
    LLM judgment (mocked in the unit tests above), so this asserts the prompt guidance that
    steers it: order_tracking explicitly owns an existing order's delivery/arrival timing."""
    from app.agents.router import _ROUTER_PROMPT

    lowered = _ROUTER_PROMPT.lower()
    # order_tracking must own "when will it arrive / how long delivery takes" for a placed order.
    assert "arrive" in lowered
    assert "delivery" in lowered
    # The bare short-follow-up examples the misroute hit must be named as order_tracking cues.
    assert "any time" in lowered


async def test_router_prompt_scopes_policy_to_general_store_policy_only() -> None:
    """policy must stay scoped to GENERAL/abstract store policy (return/exchange/refund rules,
    COD availability, shipping charges, whether the store ships somewhere) and explicitly NOT
    claim the delivery timing of an order the customer has already placed -- that exclusion is
    what stops a bare "Delivery" follow-up from being swallowed as policy again."""
    from app.agents.router import _ROUTER_PROMPT

    lowered = _ROUTER_PROMPT.lower()
    # Genuine policy topics stay in policy.
    assert "return" in lowered
    assert "cod" in lowered
    # The exclusion carving order-timing OUT of policy must be present.
    assert "already placed" in lowered


def test_router_prompt_routes_exchange_requests_to_the_exchange_intent() -> None:
    from app.agents.router import _ROUTER_PROMPT

    assert "exchange:" in _ROUTER_PROMPT
    assert "different size" in _ROUTER_PROMPT


def test_router_prompt_excludes_damaged_incorrect_items_from_exchange() -> None:
    from app.agents.router import _ROUTER_PROMPT

    assert "damaged" in _ROUTER_PROMPT.lower()
    assert "customer_support" in _ROUTER_PROMPT


def test_router_prompt_policy_no_longer_claims_actual_exchange_requests() -> None:
    # policy still owns the ABSTRACT exchange-policy question; it must explicitly exclude a
    # customer actually wanting to exchange their own order now that `exchange` exists.
    from app.agents.router import _ROUTER_PROMPT

    policy_bullet_start = _ROUTER_PROMPT.index("- policy:")
    exchange_intent_mention = _ROUTER_PROMPT.index("that is `exchange`")
    assert policy_bullet_start < exchange_intent_mention
