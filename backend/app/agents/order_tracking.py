import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.agents.base import (
    HANDOFF_JSON_CONTRACT,
    AgentContext,
    AgentReply,
    extract_json_blob,
    extract_reply_text,
    model_asked_for_handoff,
    personality_for,
)
from app.channels.copy import copy_for
from app.core.delivery_estimate import estimate_delivery
from app.providers.base import Message, ProviderError
from app.shopify.ad2ship import Ad2shipTracking, fetch_tracking
from app.shopify.models import AuthorizedOrder, Fulfillment, LineItem, Money, Order

logger = logging.getLogger(__name__)

# ad2ship shipment states past which no further movement is expected -- answered from stored
# state, never re-fetched live. Mirrors the store's monotonic-terminal guard.
_TERMINAL_SHIPMENT_STATES = frozenset({"delivered", "failure", "rto"})
# How recent a cached ad2ship snapshot must be to reuse without a fresh courier call.
_FRESH_TRACKING_WINDOW = timedelta(minutes=30)


class TrackingStore(Protocol):
    """The one IngestStore capability this agent needs: persist a live ad2ship snapshot.

    Declared locally (not imported from ``app.store``) so ``agents`` depends on an interface it
    owns, never the store adapter -- same inward-pointing pattern as ``ProductSource`` in
    ``product_search``. ``IngestStore`` structurally satisfies it.
    """

    async def set_fulfillment_shipment_status(
        self,
        fulfillment_gid: str,
        shipment_status: str,
        *,
        tracking_city: str | None = None,
        tracking_hub: str | None = None,
        last_scan: str | None = None,
        expected_date: str | None = None,
        checked_at: datetime | None = None,
    ) -> None: ...

_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}
{format_hint}
Store cancellation policy: only Cash on Delivery orders can be cancelled, and only BEFORE
they are dispatched. Prepaid orders can never be cancelled, even if not yet dispatched --
if the customer asks to cancel a prepaid order, tell them clearly that prepaid orders can't
be cancelled once placed and do not offer a cancel option for it. Once a COD order is
dispatched, cancellation is not possible either -- if the customer asks to cancel a dispatched
order, tell them clearly and do not offer a cancel option for it.

If an order has shipped and tracking details are shown above, share the courier name, tracking
number, and the tracking link exactly as given so the customer can track it. Never invent a
tracking number or courier. If no tracking details are available for an order, do not claim it
has not shipped -- go by the fulfillment status field above instead, tell the customer the
tracking details are not available yet, and offer to have the team check.

If live courier-tracking lines (Current status, Currently at, Latest update, Expected delivery)
are shown for an order, relay them exactly as given so the customer knows where their parcel is
-- never invent a status, location, scan update, or delivery date, and never compute one
yourself.

If an "Estimated delivery" line is shown above for an order, relay it to the customer exactly
as given, including the caveat that it is an estimate and may vary -- never state it as a firm
promised date, and never compute or guess a different delivery date yourself. If no estimated
delivery line is shown for an order, do not invent one.

If the customer wants to cancel an order that IS still eligible, tell them you'll bring up a
Confirm/Cancel button for them to tap -- you never cancel anything yourself.

When a Cash on Delivery order is still marked Pending, explain that as normal -- the amount is
simply collected on delivery, not something to worry about -- rather than sounding alarmed.

Format order-detail replies warmly and clearly, for example:

Hey there! 👋
Here are your order details:

*Order ID:* tavas9241
*Status:* Pending (Cash on Delivery — collected on delivery) 💵
*Fulfillment:* Not yet dispatched 📦

*Items:*
- *Product Name* (Blue / M) — ₹999

Use bold (*like this*) for the order ID, status, and item names, a warm greeting, and light,
natural emoji use -- not on every line, and never more than the message needs.

