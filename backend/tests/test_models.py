from app.shopify.models import Money, Order, normalize_order_name


def make_order(**overrides) -> Order:
    base = dict(
        gid="gid://shopify/Order/1",
        name="tavas3733",
        email="c@example.com",
        phone=None,
        shipping_phone=None,
        billing_phone=None,
        financial_status="PENDING",
        fulfillment_status="UNFULFILLED",
        cancelled_at=None,
        tags=(),
        payment_gateway_names=(),
        total=Money("949.0", "INR"),
        customer_locale="en",
    )
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


def test_best_phone_chain_order_first() -> None:
    o = make_order(phone="+911", shipping_phone="+912", billing_phone="+913")
    assert o.best_phone() == "+911"


def test_best_phone_falls_back_shipping_then_billing() -> None:
    assert make_order(shipping_phone="+912", billing_phone="+913").best_phone() == "+912"
    assert make_order(billing_phone="+913").best_phone() == "+913"
    assert make_order().best_phone() is None


def test_is_cod_via_gateway_and_tag() -> None:
    assert make_order(payment_gateway_names=("Cash on Delivery (COD)",)).is_cod()
    assert make_order(tags=("COD", "Shopflo")).is_cod()
    assert not make_order(payment_gateway_names=("Razorpay",), tags=("online",)).is_cod()


def test_is_cancelled() -> None:
    assert make_order(cancelled_at="2026-07-28T00:00:00Z").is_cancelled()
    assert not make_order().is_cancelled()


def test_normalize_order_name_variants() -> None:
    assert normalize_order_name("tavas3733") == "tavas3733"
    assert normalize_order_name("#tavas3733") == "tavas3733"
    assert normalize_order_name("3733") == "tavas3733"
    assert normalize_order_name("#3733") == "tavas3733"
    assert normalize_order_name("  TAVAS3733 ") == "tavas3733"
