from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.agents.order_tracking as ot_module
from app.agents.base import DEFAULT_REVEAL_FIELDS, AgentContext
from app.agents.order_tracking import _format_money, run
from app.core.conversation import _recover_order_by_name
from app.providers.base import CompletionResult, Message, ProviderError, ProviderErrorKind
from app.shopify.ad2ship import Ad2shipTracking
from app.shopify.models import AuthorizedOrder, Customer, Fulfillment, LineItem, Money, Order
from app.store.memory import InMemoryIngestStore


def _order(
    name: str,
    phone: str,
    fulfillment_status: str | None = None,
    cancelled_at: str | None = None,
    line_items: tuple[LineItem, ...] = (),
    payment_gateway_names: tuple[str, ...] = ("Cash on Delivery",),
    tags: tuple[str, ...] = (),
    fulfillments: tuple[Fulfillment, ...] = (),
    customer: Customer | None = None,
    created_at: str | None = None,
) -> Order:
    return Order(
        gid=f"gid://{name}", name=name, email="c@example.com", phone=phone,
        shipping_phone=None, billing_phone=None, financial_status="paid",
        fulfillment_status=fulfillment_status, cancelled_at=cancelled_at, tags=tags,
        payment_gateway_names=payment_gateway_names, total=None, customer_locale=None,
        line_items=line_items, fulfillments=fulfillments, customer=customer,
        created_at=created_at,
    )


class _UnownedShopify:
    """Minimal OrderSource whose one order is owned by a DIFFERENT phone, so
    _recover_order_by_name resolves nothing and produces its unmatched-order-number hint.
    Only find_order_by_name is exercised; the rest satisfy the structural shape."""

    def __init__(self, order: Order) -> None:
        self._order = order

    async def get_order(self, gid: str) -> Order | None:
        return None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        return self._order if raw_name == self._order.name else None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


_TRACKED = Fulfillment(
    gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
    tracking_number="AWB0099887766", tracking_url="https://track/AWB0099887766",
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


async def test_prepaid_undispatched_order_is_not_cancel_eligible() -> None:
    """A prepaid order must never read as cancel-eligible, even undispatched -- only COD orders
    are cancellable (owner decision, 2026-08-21)."""
    provider = _CapturingProvider(text='{"reply": "Let me check."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas1", "+919999999999", fulfillment_status="UNFULFILLED",
            payment_gateway_names=(),  # prepaid: no COD gateway, no "cod" tag
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "can i cancel my order", [order]))

    assert provider.captured_messages is not None
    system_msg = next((m for m in provider.captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "cancel eligible: False" in system_msg.content


async def test_prepaid_order_with_cod_tag_is_not_cancel_eligible() -> None:
    """Hardening (security review, 2026-08-22): the cancel-eligibility gate trusts ONLY the
    payment gateway, never an app-writable 'cod' tag. A genuinely prepaid order that also carries
    a 'cod' tag must still read as not cancel-eligible -- the tag alone must not unlock cancel."""
    provider = _CapturingProvider(text='{"reply": "Let me check."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas1", "+919999999999", fulfillment_status="UNFULFILLED",
            payment_gateway_names=(),  # prepaid gateway...
            tags=("cod",),  # ...but an app-writable "cod" tag is present
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "can i cancel my order", [order]))

    assert provider.captured_messages is not None
    system_msg = next((m for m in provider.captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "cancel eligible: False" in system_msg.content


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
    # The financial_status value itself must not render. Match the exact rendered form
    # ("payment status paid") rather than the bare word -- the store cancellation policy text
    # legitimately contains "prepaid", which would collide with a bare "paid" substring check.
    assert "payment status paid" not in prompt


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


async def test_tracking_rendered_when_reveal_includes_tracking_and_order_shipped() -> None:
    # Q10: when the order is shipped and tracking exists, the agent should be able to share the
    # Shopify tracking link/company/number -- so it must reach the prompt.
    provider = _CapturingProvider(text='{"reply": "Here is your tracking."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(_TRACKED,)
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "where is my order", [order]))  # default reveal has tracking

    prompt = _system_prompt(provider)
    assert "AWB0099887766" in prompt
    assert "Delhivery" in prompt
    assert "https://track/AWB0099887766" in prompt


async def test_tracking_withheld_when_not_in_reveal_fields() -> None:
    provider = _CapturingProvider(text='{"reply": "Let me check."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(_TRACKED,)
        ),
        verified_phone="+919999999999",
    )
    # status approved but tracking NOT approved -> tracking details never reach the model.
    reveal = ("order_number", "status")
    await run(_context(provider, "where is my order", [order], reveal_fields=reveal))

    prompt = _system_prompt(provider)
    assert "AWB0099887766" not in prompt
    assert "https://track/AWB0099887766" not in prompt


async def test_prompt_does_not_infer_not_shipped_from_absent_tracking() -> None:
    # A shipped (FULFILLED) order whose tracking row was never captured (scope not granted /
    # historical order not backfilled / admin excluded "tracking") must NOT be described as
    # "not shipped": that contradicts the fulfillment_status line and mis-informs the customer.
    provider = _CapturingProvider(text='{"reply": "Let me check."}')
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999", fulfillment_status="FULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "where is my order", [order]))

    prompt = _system_prompt(provider)
    # The false inference must be gone; the model is told to go by fulfillment status instead.
    assert "has not shipped yet" not in prompt
    assert "fulfillment status" in prompt.lower()


