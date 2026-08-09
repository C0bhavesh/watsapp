"""Unit tests for the order-name recovery fallback in the conversation pipeline.

Covers ``_recover_order_by_name`` -- the helper that lets a customer whose WhatsApp number is
mapped to no order still be helped by the order number they type. Ownership is enforced by
``resolve_by_order_name`` (returning None both for "no such order" and "not this sender's
order"), so these tests pin: an owned token is recovered, an unowned/absent token reveals
nothing, and extraction is safe on huge/hostile input.
"""

from app.core.conversation import _recover_order_by_name
from app.shopify.models import Order


def _order(gid: str, name: str, phone: str | None) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeShopify:
    """Only ``find_order_by_name`` is exercised by the recovery path; the rest satisfy the
    structural ``OrderSource`` shape without being called."""

    def __init__(self, orders_by_name: dict[str, Order] | None = None) -> None:
        self.orders_by_name = orders_by_name or {}

    async def get_order(self, gid: str) -> Order | None:
        return None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        return self.orders_by_name.get(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


async def test_recover_order_by_name_returns_owned_order() -> None:
    shopify = _FakeShopify(orders_by_name={"tavas5": _order("gid://5", "tavas5", "+919999999999")})

    result = await _recover_order_by_name(shopify, "919999999999", "hi where is tavas5")

    assert len(result) == 1
    assert result[0].order.gid == "gid://5"
    assert result[0].verified_phone == "+919999999999"


async def test_recover_order_by_name_unowned_returns_empty() -> None:
    # The order exists but belongs to a different phone -> resolve_by_order_name returns None,
    # so nothing is revealed (and the None is indistinguishable from "no such order").
    shopify = _FakeShopify(orders_by_name={"tavas6": _order("gid://6", "tavas6", "+911111111111")})

    result = await _recover_order_by_name(shopify, "919999999999", "where is tavas6")

    assert result == []


async def test_recover_order_by_name_no_token_returns_empty() -> None:
    shopify = _FakeShopify(orders_by_name={"tavas5": _order("gid://5", "tavas5", "+919999999999")})

    result = await _recover_order_by_name(shopify, "919999999999", "where is my order please")

    assert result == []


async def test_recover_order_by_name_extracts_token_from_long_hostile_text() -> None:
    shopify = _FakeShopify(orders_by_name={"tavas5": _order("gid://5", "tavas5", "+919999999999")})
    hostile = (
        "\U0001f600" * 5000 + "\n\x00 TAVAS no-digits here " + "tavas5"
        + " ' \" ; -- OR 1=1 " + "力" * 5000
    )

    result = await _recover_order_by_name(shopify, "919999999999", hostile)

    assert len(result) == 1
    assert result[0].order.gid == "gid://5"
