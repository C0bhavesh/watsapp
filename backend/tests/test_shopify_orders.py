from datetime import UTC, datetime, timedelta

from app.channels.shopify_orders import (
    IncomingOrder,
    choose_language,
    is_eligible_for_push,
    parse_order_created,
)

PAYLOAD = {
    "admin_graphql_api_id": "gid://shopify/Order/12187547894128",
    "name": "tavas3733",
    "order_number": 3733,
    "email": "c@example.com",
    "phone": None,
    "customer": {"first_name": "Suman", "last_name": "Bayala", "phone": None},
    "shipping_address": {"phone": "+91 9664290413"},
    "billing_address": {"phone": None},
    "tags": "COD, COD pending, Shopflo",
    "payment_gateway_names": ["Cash on Delivery (COD)"],
    "financial_status": "pending",
    "created_at": "2026-07-28T03:14:46-04:00",
    "customer_locale": "en-IN",
    "test": False,
}


def test_parse_full_payload() -> None:
    order = parse_order_created(PAYLOAD)
    assert order is not None
    assert order.gid == "gid://shopify/Order/12187547894128"
    assert order.name == "tavas3733"
    assert order.order_number == 3733
    assert order.phone_e164 == "+919664290413"     # shipping fallback, normalized
    assert order.customer_name == "Suman Bayala"
    assert order.is_cod()
    assert order.created_at is not None and order.created_at.tzinfo is not None
    assert order.locale == "en-IN"


def test_parse_missing_gid_returns_none() -> None:
    assert parse_order_created({"name": "x"}) is None


def test_parse_tolerates_missing_optional_fields() -> None:
    order = parse_order_created({"admin_graphql_api_id": "gid://shopify/Order/2", "name": "tavas9"})
    assert order is not None
    assert order.phone_e164 is None and order.tags == () and order.created_at is None


def test_choose_language() -> None:
    assert choose_language("en-IN") == "en"
    assert choose_language("hi") == "hi"
    assert choose_language("gu-IN") == "gu"
    assert choose_language("ta-IN") == "en"
    assert choose_language(None) == "en"


def test_choose_language_tolerates_non_str_locale() -> None:
    assert choose_language(5) == "en"  # type: ignore[arg-type]


def _base(**extra: object) -> dict:
    return {"admin_graphql_api_id": "gid://shopify/Order/9", "name": "tavas9", **extra}


def test_int_phone_coerced_to_none() -> None:
    order = parse_order_created(_base(phone=919664290413))
    assert order is not None and order.phone_e164 is None


def test_str_customer_does_not_crash() -> None:
    order = parse_order_created(_base(customer="pwn"))
    assert order is not None and order.customer_name is None


def test_int_payment_gateway_names_is_empty() -> None:
    order = parse_order_created(_base(payment_gateway_names=5))
    assert order is not None and order.gateways == ()


def test_int_customer_locale_is_none() -> None:
    order = parse_order_created(_base(customer_locale=5))
    assert order is not None and order.locale is None


def test_non_str_email_and_financial_status_become_none() -> None:
    order = parse_order_created(_base(email={"x": 1}, financial_status=5))
    assert order is not None
    assert order.email is None
    assert order.financial_status is None


def make_order(created_delta_hours: float, cod: bool) -> IncomingOrder:
    parsed = parse_order_created(PAYLOAD)
    assert parsed is not None
    return IncomingOrder(
        **{**parsed.__dict__,
           "created_at": datetime.now(UTC) - timedelta(hours=created_delta_hours),
           "gateways": ("Cash on Delivery (COD)",) if cod else ("Razorpay",),
           "tags": () if not cod else parsed.tags}
    )


def test_eligibility_cod_only_policy() -> None:
    now = datetime.now(UTC)
    assert is_eligible_for_push(make_order(1, cod=True), now, "cod_only", 6.0)
    assert not is_eligible_for_push(make_order(1, cod=False), now, "cod_only", 6.0)
    assert is_eligible_for_push(make_order(1, cod=False), now, "all", 6.0)


def test_eligibility_staleness_guard() -> None:
    now = datetime.now(UTC)
    assert not is_eligible_for_push(make_order(7, cod=True), now, "cod_only", 6.0)


def test_eligibility_unparseable_created_at_is_ineligible() -> None:
    parsed = parse_order_created({"admin_graphql_api_id": "g", "name": "n"})
    assert parsed is not None
    assert not is_eligible_for_push(parsed, datetime.now(UTC), "all", 6.0)
