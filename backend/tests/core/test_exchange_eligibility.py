from datetime import UTC, datetime

from app.core.exchange_eligibility import check_exchange_eligibility
from app.shopify.models import Fulfillment, Order


def _order(
    cancelled_at: str | None = None,
    fulfillments: tuple[Fulfillment, ...] = (),
) -> Order:
    return Order(
        gid="gid://o/1", name="tavas1", email=None, phone=None, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, fulfillments=fulfillments,
    )


def _delivered(at: str) -> Fulfillment:
    return Fulfillment(
        gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
        tracking_number="AWB1", tracking_url="https://track/AWB1", delivered_at=at,
    )


def test_cancelled_order_is_not_eligible() -> None:
    order = _order(cancelled_at="2026-08-10T00:00:00+00:00")
    result = check_exchange_eligibility(order, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert result.eligible is False
    assert "cancelled" in result.reason


def test_undelivered_order_is_not_eligible() -> None:
    order = _order()
    result = check_exchange_eligibility(order, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert result.eligible is False
    assert "not been delivered" in result.reason


def test_delivered_within_window_is_eligible() -> None:
    order = _order(fulfillments=(_delivered("2026-08-10T00:00:00+00:00"),))
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)  # 36 hours later
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is True
    assert "2026-08-10" in result.reason


def test_delivered_exactly_at_the_48_hour_boundary_is_still_eligible() -> None:
    order = _order(fulfillments=(_delivered("2026-08-10T00:00:00+00:00"),))
    now = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)  # exactly 48h later
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is True


def test_delivered_just_past_the_48_hour_boundary_is_not_eligible() -> None:
    order = _order(fulfillments=(_delivered("2026-08-10T00:00:00+00:00"),))
    now = datetime(2026, 8, 12, 0, 1, tzinfo=UTC)  # 48h1m later
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is False
    assert "outside the 48-hour" in result.reason


def test_multiple_fulfillments_uses_the_latest_delivered_at() -> None:
    order = _order(
        fulfillments=(
            _delivered("2026-08-05T00:00:00+00:00"),  # older, would be ineligible alone
            _delivered("2026-08-10T00:00:00+00:00"),  # latest -- this one governs
        ),
    )
    now = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is True


def test_unparsable_delivered_at_is_treated_as_not_delivered() -> None:
    order = _order(fulfillments=(_delivered("not-a-real-timestamp"),))
    result = check_exchange_eligibility(order, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert result.eligible is False
    assert "not been delivered" in result.reason
