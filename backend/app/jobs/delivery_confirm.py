"""Delivery-confirmation sweep job — the RTO-aware gate before `order_delivered` is sent.

A `fulfillments/update` webhook whose ``shipment_status`` is exactly "delivered" no longer sends
``order_delivered`` immediately: Delhivery stamps that same status on an RTO's final "delivered back
to origin" scan exactly as it does on a genuine customer delivery (the tavas3908 bug). Task 5
instead parks a ``pending_delivery_confirmations`` row due ~2h later. This job sweeps the DUE rows
and decides, per shipment, whether the customer was really delivered to:

1. Primary signal — the ad2ship public tracking page (``fetch_tracking``): it distinguishes a
   genuine customer delivery from an RTO, which Shopify's own status cannot.
2. Fallback — used when ad2ship can't be read (returns ``None``) OR reports a non-terminal
   (not-yet-resolved, e.g. still in transit) status: the intentionally leaky Shopify heuristic
   (``fulfillment_is_genuinely_delivered``). It still marks some RTOs as DELIVERED, but it only has
   to beat blindly sending.

Outcomes: a confirmed genuine delivery sends ``order_delivered`` and marks the row ``sent``; an RTO
is recorded (``state=rto``, ``shipment_status=rto``) and NEVER messaged; anything inconclusive
leaves the row ``pending`` for the next run; a row older than ``_ABANDON_AFTER`` is abandoned. One
row's unexpected failure never aborts the batch (mirrors ``jobs.reconcile.run_reconcile_cancels``).
The ``send_mode`` kill switch is enforced downstream inside the inline send, not here.
"""

import logging
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any

from app.channels.shopify_orders import customer_display_name
from app.channels.shopify_webhook import (
    TEMPLATE_NAME_DELIVERED,
    _enqueue_and_send_fulfillment_notification,
)
from app.core.delivery_outcome import (
    fulfillment_is_genuinely_delivered,
    normalized_shipment_status,
)
from app.deps import Container
from app.shopify.ad2ship import fetch_tracking
from app.shopify.models import Order
from app.store.base import PendingDeliveryConfirmation

logger = logging.getLogger("app.jobs.delivery_confirm")

_BATCH_LIMIT = 50
_ABANDON_AFTER = timedelta(days=7)


async def _send(c: Container, row: PendingDeliveryConfirmation, order: Order) -> bool:
    """Enqueue + inline-send ONE ``order_delivered`` notification; mark the row ``sent`` ONLY when a
    durable outbound row was actually created. Returns that success bool so the caller counts the
    row correctly.

    ``_enqueue_and_send_fulfillment_notification`` never raises and enforces the ``send_mode`` kill
    switch downstream (a suppressed send leaves the queued row untouched, zero Meta calls — but the
    row IS still enqueued, so this returns ``True`` and the confirmation is genuinely queued for
    later delivery, not lost). It returns ``False`` only when the enqueue itself failed (DB error /
    no row created); in that case we do NOT advance to ``sent``, so the sweep retries the row.
    """
    ok = await _enqueue_and_send_fulfillment_notification(
        c,
        order_gid=row.order_gid,
        dedupe_key=f"fulfillment_delivered:{row.fulfillment_gid}",
        phone=row.phone_e164,
        template=TEMPLATE_NAME_DELIVERED,
        body_params=[customer_display_name(order), order.name],
    )
    if ok:
        await c.ingest.set_delivery_confirmation_state(row.fulfillment_gid, "sent")
    return ok


