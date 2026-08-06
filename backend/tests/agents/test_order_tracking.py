from app.agents.base import AgentContext
from app.agents.order_tracking import run
from app.providers.base import CompletionResult, ProviderError, ProviderErrorKind
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
        messages: list,
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


def _context(provider: _FixedProvider, user_text: str, orders: list) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=orders, is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None,
    )


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
