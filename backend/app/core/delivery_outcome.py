"""Shopify-only fallback rule for "was this fulfillment genuinely delivered?".

Used ONLY as the fallback in the delivery-status sweep job when the ad2ship page cannot be
read -- it is intentionally leaky (Shopify still marks some RTOs as DELIVERED), it only has to
beat blindly sending. The primary signal is the ad2ship page; this is the cheap second-best.

Pure function of a single Fulfillment: no I/O, no logging, no exceptions. It treats a shipment
as genuinely delivered only when Shopify's display status says DELIVERED *and* the DELIVERED
event is the LAST thing that happened -- a later ATTEMPTED/failure scan (a marked-delivered-then-
returned shipment) means it was not really delivered. `events` is oldest-first (ascending by
happened_at), so the latest event is the lexical max of the ISO-8601 stamps.
"""

from app.shopify.ad2ship import Ad2shipTracking
from app.shopify.models import TERMINAL_SHIPMENT_STATES, Fulfillment

_DELIVERED = "DELIVERED"


def fulfillment_is_genuinely_delivered(f: Fulfillment) -> bool:
    if f.display_status != _DELIVERED or not f.events:
        return False
    delivered_at = next((e.happened_at for e in f.events if e.status == _DELIVERED), None)
    if delivered_at is None:
        return False
    return delivered_at == max(e.happened_at for e in f.events)


def normalized_shipment_status(t: Ad2shipTracking) -> str | None:
    """Map a live ad2ship snapshot to the store's normalized ``shipment_status`` token, or None.

    The single shared normalizer for every writer of ``Fulfillment.shipment_status`` (the delivery
    sweep ``jobs/delivery_confirm.py`` and the order-tracking agent) -- so the two can never drift.
    Only an upstream-CONFIRMED terminal outcome (``is_delivered_to_customer()`` / ``is_rto()``) is
    written as a terminal token; a raw badge that merely equals a terminal token (a stray
    ``"failure"`` / ``"rto"`` / ``"delivered"`` string not backed by the ``is_*`` checks) is a data
    anomaly that must NOT pin the store's monotonic guard, so the write is skipped (None). Genuine
    non-terminal movement (``in_transit`` / ``out_for_delivery`` / ``attempted_delivery``) passes
    through unchanged.
    """
    if t.is_delivered_to_customer():
        return "delivered"
    if t.is_rto():
        return "rto"
    if t.status in TERMINAL_SHIPMENT_STATES:
        return None
    return t.status
