from app.core.delivery_outcome import fulfillment_is_genuinely_delivered
from app.shopify.models import Fulfillment, FulfillmentEvent


def _fulfillment(
    display_status: str | None,
    events: tuple[FulfillmentEvent, ...],
) -> Fulfillment:
    return Fulfillment(
        gid="gid://f/1",
        status=None,
        tracking_company=None,
        tracking_number=None,
        tracking_url=None,
        display_status=display_status,
        events=events,
    )


def test_clean_delivery_is_genuine() -> None:
    f = _fulfillment(
        "DELIVERED",
        (
            FulfillmentEvent("OUT_FOR_DELIVERY", "2026-08-29T07:00:00Z"),
            FulfillmentEvent("DELIVERED", "2026-08-29T08:00:00Z"),
        ),
    )
    assert fulfillment_is_genuinely_delivered(f) is True


def test_rto_late_scan_after_delivered_is_not_genuine() -> None:
    f = _fulfillment(
        "DELIVERED",
        (
            FulfillmentEvent("DELIVERED", "2026-08-29T08:00:00Z"),
            FulfillmentEvent("ATTEMPTED_DELIVERY", "2026-08-29T10:00:00Z"),
        ),
    )
    assert fulfillment_is_genuinely_delivered(f) is False


def test_stuck_attempted_is_not_genuine() -> None:
    f = _fulfillment(
        "ATTEMPTED_DELIVERY",
        (
            FulfillmentEvent("OUT_FOR_DELIVERY", "2026-08-29T07:00:00Z"),
            FulfillmentEvent("ATTEMPTED_DELIVERY", "2026-08-29T10:00:00Z"),
        ),
    )
    assert fulfillment_is_genuinely_delivered(f) is False


def test_no_events_is_not_genuine() -> None:
    f = _fulfillment("DELIVERED", ())
    assert fulfillment_is_genuinely_delivered(f) is False


def test_none_display_status_is_not_genuine() -> None:
    f = _fulfillment(None, ())
    assert fulfillment_is_genuinely_delivered(f) is False