def _log_row_failure(fulfillment_gid: str, exc: BaseException) -> None:
    """PII-free per-row failure log: exception TYPE + last-frame location only.

    Never ``str(exc)`` (its text can echo a tracking number/URL) and never the awb/phone/tracking
    text — mirrors ``shopify_webhook._log_notify_failure``. The fulfillment gid is an opaque Shopify
    id (not PII) and makes a genuine future bug locatable in production.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    location = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else "unknown"
    logger.error(
        "delivery confirm: row failed gid=%s type=%s at=%s",
        fulfillment_gid, type(exc).__name__, location,
    )


async def run_delivery_confirm(c: Container) -> dict[str, Any]:
    now = datetime.now(UTC)
    due_rows = await c.ingest.due_delivery_confirmations(now, limit=_BATCH_LIMIT)
    sent = rto = pending = abandoned = errors = 0
    for row in due_rows:
        # One row's unexpected failure (store hiccup, programming bug) must degrade to a logged skip
        # + errors counter, never abort the batch or crash the sweep (mirrors reconcile_cancels).
        try:
            if now - row.due_at > _ABANDON_AFTER:
                # Parked far too long to still be actionable -> stop retrying it.
                await c.ingest.set_delivery_confirmation_state(row.fulfillment_gid, "abandoned")
                abandoned += 1
                continue

            order = await c.ingest.get_mirrored_order(row.order_gid)
            if order is None:
                # Mirror miss (order not mirrored yet / raced): nothing to render or send. Leave
                # the row pending so the next run retries once the mirror catches up. gid-only log.
                logger.info(
                    "delivery confirm: no mirrored order for %s; left pending", row.order_gid
                )
                errors += 1
                continue

            fulfillment = next(
                (f for f in order.fulfillments if f.gid == row.fulfillment_gid), None
            )
            awb = fulfillment.tracking_number if fulfillment else None

            # Trust our OWN stored ad2ship-derived terminal state BEFORE the leaky Shopify D5
            # fallback can override it. Task 7's agent writes shipment_status from the SAME reliable
            # ad2ship signal whenever a customer asks about their order, so in the exact window the
            # D5 heuristic exists for (ad2ship unreadable now) a shipment we ALREADY positively
            # identified as RTO must never be re-classified as delivered, and one already confirmed
            # delivered can be sent without re-fetching. Both stored states are terminal.
            if fulfillment is not None and fulfillment.shipment_status == "rto":
                await c.ingest.set_delivery_confirmation_state(row.fulfillment_gid, "rto")
                rto += 1
                continue
            if fulfillment is not None and fulfillment.shipment_status == "delivered":
                if await _send(c, row, order):
                    sent += 1
                else:
                    errors += 1
                continue

            # ad2ship is the reliable RTO signal. fetch_tracking never raises: any transport/parse
            # failure or missing awb yields None, which routes to the Shopify fallback below.
            tracking = await fetch_tracking(c.http, awb) if awb else None

            if tracking is not None and tracking.is_delivered_to_customer():
                if await _send(c, row, order):
                    # sent += 1 AFTER the status write (invariant: a row counts exactly once; if the
                    # write raises, the outer except counts it as errors, never as both).
                    await c.ingest.set_fulfillment_shipment_status(
                        row.fulfillment_gid, "delivered",
                        tracking_city=tracking.current_city, tracking_hub=tracking.current_hub,
                        last_scan=tracking.last_scan_remark or tracking.last_scan,
                        expected_date=tracking.expected_date, checked_at=now,
                    )
                    sent += 1
                else:
                    # Enqueue failed -> notification not durable. Leave pending, retry next run.
                    errors += 1
            elif tracking is not None and tracking.is_rto():
                # A returned shipment: record it and NEVER congratulate the customer.
                await c.ingest.set_delivery_confirmation_state(row.fulfillment_gid, "rto")
                await c.ingest.set_fulfillment_shipment_status(
                    row.fulfillment_gid, "rto",
                    tracking_city=tracking.current_city, tracking_hub=tracking.current_hub,
                    last_scan=tracking.last_scan_remark or tracking.last_scan,
                    expected_date=tracking.expected_date, checked_at=now,
                )
                rto += 1
            else:
                # ad2ship unreadable (None) or a non-terminal state -> Shopify fallback.
                # normalized_shipment_status (shared with the agent) returns the pass-through
                # non-terminal token here (is_delivered/is_rto already both False above), or None
                # for a raw badge that literally equals a terminal token without upstream
                # confirmation -- a data anomaly that must not wrongly pin the monotonic guard.
                normalized = (
                    normalized_shipment_status(tracking) if tracking is not None else None
                )
                if tracking is not None and normalized is not None:
                    # Persist whatever ad2ship DID give us (real but non-terminal movement) so
                    # Task 7's agent enrichment sees the freshest snapshot even though we're not
                    # acting.
                    await c.ingest.set_fulfillment_shipment_status(
                        row.fulfillment_gid, normalized,
                        tracking_city=tracking.current_city, tracking_hub=tracking.current_hub,
                        last_scan=tracking.last_scan_remark or tracking.last_scan,
                        expected_date=tracking.expected_date, checked_at=now,
                    )
                # get_order_fulfillments already swallows ShopifyError into () internally, so an
                # empty tuple (scope missing/outage) OR no gid match is simply "fallback
                # inconclusive" -> leave the row pending for the next run.
                shopify_fulfillments = await c.shopify.get_order_fulfillments(row.order_gid)
                shopify_f = next(
                    (f for f in shopify_fulfillments if f.gid == row.fulfillment_gid), None
                )
                if shopify_f is not None and fulfillment_is_genuinely_delivered(shopify_f):
                    if await _send(c, row, order):
                        await c.ingest.set_fulfillment_shipment_status(
                            row.fulfillment_gid, "delivered", checked_at=now
                        )
                        sent += 1
                    else:
                        errors += 1
                else:
                    pending += 1
        except Exception as exc:  # noqa: BLE001 — one row never fails the whole run (see reconcile)
            errors += 1
            _log_row_failure(row.fulfillment_gid, exc)
    return {
        "swept": len(due_rows), "sent": sent, "rto": rto,
        "pending": pending, "abandoned": abandoned, "errors": errors,
    }