async def test_no_tracking_fabricated_when_order_not_shipped() -> None:
    # An order with no fulfillments has no tracking -- the prompt must not invent a tracking line
    # even though "tracking" is in reveal_fields.
    provider = _CapturingProvider(text='{"reply": "Not shipped yet."}')
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999", fulfillment_status="UNFULFILLED"),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "where is my order", [order]))

    prompt = _system_prompt(provider)
    assert "Tracking" not in prompt or "AWB" not in prompt
    assert "https://track" not in prompt


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


async def test_order_number_format_hint_is_rendered_into_the_prompt() -> None:
    provider = _CapturingProvider(text='{"reply": "Could you double check your order ID?"}')
    hint = "The customer mentioned a number that doesn't match our order ID format."
    context = AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text="my order id is 965",
        history=[], orders=[], is_vip=False, knowledge={}, provider=provider, model="m",
        api_key="k", extra_params=None, order_number_format_hint=hint,
    )

    await run(context)

    assert hint in _system_prompt(provider)


async def test_unmatched_order_number_hint_is_rendered_into_the_prompt() -> None:
    """The hint conversation.py's _recover_order_by_name emits when a shape-valid order number
    doesn't resolve to an owned order (2026-08-23 fix) must reach the prompt exactly like the
    wrong-digit-shape hint already does -- same channel, no new plumbing. The hint is generated
    by the REAL _recover_order_by_name (order owned by a different phone), so a future wording
    change to the generator is caught here -- not just that arbitrary hint text reaches the
    prompt (which test_order_number_format_hint_is_rendered_into_the_prompt already proves)."""
    shopify = _UnownedShopify(_order("tavas6543", "+911111111111"))
    _orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas6543")
    assert hint is not None

    provider = _CapturingProvider(text='{"reply": "I could not find that order."}')
    context = AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text="where is tavas6543",
        history=[], orders=[], is_vip=False, knowledge={}, provider=provider, model="m",
        api_key="k", extra_params=None, order_number_format_hint=hint,
    )

    await run(context)

    assert hint in _system_prompt(provider)
    # The hint must explicitly neutralise the shared HANDOFF_JSON_CONTRACT's own "genuinely
    # cannot answer or resolve" escalation trigger (appended after it), so an unresolved order
    # number never re-arms the 24h AI pause -- code-review fix 2026-08-23.
    assert "keep 'handoff' false for this reason alone" in hint


async def test_absent_order_number_format_hint_is_not_rendered() -> None:
    provider = _CapturingProvider(text='{"reply": "Sure."}')
    await run(_context(provider, "where is my order", []))

    assert "doesn't match our order ID format" not in _system_prompt(provider)


def test_format_money_inr_strips_trailing_zero_cents() -> None:
    assert _format_money(Money(amount="999.00", currency="INR")) == "₹999"


def test_format_money_inr_keeps_non_zero_cents() -> None:
    assert _format_money(Money(amount="499.50", currency="INR")) == "₹499.50"


def test_format_money_non_inr_shows_currency_code() -> None:
    assert _format_money(Money(amount="10.00", currency="USD")) == "10 USD"


async def test_items_revealed_renders_title_variant_and_price() -> None:
    provider = _CapturingProvider(text='{"reply": "Here are your items."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas7",
            "+919999999999",
            fulfillment_status="UNFULFILLED",
            line_items=(
                LineItem(
                    title="Green Chikankari Kurti",
                    quantity=1,
                    variant_title="Green / XL",
                    price=Money(amount="749.00", currency="INR"),
                ),
            ),
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "what did i order", [order]))

    prompt = _system_prompt(provider)
    assert "Green Chikankari Kurti" in prompt
    assert "Green / XL" in prompt
    assert "₹749" in prompt


