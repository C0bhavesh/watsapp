from collections.abc import Sequence

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
from app.providers.base import Message, ProviderError
from app.shopify.models import AuthorizedOrder

_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}

Store cancellation policy: orders can only be cancelled BEFORE they are dispatched. Once
dispatched, cancellation is not possible -- if the customer asks to cancel a dispatched order,
tell them clearly and do not offer a cancel option for it.

If the customer wants to cancel an order that IS still eligible, tell them you'll bring up a
Confirm/Cancel button for them to tap -- you never cancel anything yourself.

{contract}
"""


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


def _order_line(order: AuthorizedOrder, reveal_fields: Sequence[str]) -> str:
    """Render one order using ONLY the fields the admin approved for disclosure.

    ``AdminControls.reveal_fields`` allows ``order_number`` / ``email`` / ``status``.
    ``order_number`` is the order name; ``status`` covers the whole payment/fulfillment/
    cancellation picture, cancel-eligibility included (it is derived from fulfillment and
    cancellation state, so it discloses nothing beyond them). ``email`` has never been rendered
    into this prompt, so there is nothing to gate for it. Withheld fields are omitted from the
    prompt entirely rather than merely "not to be mentioned" -- what the model never sees, it
    can never leak.
    """
    label = f"order {order.order.name}" if "order_number" in reveal_fields else "an order"
    if "status" not in reveal_fields:
        return f"- {label} (the store has not approved sharing its status over WhatsApp)"
    return (
        f"- {label}: payment status {order.order.financial_status or 'unknown'}, "
        f"fulfillment {order.order.fulfillment_status or 'not dispatched'}, "
        f"cancelled: {order.order.is_cancelled()}, "
        f"cancel eligible: {_is_cancel_eligible(order)}"
    )


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
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, context.reveal_fields),
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
