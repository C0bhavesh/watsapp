from datetime import UTC, datetime, timedelta

from app.channels.shopify_orders import (
    MAX_FIELD_LEN,
    IncomingOrder,
    choose_language,
    customer_from_webhook_payload,
    fulfillment_from_webhook_payload,
    is_eligible_for_push,
    order_from_webhook_payload,
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


FULFILLMENT_PAYLOAD = {
    "id": 5551234567890,
    "admin_graphql_api_id": "gid://shopify/Fulfillment/5551234567890",
    "order_id": 12187547894128,
    "status": "success",
    "tracking_company": "Delhivery",
    "tracking_number": "AWB0099887766",
    "tracking_numbers": ["AWB0099887766"],
    "tracking_url": "https://www.delhivery.com/track/AWB0099887766",
    "tracking_urls": ["https://www.delhivery.com/track/AWB0099887766"],
    "created_at": "2026-08-14T03:14:46-04:00",
    "updated_at": "2026-08-14T03:20:00-04:00",
}


def test_fulfillment_parse_full_payload() -> None:
    parsed = fulfillment_from_webhook_payload(FULFILLMENT_PAYLOAD)
    assert parsed is not None
    order_gid, fulfillment = parsed
    # order_id is numeric in the REST payload -- the order gid is CONSTRUCTED from it.
    assert order_gid == "gid://shopify/Order/12187547894128"
    assert fulfillment.gid == "gid://shopify/Fulfillment/5551234567890"
    assert fulfillment.status == "success"
    assert fulfillment.tracking_company == "Delhivery"
    assert fulfillment.tracking_number == "AWB0099887766"
    assert fulfillment.tracking_url == "https://www.delhivery.com/track/AWB0099887766"
    assert fulfillment.created_at == "2026-08-14T03:14:46-04:00"


def test_fulfillment_falls_back_to_array_tracking_when_singular_absent() -> None:
    payload = dict(FULFILLMENT_PAYLOAD)
    del payload["tracking_number"]
    del payload["tracking_url"]
    payload["tracking_numbers"] = ["FIRST123", "SECOND456"]
    payload["tracking_urls"] = ["https://track/FIRST123", "https://track/SECOND456"]
    parsed = fulfillment_from_webhook_payload(payload)
    assert parsed is not None
    _, fulfillment = parsed
    # A single fulfillment can carry multiple tracking numbers (rare) -- keep the first, matching
    # the one-tracking-per-row mirror schema.
    assert fulfillment.tracking_number == "FIRST123"
    assert fulfillment.tracking_url == "https://track/FIRST123"


def test_fulfillment_missing_own_gid_returns_none() -> None:
    payload = dict(FULFILLMENT_PAYLOAD)
    del payload["admin_graphql_api_id"]
    assert fulfillment_from_webhook_payload(payload) is None


def test_fulfillment_missing_or_bad_order_id_returns_none() -> None:
    no_order = dict(FULFILLMENT_PAYLOAD)
    del no_order["order_id"]
    assert fulfillment_from_webhook_payload(no_order) is None
    bad_order = dict(FULFILLMENT_PAYLOAD, order_id="not-a-number")
    assert fulfillment_from_webhook_payload(bad_order) is None
    # bool is an int subclass -- must not construct gid://shopify/Order/True
    assert fulfillment_from_webhook_payload(dict(FULFILLMENT_PAYLOAD, order_id=True)) is None


def test_fulfillment_accepts_string_order_id() -> None:
    parsed = fulfillment_from_webhook_payload(dict(FULFILLMENT_PAYLOAD, order_id="98765"))
    assert parsed is not None
    order_gid, _ = parsed
    assert order_gid == "gid://shopify/Order/98765"


def test_fulfillment_without_tracking_keeps_none_fields() -> None:
    # A fulfillment can exist with no tracking yet (label created, not scanned) -- parse it, do
    # not fabricate a tracking number.
    payload = {
        "admin_graphql_api_id": "gid://shopify/Fulfillment/1",
        "order_id": 42,
        "status": "pending",
    }
    parsed = fulfillment_from_webhook_payload(payload)
    assert parsed is not None
    _, fulfillment = parsed
    assert fulfillment.status == "pending"
    assert fulfillment.tracking_number is None
    assert fulfillment.tracking_url is None
    assert fulfillment.tracking_company is None
    assert fulfillment.has_tracking() is False


def test_fulfillment_ignores_non_string_array_entries() -> None:
    payload = {
        "admin_graphql_api_id": "gid://shopify/Fulfillment/1",
        "order_id": 42,
        "tracking_numbers": [123, "GOOD"],
    }
    parsed = fulfillment_from_webhook_payload(payload)
    assert parsed is not None
    _, fulfillment = parsed
    # first entry is not a str -> skip it, take the first usable string
    assert fulfillment.tracking_number == "GOOD"


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


ORDER_WEBHOOK_PAYLOAD = {
    **PAYLOAD,
    "fulfillment_status": "fulfilled",
    "cancelled_at": None,
    "total_price": "949.00",
    "currency": "INR",
    "shipping_address": {
        "phone": "+919664290413", "address1": "12 MG Road", "address2": None,
        "city": "Bengaluru", "province": "Karnataka", "zip": "560001", "country": "India",
    },
    "billing_address": {"phone": None},
    "customer": {
        "id": 987654321, "admin_graphql_api_id": "gid://shopify/Customer/987654321",
        "first_name": "Suman", "last_name": "Bayala", "email": "c@example.com", "phone": None,
    },
    "line_items": [
        {"title": "Blue Chikankari Kurti", "sku": "KUR-BLU-M", "quantity": 1,
         "variant_title": "Blue / M", "price": "999.00"},
        {"title": "Cotton Dupatta", "sku": None, "quantity": 2,
         "variant_title": None, "price": "150.00"},
    ],
}


def test_order_from_webhook_payload_parses_full_order() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert order.gid == "gid://shopify/Order/12187547894128"
    assert order.name == "tavas3733"
    assert order.fulfillment_status == "fulfilled"
    assert order.cancelled_at is None
    assert order.total is not None
    assert order.total.amount == "949.00" and order.total.currency == "INR"
    assert order.shipping_phone == "+919664290413"
    assert order.billing_phone is None


def test_order_from_webhook_payload_parses_line_items() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert len(order.line_items) == 2
    first, second = order.line_items
    assert first.title == "Blue Chikankari Kurti"
    assert first.sku == "KUR-BLU-M"
    assert first.variant_title == "Blue / M"
    assert first.price is not None and first.price.amount == "999.00"
    assert second.sku is None
    assert second.variant_title is None


def test_order_from_webhook_payload_zero_line_items() -> None:
    p = {**ORDER_WEBHOOK_PAYLOAD, "line_items": []}
    order = order_from_webhook_payload(p)
    assert order is not None
    assert order.line_items == ()


def test_order_from_webhook_payload_missing_gid_returns_none() -> None:
    assert order_from_webhook_payload({"name": "x"}) is None


def test_order_from_webhook_payload_parses_customer() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert order.customer is not None
    assert order.customer.gid == "gid://shopify/Customer/987654321"
    assert order.customer.first_name == "Suman"
    assert order.customer.city == "Bengaluru"
    assert order.customer.postal_code == "560001"


def test_order_from_webhook_payload_no_customer_object() -> None:
    p = {k: v for k, v in ORDER_WEBHOOK_PAYLOAD.items() if k != "customer"}
    order = order_from_webhook_payload(p)
    assert order is not None
    assert order.customer is None


CUSTOMER_UPDATE_PAYLOAD = {
    "id": 987654321,
    "admin_graphql_api_id": "gid://shopify/Customer/987654321",
    "first_name": "Suman",
    "last_name": "Bayala",
    "email": "c@example.com",
    "phone": "+919999999999",
    "default_address": {
        "address1": "12 MG Road", "address2": "Flat 4B", "city": "Bengaluru",
        "province": "Karnataka", "zip": "560001", "country": "India",
    },
}


def test_customer_from_webhook_payload_parses_full_customer() -> None:
    cust = customer_from_webhook_payload(CUSTOMER_UPDATE_PAYLOAD)
    assert cust is not None
    assert cust.gid == "gid://shopify/Customer/987654321"
    assert cust.first_name == "Suman"
    assert cust.phone == "+919999999999"
    assert cust.address_line2 == "Flat 4B"
    assert cust.postal_code == "560001"


def test_customer_from_webhook_payload_missing_id_returns_none() -> None:
    assert customer_from_webhook_payload({"first_name": "x"}) is None


def test_customer_from_webhook_payload_no_default_address() -> None:
    p = {k: v for k, v in CUSTOMER_UPDATE_PAYLOAD.items() if k != "default_address"}
    cust = customer_from_webhook_payload(p)
    assert cust is not None
    assert cust.address_line1 is None
    assert cust.city is None


def test_webhook_payloads_carry_updated_at_for_the_ordering_guard() -> None:
    # Shopify sends updated_at on both resources; the mirror needs it to reject a late-arriving
    # RETRY of an older update that would otherwise revert newer state.
    order = order_from_webhook_payload(
        {**ORDER_WEBHOOK_PAYLOAD, "updated_at": "2026-08-11T12:00:00Z"}
    )
    assert order is not None
    assert order.updated_at == "2026-08-11T12:00:00Z"
    cust = customer_from_webhook_payload(
        {**CUSTOMER_UPDATE_PAYLOAD, "updated_at": "2026-08-11T12:00:00Z"}
    )
    assert cust is not None
    assert cust.updated_at == "2026-08-11T12:00:00Z"


def test_missing_updated_at_is_none_not_a_failure() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None and order.updated_at is None


def test_order_from_webhook_payload_clips_oversized_fields() -> None:
    # Same posture as the orders/create mapping write: every field on a signed payload is
    # attacker-typed, so nothing unbounded reaches a text column in the mirror.
    long = "x" * 5000
    p = {
        **ORDER_WEBHOOK_PAYLOAD,
        "name": long,
        "email": long,
        "tags": long,
        "total_price": long,
        "line_items": [{"title": long, "sku": long, "quantity": 1, "variant_title": long,
                        "price": long}],
        "customer": {**ORDER_WEBHOOK_PAYLOAD["customer"], "first_name": long},  # type: ignore[dict-item]
        "shipping_address": {**ORDER_WEBHOOK_PAYLOAD["shipping_address"], "city": long},  # type: ignore[dict-item]
    }
    order = order_from_webhook_payload(p)
    assert order is not None
    assert len(order.name) == MAX_FIELD_LEN
    assert order.email is not None and len(order.email) == MAX_FIELD_LEN
    assert len(order.tags[0]) == MAX_FIELD_LEN
    assert order.total is not None and len(order.total.amount) == MAX_FIELD_LEN
    item = order.line_items[0]
    assert len(item.title) == MAX_FIELD_LEN
    assert item.sku is not None and len(item.sku) == MAX_FIELD_LEN
    assert item.variant_title is not None and len(item.variant_title) == MAX_FIELD_LEN
    assert order.customer is not None
    assert order.customer.first_name is not None
    assert len(order.customer.first_name) == MAX_FIELD_LEN
    assert order.customer.city is not None and len(order.customer.city) == MAX_FIELD_LEN


def test_customer_from_webhook_payload_clips_oversized_fields() -> None:
    long = "y" * 5000
    cust = customer_from_webhook_payload(
        {**CUSTOMER_UPDATE_PAYLOAD, "first_name": long, "email": long,
         "default_address": {"address1": long, "city": long}}
    )
    assert cust is not None
    assert cust.first_name is not None and len(cust.first_name) == MAX_FIELD_LEN
    assert cust.email is not None and len(cust.email) == MAX_FIELD_LEN
    assert cust.address_line1 is not None and len(cust.address_line1) == MAX_FIELD_LEN
    assert cust.city is not None and len(cust.city) == MAX_FIELD_LEN
