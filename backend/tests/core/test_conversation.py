"""Unit tests for the order-name recovery fallback in the conversation pipeline.

Covers ``_recover_order_by_name`` -- the helper that lets a customer be helped by the order
number they type, whether or not their WhatsApp number is already mapped to an order.
Ownership is enforced by ``resolve_by_order_name`` (returning None both for "no such order" and
"not this sender's order"), so these tests pin: an owned token is recovered, an unowned/absent
token reveals nothing, extraction is safe on huge/hostile input, and a number-shaped token of
the wrong digit count never reaches Shopify at all -- it produces a format hint instead.
"""

from app.core.conversation import _recover_order_by_name
from app.shopify.models import ORDER_NUMBER_DIGIT_LENGTH, Order


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
        self.calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        return None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        self.calls.append(raw_name)
        return self.orders_by_name.get(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


async def test_recover_order_by_name_returns_owned_order() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "hi where is tavas5432")

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://5"
    assert orders[0].verified_phone == "+919999999999"
    assert hint is None


async def test_recover_order_by_name_unowned_returns_empty() -> None:
    # The order exists but belongs to a different phone -> resolve_by_order_name returns None,
    # so nothing is revealed (and the None is indistinguishable from "no such order").
    shopify = _FakeShopify(
        orders_by_name={"tavas6543": _order("gid://6", "tavas6543", "+911111111111")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas6543")

    assert orders == []
    assert hint is None


async def test_recover_order_by_name_no_token_returns_empty() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is my order please")

    assert orders == []
    assert hint is None


async def test_recover_order_by_name_extracts_token_from_long_hostile_text() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )
    hostile = (
        "\U0001f600" * 5000 + "\n\x00 TAVAS no-digits here " + "tavas5432"
        + " ' \" ; -- OR 1=1 " + "力" * 5000
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", hostile)

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://5"
    assert hint is None


async def test_recover_order_by_name_bare_digits_normalize_to_tavas_prefix() -> None:
    # A bare 4-digit number (no "tavas"/"#" prefix) is still a valid candidate -- passed through
    # as-is to resolve_by_order_name, which normalizes it exactly like the real Shopify client
    # does (bare digits -> "tavas" + digits).
    shopify = _FakeShopify(
        orders_by_name={"9652": _order("gid://9652", "tavas9652", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "my order number is 9652")

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://9652"
    assert hint is None


async def test_recover_order_by_name_hash_prefixed() -> None:
    shopify = _FakeShopify(
        orders_by_name={"#9652": _order("gid://9652", "tavas9652", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "order #9652 please")

    assert len(orders) == 1
    assert hint is None


async def test_recover_order_by_name_short_bare_number_is_not_a_candidate() -> None:
    # Below the 3-digit floor for a bare (unprefixed) number -- dates/quantities/etc. should not
    # be mistaken for an order-number attempt.
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "I ordered 2 shirts")

    assert orders == []
    assert hint is None
    assert shopify.calls == []


async def test_recover_order_by_name_wrong_digit_count_never_calls_shopify() -> None:
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "my order id is 965")

    assert orders == []
    assert hint is not None
    assert str(ORDER_NUMBER_DIGIT_LENGTH) in hint
    assert shopify.calls == []


async def test_recover_order_by_name_tavas_prefixed_wrong_digit_count_never_calls_shopify() -> None:
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas96522")

    assert orders == []
    assert hint is not None
    assert shopify.calls == []
