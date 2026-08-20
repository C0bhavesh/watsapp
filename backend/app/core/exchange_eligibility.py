"""Deterministic size-exchange eligibility check.

Mirrors app/core/delivery_estimate.py's discipline: a pure function of (Order, now), no I/O,
computed once and handed to the agent (app/agents/exchange.py) as an already-decided fact --
the LLM only relays ExchangeEligibility.reason verbatim, it never computes or guesses the
48-hour window itself.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.shopify.models import Order

_EXCHANGE_WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class ExchangeEligibility:
    eligible: bool
    reason: str


def _parse_datetime(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _latest_delivery(order: Order) -> datetime | None:
    parsed = [
        dt for f in order.fulfillments
        if f.delivered_at is not None and (dt := _parse_datetime(f.delivered_at)) is not None
    ]
    return max(parsed) if parsed else None


def check_exchange_eligibility(order: Order, now: datetime) -> ExchangeEligibility:
    """Eligible only for a non-cancelled order delivered within the last 48 hours.

    ``reason`` is always populated, both when eligible and not -- app/agents/exchange.py
    relays it to the customer verbatim rather than composing its own explanation, so the
    exact wording here is what the customer sees.
    """
    if order.is_cancelled():
        return ExchangeEligibility(eligible=False, reason="this order is cancelled.")

    delivered_at = _latest_delivery(order)
    if delivered_at is None:
        return ExchangeEligibility(
            eligible=False, reason="this order has not been delivered yet."
        )

    delivered_date = delivered_at.date().isoformat()
    if now - delivered_at > _EXCHANGE_WINDOW:
        return ExchangeEligibility(
            eligible=False,
            reason=(
                f"delivered on {delivered_date}, which is outside the 48-hour exchange "
                "window."
            ),
        )
    return ExchangeEligibility(
        eligible=True,
        reason=f"delivered on {delivered_date}, within the 48-hour exchange window.",
    )
