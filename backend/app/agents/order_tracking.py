from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
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

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
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


def _order_context(orders: list[AuthorizedOrder]) -> str:
    """Format the order context for the system prompt."""
    if not orders:
        return "No order is linked to this WhatsApp number yet. Ask for their order number."
    lines = []
    for o in orders:
        lines.append(
            f"- order {o.order.name}: payment status {o.order.financial_status or 'unknown'}, "
            f"fulfillment {o.order.fulfillment_status or 'not dispatched'}, "
            f"cancelled: {o.order.is_cancelled()}, "
            f"cancel eligible: {_is_cancel_eligible(o)}"
        )
    return "\n".join(lines)


async def run(context: AgentContext) -> AgentReply:
    """Handle order tracking queries.

    Calls the LLM provider with order context and returns a parsed reply.
    On provider error, returns a safe fallback message.
    """
    fallback = copy_for("error_fallback", "en")
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=PERSONALITY, order_context=_order_context(context.orders)
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
        return AgentReply(text=fallback)
    return AgentReply(text=extract_reply_text(result.text, fallback))