async def test_items_withheld_from_reveal_fields_omits_item_details() -> None:
    provider = _CapturingProvider(text='{"reply": "Here is your order status."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas7",
            "+919999999999",
            fulfillment_status="UNFULFILLED",
            line_items=(
                LineItem(
                    title="Red Silk Saree",
                    quantity=1,
                    variant_title="Red / L",
                    price=Money(amount="1499.00", currency="INR"),
                ),
            ),
        ),
        verified_phone="+919999999999",
    )
    reveal = tuple(f for f in DEFAULT_REVEAL_FIELDS if f != "items")
    await run(_context(provider, "what did i order", [order], reveal_fields=reveal))

    prompt = _system_prompt(provider)
    assert "Red Silk Saree" not in prompt
    assert "Red / L" not in prompt


async def test_cod_pending_order_renders_cash_on_delivery_note() -> None:
    provider = _CapturingProvider(text='{"reply": "That is normal for COD."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas8",
            "+919999999999",
            fulfillment_status="UNFULFILLED",
            payment_gateway_names=("Cash on Delivery (COD)",),
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "why is my payment pending", [order]))

    assert "(Cash on Delivery)" in _system_prompt(provider)


async def test_non_cod_order_has_no_cash_on_delivery_note() -> None:
    provider = _CapturingProvider(text='{"reply": "Your payment went through."}')
    order = AuthorizedOrder(
        order=_order(
            "tavas9", "+919999999999", fulfillment_status="UNFULFILLED",
            payment_gateway_names=(),  # prepaid: no COD gateway
        ),
        verified_phone="+919999999999",
    )
    await run(_context(provider, "is my payment done", [order]))

    assert "(Cash on Delivery)" not in _system_prompt(provider)


def _customer(state: str = "Gujarat") -> Customer:
    return Customer(
        gid="gid://c/1", first_name=None, last_name=None, email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=state,
        postal_code=None, country=None,
    )


async def test_order_line_includes_estimated_delivery_when_computable() -> None:
    provider = _CapturingProvider('{"reply": "ok"}')
    # created "today" (whenever the suite runs) so the formula date always lands in the future,
    # regardless of run date -- order_tracking.py computes "today" from the real clock.
    created_at = datetime.now(UTC).date().isoformat() + "T00:00:00+00:00"
    order = _order(
        "tavas1", "+919999999999",
        customer=_customer("Gujarat"), created_at=created_at,
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "when will my order arrive", [authorized]))
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" in prompt
    assert "estimate" in prompt.lower() and "1-2 days" in prompt.replace("1–2 days", "1-2 days")


async def test_order_line_omits_estimate_for_mirror_delivered_order() -> None:
    # Mirror-shaped "delivered" order: fulfillment_status is FULFILLED but no fulfillment carries
    # a delivered_at timestamp (the REST fulfillment webhook never populates it -- see
    # app/channels/shopify_orders.py). created_at is old enough that the raw formula date would
    # already be in the past. The past-date suppression in estimate_delivery must still keep this
    # off the prompt so the customer is never told a stale/past "estimated delivery" date.
    provider = _CapturingProvider('{"reply": "ok"}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED",
        customer=_customer("Gujarat"), created_at="2026-07-01T00:00:00+00:00",
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "when will my order arrive", [authorized]))
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" not in prompt


async def test_order_line_omits_estimated_delivery_without_created_at() -> None:
    provider = _CapturingProvider('{"reply": "ok"}')
    order = _order("tavas1", "+919999999999", customer=_customer("Gujarat"), created_at=None)
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "when will my order arrive", [authorized]))
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" not in prompt


async def test_order_line_omits_estimated_delivery_when_status_not_revealed() -> None:
    provider = _CapturingProvider('{"reply": "ok"}')
    order = _order(
        "tavas1", "+919999999999",
        customer=_customer("Gujarat"), created_at="2026-08-10T00:00:00+00:00",
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(
        _context(
            provider, "when will my order arrive", [authorized],
            reveal_fields=("order_number",),
        )
    )
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" not in prompt


# --- Task 7: live ad2ship tracking enrichment ---------------------------------------------

_IN_TRANSIT = Ad2shipTracking(
    status="in_transit",
    status_label="In Transit",
    current_city="Maharashtra",
    current_hub="Mumbai_Hub",
    last_scan="In Transit",
    last_scan_remark="Shipment in transit",
    last_scan_at="2026-08-31 10:00",
    expected_date="2026-09-02",
)


class _StubFetch:
    """Records call count and returns a canned result, standing in for fetch_tracking."""

    def __init__(self, result: Ad2shipTracking | None) -> None:
        self._result = result
        self.calls = 0

    async def __call__(
        self, http: httpx.AsyncClient, awb: str, *, timeout: float = 4.0
    ) -> Ad2shipTracking | None:
        self.calls += 1
        return self._result


def _dead_client() -> httpx.AsyncClient:
    # A real HTTP hit would 500 -- proving the live path only ever goes through the monkeypatched
    # fetch_tracking, never the transport directly.
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500)))


