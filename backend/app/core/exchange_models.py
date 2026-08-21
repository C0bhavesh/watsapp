"""App-owned exchange-request state -- kept out of app/shopify/models.py, which holds only
Shopify-sourced data. An exchange request has no Shopify counterpart; it is this app's own
record of a size-exchange conversation, tracked through to admin-driven fulfillment (see
docs/superpowers/specs/2026-08-20-exchange-guide-agent-design.md).
"""

from dataclasses import dataclass
from typing import Literal

ExchangeStatus = Literal[
    "requested", "return_picked_up", "qc_passed", "qc_failed",
    "replacement_dispatched", "delivered",
]


@dataclass(frozen=True)
class ExchangeRequest:
    id: int
    order_gid: str
    order_name: str
    phone_e164: str
    requested_size: str
    status: ExchangeStatus
    requested_at: str  # raw ISO-8601, same convention as Order.created_at
    return_tracking_url: str | None
    replacement_tracking_url: str | None
    updated_at: str  # raw ISO-8601
