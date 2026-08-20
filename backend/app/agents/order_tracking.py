from collections.abc import Sequence
from datetime import UTC, datetime

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
from app.shopify.models import AuthorizedOrder, Fulfillment, LineItem, Money, Order

_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}
{format_hint}
Store cancellation policy: orders can only be cancelled BEFORE they are dispatched. Once
dispatched, cancellation is not possible -- if the customer asks to cancel a dispatched order,
tell them clearly and do not offer a cancel option for it.

If an order has shipped and tracking details are shown above, share the courier name, tracking
number, and the tracking link exactly as given so the customer can track it. Never invent a
tracking number or courier. If no tracking details are available for an order, do not claim it
has not shipped -- go by the fulfillment status field above instead, tell the customer the
tracking details are not available yet, and offer to have the team check.

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

    An order is cancel-eligible if it is not already cancelled AND has not yet been dispatched.
    fulfillment_status is the closest available signal to "has this shipped" without
    a live courier integration — UNFULFILLED/unset = not yet dispatched (cancel-eligible),
    anything else is treated as dispatched (not cancel-eligible).
    """
    if order.order.is_cancelled():
        return False
    # displayFulfillmentStatus is the closest available signal to "has this shipped" without
    # a live courier integration (Q10: none is built) -- UNFULFILLED/unset = not yet
    # dispatched, anything else is treated as dispatched.
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


def _delivery_estimate_line(order: Order) -> str | None:
    result = estimate_delivery(order, today=datetime.now(UTC).date())
    if result is None:
        return None
    return (
        f"  - Estimated delivery: {result.expected_date.isoformat()} "
        "(this is an estimate and may vary by 1-2 days)"
    )


def _order_line(order: AuthorizedOrder, reveal_fields: Sequence[str]) -> str:
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
        # unshipped order or a label-only fulfillment with no tracking yet.
        lines.extend(
            _tracking_line(f) for f in order.order.fulfillments if f.has_tracking()
        )
    estimate_line = _delivery_estimate_line(order.order)
    if estimate_line is not None:
        lines.append(estimate_line)
    return "\n".join(lines)


def _order_context(orders: list[AuthorizedOrder], reveal_fields: Sequence[str]) -> str:
    """Format the order context for the system prompt."""
    if not orders:
        return "No order is linked to this WhatsApp number yet. Ask for their order number."
    return "\n".join(_order_line(o, reveal_fields) for o in orders)


async def run(context: AgentContext) -> AgentReply:
    """Handle order tracking queries.

    Calls the LLM provider with order context and returns a parsed reply.
    On provider error, returns a safe fallback message.
    """
    fallback = copy_for("error_fallback", context.language)
    format_hint = (
        f"\n{context.order_number_format_hint}\n" if context.order_number_format_hint else ""
    )
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, context.reveal_fields),
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
