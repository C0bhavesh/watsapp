from app.agents.base import DEFAULT_REVEAL_FIELDS, AgentContext
from app.agents.exchange import run
from app.core.exchange_models import ExchangeRequest
from app.providers.base import CompletionResult, Message
from app.shopify.models import AuthorizedOrder, Fulfillment, Order


class _FakeExchangeStore:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []
        self._next_id = 1

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest:
        self.created.append((order_gid, order_name, phone_e164, requested_size))
        row = ExchangeRequest(
            id=self._next_id, order_gid=order_gid, order_name=order_name,
            phone_e164=phone_e164, requested_size=requested_size, status="requested",
            requested_at="2026-08-20T00:00:00+00:00", return_tracking_url=None,
            updated_at="2026-08-20T00:00:00+00:00",
        )
        self._next_id += 1
        return row

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
        return []

    async def get(self, id: int) -> ExchangeRequest | None:
        return None

    async def set_status(self, id: int, status: str) -> None:
        pass

    async def set_return_tracking_url(self, id: int, url: str) -> None:
        pass


class _CapturingProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.captured_messages: list[Message] | None = None

    async def complete(
        self, model: str, messages: list[Message], api_key: str, timeout: float, *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.captured_messages = messages
        return CompletionResult(text=self._text, model=model)


def _order(
    gid: str = "gid://o/1", name: str = "tavas1", phone: str = "+919999999999",
    cancelled_at: str | None = None,
    fulfillments: tuple[Fulfillment, ...] = (
        Fulfillment(
            gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
            tracking_number="AWB1", tracking_url="https://track/AWB1",
            delivered_at="2026-08-19T00:00:00+00:00",
        ),
    ),
) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status="paid", fulfillment_status="FULFILLED",
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, fulfillments=fulfillments,
    )


def _context(
    provider: _CapturingProvider, user_text: str, orders: list[AuthorizedOrder],
    exchange_requests: list[ExchangeRequest] | None = None,
) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=orders, is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None, reveal_fields=DEFAULT_REVEAL_FIELDS,
        exchange_requests=exchange_requests or [],
    )


async def test_prompt_includes_eligible_fact_for_a_recently_delivered_order() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "I want to exchange this", [authorized]), _FakeExchangeStore())
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "within the 48-hour exchange window" in prompt


async def test_prompt_includes_ineligible_fact_for_an_old_order() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order(
        fulfillments=(
            Fulfillment(
                gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
                tracking_number="AWB1", tracking_url="https://track/AWB1",
                delivered_at="2026-01-01T00:00:00+00:00",
            ),
        ),
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "I want to exchange this", [authorized]), _FakeExchangeStore())
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "outside the 48-hour exchange window" in prompt


async def test_prompt_states_size_only_no_color_or_product() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "I want to exchange this", [authorized]), _FakeExchangeStore())
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "size" in prompt.lower()
    assert "color" in prompt.lower() or "colour" in prompt.lower()


async def test_create_exchange_for_an_eligible_known_order_creates_a_record() -> None:
    store = _FakeExchangeStore()
    order = _order(gid="gid://o/42", name="tavas42")
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    reply = (
        '{"reply": "Done!", "handoff": false, '
        '"create_exchange": {"order_gid": "gid://o/42", "size": "M"}}'
    )
    provider = _CapturingProvider(reply)
    result = await run(_context(provider, "size M please", [authorized]), store)
    assert store.created == [("gid://o/42", "tavas42", "+919999999999", "M")]
    assert result.text == "Done!"


async def test_create_exchange_for_an_ineligible_order_is_silently_ignored() -> None:
    store = _FakeExchangeStore()
    order = _order(
        gid="gid://o/42", name="tavas42",
        fulfillments=(
            Fulfillment(
                gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
                tracking_number="AWB1", tracking_url="https://track/AWB1",
                delivered_at="2026-01-01T00:00:00+00:00",  # long past the 48h window
            ),
        ),
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    reply = (
        '{"reply": "Done!", "handoff": false, '
        '"create_exchange": {"order_gid": "gid://o/42", "size": "M"}}'
    )
    provider = _CapturingProvider(reply)
    await run(_context(provider, "size M please", [authorized]), store)
    assert store.created == []


async def test_create_exchange_for_an_unknown_order_gid_is_silently_ignored() -> None:
    store = _FakeExchangeStore()
    order = _order(gid="gid://o/42", name="tavas42")
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    reply = (
        '{"reply": "Done!", "handoff": false, '
        '"create_exchange": {"order_gid": "gid://o/DOES-NOT-EXIST", "size": "M"}}'
    )
    provider = _CapturingProvider(reply)
    await run(_context(provider, "size M please", [authorized]), store)
    assert store.created == []


async def test_no_create_exchange_field_does_not_create_a_record() -> None:
    store = _FakeExchangeStore()
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    provider = _CapturingProvider('{"reply": "Which size?", "handoff": false}')
    await run(_context(provider, "I want to exchange this", [authorized]), store)
    assert store.created == []


async def test_existing_exchange_requests_are_rendered_for_status_questions() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    existing = ExchangeRequest(
        id=7, order_gid="gid://o/1", order_name="tavas1", phone_e164="+919999999999",
        requested_size="M", status="return_picked_up", requested_at="2026-08-19T00:00:00+00:00",
        return_tracking_url="https://track/return1", updated_at="2026-08-19T12:00:00+00:00",
    )
    await run(
        _context(provider, "where is my exchange", [authorized], exchange_requests=[existing]),
        _FakeExchangeStore(),
    )
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "return_picked_up" in prompt
    assert "https://track/return1" in prompt
