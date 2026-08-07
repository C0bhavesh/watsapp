from app.agents.base import DEFAULT_REVEAL_FIELDS, AgentContext
from app.agents.order_tracking import run
from app.providers.base import CompletionResult, Message, ProviderError, ProviderErrorKind
from app.shopify.models import AuthorizedOrder, Order


def _order(
    name: str, phone: str, fulfillment_status: str | None = None, cancelled_at: str | None = None
) -> Order:
    return Order(
        gid=f"gid://{name}", name=name, email="c@example.com", phone=phone,
        shipping_phone=None, billing_phone=None, financial_status="paid",
        fulfillment_status=fulfillment_status, cancelled_at=cancelled_at, tags=(),
        payment_gateway_names=(), total=None, customer_locale=None,
    )


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


class _CapturingProvider:
    """Provider that captures messages passed to complete() for assertion."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.captured_messages: list[Message] | None = None

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.captured_messages = messages
        return CompletionResult(text=self._text, model=model)


def _context(
    provider: _FixedProvider | _CapturingProvider,
    user_text: str,
    orders: list[AuthorizedOrder],
    reveal_fields: tuple[str, ...] = DEFAULT_REVEAL_FIELDS,
) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=orders, is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None, reveal_fields=reveal_fields,
    )


def _system_prompt(provider: _CapturingProvider) -> str:
    assert provider.captured_messages is not None
    system_msg = next((m for m in provider.captured_messages if m.role == "system"), None)
    assert system_msg is not None
    return system_msg.content


async def test_run_returns_parsed_reply() -> None:
    provider = _FixedProvider(text='{"reply": "Your order tavas1 is confirmed and on its way."}')
    order = AuthorizedOrder(order=_order("tavas1", "+919999999999"), verified_phone="+919999999999")
    result = await run(_context(provider, "where is my order", [order]))
    assert result.text == "Your order tavas1 is confirmed and on its way."


async def test_run_with_no_orders_asks_for_order_number() -> None:
    provider = _FixedProvider(text='{"reply": "Could you share your order number?"}')
    result = await run(_context(provider, "where is my order", []))
    assert "order number" in result.text.lower()


async def test_run_on_provider_error_returns_safe_fallback() -> None:
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await run(_context(provider, "where is my order", []))
    assert "team" in result.text


async def test_unfulfilled_order_is_cancel_eligible() -> None:
    """Verify UNFULFILLED orders marked as cancel-eligible in system prompt."""
    provider = _CapturingProvider(text='{"reply": "Your order is eligible."}')
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999", fulfillment_status="UNFULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "can i cancel my order", [order]))

    # Extract the system prompt (first message with role="system")
    assert provider.captured_messages is not None
    system_msg = next((m for m in provider.captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "cancel eligible: True" in system_msg.content


async def test_fulfilled_order_is_not_cancel_eligible() -> None:
    """Verify FULFILLED orders marked as not cancel-eligible (dispatched)."""
    provider = _CapturingProvider(text='{"reply": "Too late to cancel."}')
    order = AuthorizedOrder(
        order=_order("tavas2", "+919999999999", fulfillment_status="FULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "can i cancel my order", [order]))

    assert provider.captured_messages is not None
    system_msg = next((m for m in provider.captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "cancel eligible: False" in system_msg.content


async def test_already_cancelled_order_is_not_eligible() -> None:
    """Verify orders with cancelled_at marked as not cancel-eligible."""
    provider = _CapturingProvider(text='{"reply": "Already cancelled."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas3",
            "+919999999999",
            fulfillment_status="UNFULFILLED",
            cancelled_at="2026-08-01T10:00:00Z",
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "can i cancel my order", [order]))

    assert provider.captured_messages is not None
    system_msg = next((m for m in provider.captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "cancel eligible: False" in system_msg.content
    assert "cancelled: True" in system_msg.content


async def test_reveal_fields_without_status_withholds_status_and_cancellation() -> None:
    """AdminControls.reveal_fields is a disclosure control, so it must gate what reaches the
    prompt -- an admin who unticks "status" must not have payment/fulfillment/cancellation
    state handed to the model anyway."""
    provider = _CapturingProvider(text='{"reply": "Let me check on that."}')
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999", fulfillment_status="FULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "where is my order", [order], reveal_fields=("order_number",)))

    prompt = _system_prompt(provider)
    assert "tavas1" in prompt  # order_number IS approved
    assert "payment status" not in prompt
    assert "fulfillment FULFILLED" not in prompt
    assert "cancelled: " not in prompt
    assert "cancel eligible" not in prompt
    assert "paid" not in prompt  # the financial_status value itself


async def test_reveal_fields_without_order_number_withholds_the_order_name() -> None:
    provider = _CapturingProvider(text='{"reply": "Let me check on that."}')
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999", fulfillment_status="UNFULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "where is my order", [order], reveal_fields=("status",)))

    prompt = _system_prompt(provider)
    assert "tavas1" not in prompt
    assert "cancel eligible: True" in prompt  # status IS approved


async def test_reveal_fields_empty_withholds_every_order_detail() -> None:
    provider = _CapturingProvider(text='{"reply": "Let me check on that."}')
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999", fulfillment_status="UNFULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "where is my order", [order], reveal_fields=()))

    prompt = _system_prompt(provider)
    assert "tavas1" not in prompt
    assert "payment status" not in prompt
    assert "cancel eligible" not in prompt


async def test_model_handoff_flag_is_honored() -> None:
    """order_tracking's prompt can end in "let me connect you with the team"; that has to set
    handoff so core.conversation actually pauses the AI for a human."""
    provider = _FixedProvider(
        text='{"reply": "Let me connect you with our team.", "handoff": true}'
    )
    result = await run(_context(provider, "this is still not delivered", []))
    assert result.handoff is True


async def test_reply_without_handoff_flag_does_not_hand_off() -> None:
    provider = _FixedProvider(text='{"reply": "Your order tavas1 is on its way."}')
    result = await run(_context(provider, "where is my order", []))
    assert result.handoff is False


async def test_system_prompt_requests_the_handoff_field() -> None:
    provider = _CapturingProvider(text='{"reply": "Sure.", "handoff": false}')
    await run(_context(provider, "where is my order", []))
    assert '"handoff"' in _system_prompt(provider)