async def _run_with_live(
    provider: _CapturingProvider,
    user_text: str,
    orders: list[AuthorizedOrder],
    stub: _StubFetch,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reveal_fields: tuple[str, ...] = DEFAULT_REVEAL_FIELDS,
    ingest: InMemoryIngestStore | None = None,
) -> InMemoryIngestStore:
    monkeypatch.setattr(ot_module, "fetch_tracking", stub)
    store = ingest if ingest is not None else InMemoryIngestStore()
    async with _dead_client() as http:
        await run(_context(provider, user_text, orders, reveal_fields), http, store)
    return store


async def test_live_tracking_fetched_and_rendered_for_in_flight_fulfillment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CapturingProvider(text='{"reply": "On its way."}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(_TRACKED,)
    )
    ingest = InMemoryIngestStore()
    await ingest.upsert_order_mirror(order)
    await ingest.upsert_fulfillment(order.gid, _TRACKED)
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    stub = _StubFetch(_IN_TRANSIT)

    store = await _run_with_live(
        provider, "where is my order", [authorized], stub, monkeypatch, ingest=ingest
    )

    prompt = _system_prompt(provider)
    assert "Current status: In Transit" in prompt
    assert "Currently at: Mumbai_Hub" in prompt
    assert stub.calls == 1
    # The fetched snapshot was persisted onto the mirror fulfillment.
    stored = store.fulfillments[order.gid][_TRACKED.gid]
    assert stored.shipment_status == "in_transit"
    assert stored.tracking_city == "Maharashtra"


async def test_live_tracking_uses_fresh_cache_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = Fulfillment(
        gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
        tracking_number="AWB0099887766", tracking_url="https://track/AWB0099887766",
        shipment_status="in_transit",
        tracking_checked_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        tracking_city="Pune", tracking_hub="Pune_Hub",
        tracking_last_scan="Reached hub", tracking_expected_date="2026-09-01",
    )
    provider = _CapturingProvider(text='{"reply": "On its way."}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(fresh,)
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    stub = _StubFetch(_IN_TRANSIT)

    await _run_with_live(provider, "where is my order", [authorized], stub, monkeypatch)

    prompt = _system_prompt(provider)
    assert stub.calls == 0  # fresh cache -> no live courier call
    assert "Current status: In Transit" in prompt
    assert "Currently at: Pune_Hub" in prompt
    assert "Expected delivery: 2026-09-01" in prompt


async def test_live_tracking_none_result_leaves_static_line_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CapturingProvider(text='{"reply": "Here is your tracking."}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(_TRACKED,)
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    stub = _StubFetch(None)

    await _run_with_live(provider, "where is my order", [authorized], stub, monkeypatch)

    prompt = _system_prompt(provider)
    assert "Current status:" not in prompt
    # The existing static Shopify tracking line is untouched.
    assert "AWB0099887766" in prompt
    assert "https://track/AWB0099887766" in prompt


async def test_live_tracking_skipped_when_tracking_not_revealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CapturingProvider(text='{"reply": "Let me check."}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(_TRACKED,)
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    stub = _StubFetch(_IN_TRANSIT)

    await _run_with_live(
        provider, "where is my order", [authorized], stub, monkeypatch,
        reveal_fields=("order_number", "status"),
    )

    prompt = _system_prompt(provider)
    assert stub.calls == 0
    assert "Current status:" not in prompt


async def test_live_tracking_skipped_for_terminal_shipment_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rto = Fulfillment(
        gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
        tracking_number="AWB0099887766", tracking_url="https://track/AWB0099887766",
        shipment_status="rto",
        tracking_city="Surat", tracking_hub="Surat_Hub",
        tracking_last_scan="Returned to origin",
    )
    provider = _CapturingProvider(text='{"reply": "Let me check."}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(rto,)
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    stub = _StubFetch(_IN_TRANSIT)

    await _run_with_live(provider, "where is my order", [authorized], stub, monkeypatch)

    prompt = _system_prompt(provider)
    assert stub.calls == 0  # terminal -> answered from stored state, no live call
    assert "Currently at: Surat_Hub" in prompt
    assert "AWB0099887766" in prompt


async def test_no_live_lines_when_http_and_ingest_absent() -> None:
    # Calling run(context) exactly like the 29 existing call sites: no live enrichment at all.
    provider = _CapturingProvider(text='{"reply": "Here is your tracking."}')
    order = _order(
        "tavas1", "+919999999999", fulfillment_status="FULFILLED", fulfillments=(_TRACKED,)
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")

    await run(_context(provider, "where is my order", [authorized]))

    prompt = _system_prompt(provider)
    assert "Current status:" not in prompt
    assert "AWB0099887766" in prompt  # static line unchanged
