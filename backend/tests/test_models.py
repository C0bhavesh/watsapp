import pytest

from app.shopify.models import AuthorizedOrder, Customer, Money, Order, normalize_order_name


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


def test_authorized_order_accepts_matching_order_phone() -> None:
    o = make_order(phone="+919999999999")
    auth = AuthorizedOrder(order=o, verified_phone="+919999999999")
    assert auth.verified_phone == "+919999999999"


def test_authorized_order_accepts_matching_shipping_phone() -> None:
    o = make_order(shipping_phone="+918888888888")
    assert AuthorizedOrder(order=o, verified_phone="+918888888888").order is o


def test_authorized_order_accepts_matching_billing_phone() -> None:
    o = make_order(billing_phone="+917777777777")
    assert AuthorizedOrder(order=o, verified_phone="+917777777777").order is o


def test_authorized_order_rejects_non_matching_phone() -> None:
    o = make_order(phone="+919999999999")
    with pytest.raises(ValueError):
        AuthorizedOrder(order=o, verified_phone="+910000000000")


def test_authorized_order_rejects_empty_verified_phone() -> None:
    o = make_order(phone="+919999999999")
    with pytest.raises(ValueError):
        AuthorizedOrder(order=o, verified_phone="")


def test_authorized_order_accepts_same_number_in_different_formats() -> None:
    # The ownership check must be source-independent: the mirror stores phones E.164-normalized
    # while live Shopify supplies them raw (customer free-text). Two representations of the SAME
    # number must be treated as the same number, whichever OrderSource answered.
    o = make_order(phone="9876543210")  # raw, as live Shopify might return it
    assert AuthorizedOrder(order=o, verified_phone="+919876543210").order is o
    o2 = make_order(phone="+919876543210")  # E.164, as the mirror stores it
    assert AuthorizedOrder(order=o2, verified_phone="09876543210").order is o2
    assert AuthorizedOrder(order=o2, verified_phone="9876543210").order is o2


def test_authorized_order_still_rejects_a_genuinely_different_number() -> None:
    o = make_order(phone="+919876543210")
    with pytest.raises(ValueError):
        AuthorizedOrder(order=o, verified_phone="+919000000000")


def test_authorized_order_rejects_when_both_sides_are_unparseable() -> None:
    # normalize_phone returns None for junk; two None values must NOT be treated as a match
    # (that would authorize an order against an unverifiable number).
    o = make_order(phone="junk")
    with pytest.raises(ValueError):
        AuthorizedOrder(order=o, verified_phone="also-junk")


def test_normalize_order_name_variants() -> None:
    assert normalize_order_name("tavas3733") == "tavas3733"
    assert normalize_order_name("#tavas3733") == "tavas3733"
    assert normalize_order_name("3733") == "tavas3733"
    assert normalize_order_name("#3733") == "tavas3733"
    assert normalize_order_name("  TAVAS3733 ") == "tavas3733"


def test_order_customer_defaults_to_none() -> None:
    assert make_order().customer is None


def test_order_accepts_a_customer() -> None:
    cust = Customer(
        gid="gid://shopify/Customer/1",
        first_name="Suman",
        last_name="Bayala",
        email="c@example.com",
        phone="+919999999999",
        address_line1="12 MG Road",
        address_line2=None,
        city="Bengaluru",
        state="Karnataka",
        postal_code="560001",
        country="India",
    )
    order = make_order(customer=cust)
    assert order.customer is cust
    assert order.customer.city == "Bengaluru"