{contract}
"""


def _format_money(money: Money) -> str:
    """Render a price for a customer-facing WhatsApp reply.

    INR gets its symbol (this store's currency); anything else falls back to the raw currency
    code rather than guessing a symbol. A trailing ".00" is stripped for a cleaner look
    ("999.00" -> "₹999", not "₹999.00") -- non-".00" amounts are left exactly as Shopify sent
    them (no rounding).
    """
    amount = money.amount[:-3] if money.amount.endswith(".00") else money.amount
    if money.currency == "INR":
        return f"₹{amount}"
    return f"{amount} {money.currency}"


def _line_item_line(item: LineItem) -> str:
    variant = f" ({item.variant_title})" if item.variant_title else ""
    price = f" — {_format_money(item.price)}" if item.price else ""
    return f"- *{item.title}*{variant}{price}"


def _is_cancel_eligible(order: AuthorizedOrder) -> bool:
    """Check if an order is eligible for cancellation.

    An order is cancel-eligible only if it is a Cash on Delivery order (by Shopify's OWN payment
    gateway -- ``is_cod_by_gateway()``, NOT the app-writable "cod" tag; security review 2026-08-22),
    is not already cancelled, AND has not yet been dispatched. Prepaid orders are never
    cancel-eligible, regardless of dispatch status (owner decision, 2026-08-21) -- COD is the only
    payment method this store allows a customer to cancel. fulfillment_status is the closest
    available signal to "has this shipped" without a live courier integration --
    UNFULFILLED/unset = not yet dispatched (cancel-eligible), anything else is treated
    as dispatched (not cancel-eligible).
    """
    if not order.order.is_cod_by_gateway():
        return False
    if order.order.is_cancelled():
        return False
    return order.order.fulfillment_status in (None, "UNFULFILLED")


def _tracking_line(fulfillment: Fulfillment) -> str:
    parts = []
    if fulfillment.tracking_company:
        parts.append(f"courier {fulfillment.tracking_company}")
    if fulfillment.tracking_number:
        parts.append(f"tracking number {fulfillment.tracking_number}")
    if fulfillment.tracking_url:
        parts.append(f"tracking link {fulfillment.tracking_url}")
    return "  - Tracking: " + ", ".join(parts)


def _stored_tracking(f: Fulfillment) -> Ad2shipTracking | None:
    """Build an Ad2shipTracking from the fulfillment's OWN cached tracking_* columns.

    Used when a live courier call is unnecessary (terminal shipment_status, or a snapshot still
    inside the freshness window). Returns None if there is no stored status to describe.
    """
    status = f.shipment_status
    if status is None:
        return None
    return Ad2shipTracking(
        status=status,
        status_label=status.replace("_", " ").title(),
        current_city=f.tracking_city,
        current_hub=f.tracking_hub,
        last_scan=f.tracking_last_scan,
        last_scan_remark=f.tracking_last_scan,
        last_scan_at=None,
        expected_date=f.tracking_expected_date,
    )


def _tracking_is_fresh(checked_at_iso: str | None, now: datetime) -> bool:
    """True when a stored ad2ship snapshot is recent enough to reuse without a live call."""
    if checked_at_iso is None:
        return False
    try:
        checked = datetime.fromisoformat(checked_at_iso)
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return abs(now - checked) <= _FRESH_TRACKING_WINDOW


async def _live_tracking_for(
    http: httpx.AsyncClient,
    ingest: TrackingStore,
    f: Fulfillment,
    *,
    now: datetime,
) -> Ad2shipTracking | None:
    """Return a live (or freshly cached) ad2ship snapshot for one fulfillment, or None.

    Never raises: any unexpected error degrades to None (the caller then renders today's static
    fallback). A store-write hiccup is swallowed too -- the customer's answer must not wait on
    persistence. Terminal or freshly cached states are answered from stored columns with no
    courier call.
    """
    try:
        if not f.has_tracking() or f.tracking_number is None:
            return None
        if f.shipment_status in _TERMINAL_SHIPMENT_STATES:
            return _stored_tracking(f)
        if _tracking_is_fresh(f.tracking_checked_at, now):
            return _stored_tracking(f)
        tracking = await fetch_tracking(http, f.tracking_number)
        if tracking is None:
            return None
        try:
            await ingest.set_fulfillment_shipment_status(
                f.gid,
                tracking.status,
                tracking_city=tracking.current_city,
                tracking_hub=tracking.current_hub,
                last_scan=(tracking.last_scan_remark or tracking.last_scan),
                expected_date=tracking.expected_date,
                checked_at=now,
            )
        except Exception as exc:  # best-effort persist -- never block the reply
            logger.warning(
                "live tracking persist failed in _live_tracking_for: type=%s",
                type(exc).__name__,
            )
        return tracking
    except Exception as exc:
        logger.warning(
            "live tracking enrichment failed in _live_tracking_for: type=%s",
            type(exc).__name__,
        )
        return None


def _live_tracking_lines(t: Ad2shipTracking) -> list[str]:
    """Render the live courier snapshot as prompt lines, omitting any absent field."""
    lines: list[str] = []
    if t.status_label:
        lines.append(f"  - Current status: {t.status_label}")
    location = t.current_hub or t.current_city
    if location:
        lines.append(f"  - Currently at: {location}")
    scan = t.last_scan_remark or t.last_scan
    if scan:
        if t.last_scan_at:
            lines.append(f"  - Latest update: {scan} ({t.last_scan_at})")
        else:
            lines.append(f"  - Latest update: {scan}")
    if t.expected_date:
        lines.append(f"  - Expected delivery: {t.expected_date}")
    return lines


def _delivery_estimate_line(order: Order) -> str | None:
    result = estimate_delivery(order, today=datetime.now(UTC).date())
    if result is None:
        return None
    return (
        f"  - Estimated delivery: {result.expected_date.isoformat()} "
        "(this is an estimate and may vary by 1-2 days)"
    )


def _order_line(
    order: AuthorizedOrder,
    reveal_fields: Sequence[str],
    live: dict[str, Ad2shipTracking] | None = None,
) -> str:
    """Render one order using ONLY the fields the admin approved for disclosure.

    ``AdminControls.reveal_fields`` allows ``order_number`` / ``email`` / ``status`` / ``items`` /
    ``tracking``. ``order_number`` is the order name; ``status`` covers the whole payment/
    fulfillment/cancellation picture, cancel-eligibility included (it is derived from fulfillment
    and cancellation state, so it discloses nothing beyond them); ``items`` adds each line item's
    product name, variant, and price; ``tracking`` adds each shipped fulfillment's courier,
    tracking number, and link (Q10). ``email`` has never been rendered into this prompt, so there
    is nothing to gate for it. Withheld fields are omitted from the prompt entirely rather than
    merely "not to be mentioned" -- what the model never sees, it can never leak. Tracking is
    rendered only within the status-approved block: a tracking link inherently reveals the order
    shipped, so if status is withheld tracking is withheld too (the more conservative gate).
    """
    live = live or {}
    label = f"order {order.order.name}" if "order_number" in reveal_fields else "an order"
    if "status" not in reveal_fields:
        return f"- {label} (the store has not approved sharing its status over WhatsApp)"
    cod_note = " (Cash on Delivery)" if order.order.is_cod() else ""
    lines = [
        f"- {label}: payment status {order.order.financial_status or 'unknown'}{cod_note}, "
        f"fulfillment {order.order.fulfillment_status or 'not dispatched'}, "
        f"cancelled: {order.order.is_cancelled()}, "
        f"cancel eligible: {_is_cancel_eligible(order)}"
    ]
    if "items" in reveal_fields and order.order.line_items:
        lines.extend(_line_item_line(item) for item in order.order.line_items)
    if "tracking" in reveal_fields:
        # Only fulfillments that actually carry tracking -- never fabricate a line for an
        # unshipped order or a label-only fulfillment with no tracking yet. When a live ad2ship
        # snapshot was enriched for a fulfillment, its lines follow the static Shopify line.
        for f in order.order.fulfillments:
            if not f.has_tracking():
                continue
            lines.append(_tracking_line(f))
            if f.gid in live:
                lines.extend(_live_tracking_lines(live[f.gid]))
    estimate_line = _delivery_estimate_line(order.order)
    if estimate_line is not None:
        lines.append(estimate_line)
    return "\n".join(lines)


def _order_context(
    orders: list[AuthorizedOrder],
    reveal_fields: Sequence[str],
    live: dict[str, Ad2shipTracking] | None = None,
) -> str:
    """Format the order context for the system prompt."""
    if not orders:
        return "No order is linked to this WhatsApp number yet. Ask for their order number."
    return "\n".join(_order_line(o, reveal_fields, live) for o in orders)


async def _enrich_live_tracking(
    http: httpx.AsyncClient,
    ingest: TrackingStore,
    orders: list[AuthorizedOrder],
    now: datetime,
) -> dict[str, Ad2shipTracking]:
    """Fetch (or reuse cached) ad2ship snapshots for every tracked fulfillment, keyed by gid."""
    live: dict[str, Ad2shipTracking] = {}
    for order in orders:
        for f in order.order.fulfillments:
            if not f.has_tracking():
                continue
            tracking = await _live_tracking_for(http, ingest, f, now=now)
            if tracking is not None:
                live[f.gid] = tracking
    return live


async def run(
    context: AgentContext,
    http: httpx.AsyncClient | None = None,
    ingest: TrackingStore | None = None,
) -> AgentReply:
    """Handle order tracking queries.

    Calls the LLM provider with order context and returns a parsed reply.
    On provider error, returns a safe fallback message.

    When ``http`` and ``ingest`` are supplied AND the admin approved the ``tracking`` field,
    each shipped fulfillment is enriched with a live ad2ship snapshot (current location, latest
    scan, expected date). Without those dependencies -- or with ``tracking`` withheld -- the
    reply falls back to today's static Shopify tracking line only. Enrichment never raises.
    """
    fallback = copy_for("error_fallback", context.language)
    format_hint = (
        f"\n{context.order_number_format_hint}\n" if context.order_number_format_hint else ""
    )
    live: dict[str, Ad2shipTracking] = {}
    if http is not None and ingest is not None and "tracking" in context.reveal_fields:
        live = await _enrich_live_tracking(http, ingest, context.orders, datetime.now(UTC))
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, context.reveal_fields, live),
        format_hint=format_hint,
        contract=HANDOFF_JSON_CONTRACT,
    )
    messages = [
        Message(role="system", content=system_prompt),
        *context.history,
        Message(role="user", content=context.user_text),
    ]
    try:
        result = await context.provider.complete(
            context.model, messages, context.api_key, context.timeout,
            extra_params=context.extra_params,
        )
    except ProviderError:
        # A transient provider failure is not an escalation -- handing off here would pause the
        # AI for 24h on every blip. Only the model's own judgment escalates.
        return AgentReply(text=fallback)
    return AgentReply(
        text=extract_reply_text(result.text, fallback),
        handoff=model_asked_for_handoff(extract_json_blob(result.text)),
    )
