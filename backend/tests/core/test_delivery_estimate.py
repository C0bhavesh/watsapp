from dataclasses import replace
from datetime import date

from app.core.delivery_estimate import estimate_delivery
from app.shopify.models import Customer, Fulfillment, Order


def _customer(state: str | None) -> Customer:
    return Customer(
        gid="gid://c/1", first_name=None, last_name=None, email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=state,
        postal_code=None, country=None,
    )


def _order(
    created_at: str | None = "2026-08-10T00:00:00+00:00",
    state: str | None = "Gujarat",
    fulfillment_status: str | None = None,
    fulfillments: tuple[Fulfillment, ...] = (),
) -> Order:
    return Order(
        gid="gid://o/1", name="tavas1", email=None, phone=None, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=fulfillment_status,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, customer=_customer(state), created_at=created_at,
        fulfillments=fulfillments,
    )


def test_west_zone_adds_two_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    # 2 days prep + 2 days west transit = 4 days from order-created
    assert result.expected_date == date(2026, 8, 14)


def test_north_zone_adds_three_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Delhi")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 15)


def test_east_zone_adds_five_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="West Bengal")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)


def test_south_zone_adds_five_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Tamil Nadu")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)


def test_zone_match_is_case_insensitive() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 14)


def test_unknown_state_defaults_to_the_longest_zone() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Atlantis")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)  # 2 + 5 (south/east default)


def test_missing_customer_defaults_to_the_longest_zone() -> None:
    order = replace(_order(created_at="2026-08-10T00:00:00+00:00"), customer=None)
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)


def test_late_ship_exception_adds_two_more_days() -> None:
    # >3 days since order-created AND still not dispatched.
    order = _order(created_at="2026-08-01T00:00:00+00:00", state="Gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    # 2 prep + 2 west + 2 late-ship = 6 days from order-created
    assert result.expected_date == date(2026, 8, 7)


def test_late_ship_exception_does_not_fire_within_three_days() -> None:
    order = _order(created_at="2026-08-08T00:00:00+00:00", state="Gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 12)  # no +2, still within the window


def test_late_ship_exception_does_not_fire_once_dispatched() -> None:
    order = _order(
        created_at="2026-08-01T00:00:00+00:00", state="Gujarat",
        fulfillment_status="FULFILLED",
    )
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 5)  # no late-ship +2, dispatched already


def test_already_delivered_returns_none() -> None:
    order = _order(
        fulfillments=(
            Fulfillment(
                gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
                tracking_number="AWB1", tracking_url="https://track/AWB1",
                delivered_at="2026-08-12T00:00:00+00:00",
            ),
        ),
    )
    assert estimate_delivery(order, today=date(2026, 8, 13)) is None


def test_missing_created_at_returns_none() -> None:
    order = _order(created_at=None)
    assert estimate_delivery(order, today=date(2026, 8, 10)) is None
