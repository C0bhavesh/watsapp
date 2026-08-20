"""Formula-based delivery-date estimate for the order-tracking Q&A agent.

Deliberately does NOT read or scrape any courier tracking page for a real ETA (see
docs/superpowers/specs/2026-08-20-delivery-date-estimation-design.md -- that would reopen
client-decisions-all.md Q10, which already closed "no live courier integration"). This is a
pure function of (Order, today): prep buffer + a fixed regional zone transit time, with a
late-ship exception. Never invents a date beyond what this formula computes -- the caller
(app/agents/order_tracking.py) renders the result as plain text for the LLM to relay verbatim.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.shopify.models import Order

_PREP_DAYS = 2
_LATE_SHIP_THRESHOLD_DAYS = 3
_LATE_SHIP_EXTRA_DAYS = 2

_ZONE_DAYS: dict[str, int] = {"west": 2, "north": 3, "east": 5, "south": 5}

# No official 4-bucket zone covers every Indian state/UT. Madhya Pradesh and Chhattisgarh are
# grouped into "west" (nearest geographic fit); the Northeast states are grouped into "east" --
# both call-outs are documented in the design doc for the owner to revisit if wanted.
_STATE_ZONE: dict[str, str] = {
    "jammu and kashmir": "north", "ladakh": "north", "himachal pradesh": "north",
    "punjab": "north", "chandigarh": "north", "uttarakhand": "north",
    "haryana": "north", "delhi": "north", "uttar pradesh": "north", "rajasthan": "north",
    "gujarat": "west", "maharashtra": "west", "goa": "west",
    "dadra and nagar haveli and daman and diu": "west",
    "madhya pradesh": "west", "chhattisgarh": "west",
    "west bengal": "east", "odisha": "east", "bihar": "east", "jharkhand": "east",
    "assam": "east", "sikkim": "east", "arunachal pradesh": "east", "nagaland": "east",
    "manipur": "east", "mizoram": "east", "tripura": "east", "meghalaya": "east",
    "karnataka": "south", "andhra pradesh": "south", "telangana": "south",
    "tamil nadu": "south", "kerala": "south", "puducherry": "south",
    "andaman and nicobar islands": "south", "lakshadweep": "south",
}
# Unknown/missing state -> longest transit. Safer to slightly over-promise than under.
_DEFAULT_ZONE = "south"

# Matches app.agents.order_tracking._is_cancel_eligible's fulfillment-status predicate: these
# two values are the only ones that mean "not yet dispatched" in this codebase.
_UNDISPATCHED_STATUSES = (None, "UNFULFILLED")


@dataclass(frozen=True)
class DeliveryEstimate:
    expected_date: date


def _zone_for(order: Order) -> str:
    state = order.customer.state if order.customer is not None else None
    if not state:
        return _DEFAULT_ZONE
    return _STATE_ZONE.get(state.strip().lower(), _DEFAULT_ZONE)


def _is_delivered(order: Order) -> bool:
    return any(f.delivered_at is not None for f in order.fulfillments)


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def estimate_delivery(order: Order, today: date) -> DeliveryEstimate | None:
    """Compute a formula-based delivery estimate, or None if one cannot/should not be given.

    Returns None when the order is already delivered (nothing to estimate -- the caller shows
    the real delivery info instead) or when order.created_at is missing (a legacy order synced
    before this field existed; guessing from an unknown start point would be worse than no
    estimate).
    """
    if _is_delivered(order):
        return None
    if order.created_at is None:
        return None
    created = _parse_date(order.created_at)
    if created is None:
        return None

    zone_days = _ZONE_DAYS[_zone_for(order)]
    total_days = _PREP_DAYS + zone_days

    undispatched = order.fulfillment_status in _UNDISPATCHED_STATUSES
    if undispatched and (today - created).days > _LATE_SHIP_THRESHOLD_DAYS:
        total_days += _LATE_SHIP_EXTRA_DAYS

    return DeliveryEstimate(expected_date=created + timedelta(days=total_days))
