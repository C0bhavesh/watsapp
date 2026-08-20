"""Size-exchange guide agent.

Checks eligibility (app/core/exchange_eligibility.py, computed in Python -- never by the
model), collects the requested size, and creates the exchange record itself once an eligible
order + size are established in conversation (owner-directed: no button-tap confirmation step,
see docs/superpowers/specs/2026-08-20-exchange-guide-agent-design.md's "Mutation-safety note").

Deliberately does NOT use the shared HANDOFF_JSON_CONTRACT from app/agents/base.py: this
agent's JSON reply needs a THIRD field (create_exchange) beyond reply/handoff, and this
session's own error_learnings.md documents exactly the bug class that risks -- a shared
fragment appended after a local instruction can silently override it. customer_support.py
already sets the precedent of writing its own self-contained handoff wording instead of the
shared contract; this agent follows the same precedent, one level further.
"""

from datetime import UTC, datetime

from app.agents.base import (
    AgentContext,
    AgentReply,
    extract_json_blob,
    extract_reply_text,
    model_asked_for_handoff,
    personality_for,
)
from app.channels.copy import copy_for
from app.core.exchange_eligibility import check_exchange_eligibility
from app.core.exchange_models import ExchangeRequest
from app.providers.base import Message, ProviderError
from app.shopify.models import AuthorizedOrder
from app.store.base import ExchangeStore

_SYSTEM_TEMPLATE = """{personality}

The customer wants to exchange an item from their own order for a DIFFERENT SIZE. This store
does NOT offer a color or product exchange -- only a size exchange. If the customer asks for
anything other than a size change, tell them plainly that only size exchanges are available.

Below is each of the customer's orders and whether Thetavas' automated eligibility check
allows an exchange for it right now (delivered within the last 48 hours, order not
cancelled). This fact is already decided -- relay the reason given exactly as stated, never
recompute or guess the window yourself, and never offer an exchange for an order marked not
eligible.

{order_context}

Once you know BOTH which specific order the customer means AND the exact size they want,
confirm it back to them and describe this process in your own natural words (do not copy this
verbatim, keep your own warm tone):

1. We will initiate the return pickup -- our courier partner collects it within 1-2 business
   days.
2. Our Quality Check team inspects it: it must be unused, undamaged, with all original
   components. A used, torn, damaged, or incomplete item may not be eligible.
3. Once it passes inspection, we dispatch the replacement the SAME DAY.
4. The replacement arrives within 4-7 business days depending on location and courier -- you
   will get a tracking link once it ships.

{existing_requests}

Respond with STRICT JSON only, no other text:
{{"reply": "<your reply to the customer>", "handoff": <true or false>, "create_exchange": \
{{"order_gid": "<the exact order gid from the list above>", "size": "<the exact size>"}} or \
null}}

Only set "handoff" to true if the customer explicitly asks to speak with a person, or you
genuinely cannot help them with what you know -- never merely because one detail is still
missing. Set "create_exchange" ONLY on the turn where you have confirmed BOTH a specific
ELIGIBLE order and the exact size the customer wants -- otherwise it must be null. Never set
both "handoff" and "create_exchange" together.
"""


def _order_context_line(order: AuthorizedOrder, now: datetime) -> str:
    eligibility = check_exchange_eligibility(order.order, now)
    status = "eligible" if eligibility.eligible else "NOT eligible"
    return f"- order {order.order.name} (gid {order.order.gid}): {status} -- {eligibility.reason}"


def _order_context(orders: list[AuthorizedOrder], now: datetime) -> str:
    if not orders:
        return "No order is linked to this WhatsApp number yet. Ask for their order number."
    return "\n".join(_order_context_line(o, now) for o in orders)


def _existing_request_line(request: ExchangeRequest) -> str:
    tracking = (
        f", return tracking: {request.return_tracking_url}"
        if request.return_tracking_url else ""
    )
    return (
        f"- order {request.order_name}: exchange to size {request.requested_size}, "
        f"status: {request.status}, requested {request.requested_at}{tracking}"
    )


def _existing_requests_block(requests: list[ExchangeRequest]) -> str:
    if not requests:
        return ""
    lines = "\n".join(_existing_request_line(r) for r in requests)
    return f"This customer's existing exchange requests, for status questions:\n{lines}"


def _validated_create_exchange(
    data: dict[str, object] | None, orders: list[AuthorizedOrder], now: datetime,
) -> tuple[str, str, str] | None:
    """Re-derive (order_gid, order_name, size) ONLY if the model's claim checks out against
    real data -- never trust the model's own claim of eligibility or of which order/gid it
    means. Returns None (silently, the caller logs) on any mismatch."""
    if data is None:
        return None
    raw = data.get("create_exchange")
    if not isinstance(raw, dict):
        return None
    order_gid = raw.get("order_gid")
    size = raw.get("size")
    if not isinstance(order_gid, str) or not isinstance(size, str) or not size.strip():
        return None
    matching = next((o for o in orders if o.order.gid == order_gid), None)
    if matching is None:
        return None
    if not check_exchange_eligibility(matching.order, now).eligible:
        return None
    return matching.order.gid, matching.order.name, size.strip()


async def run(context: AgentContext, exchanges: ExchangeStore) -> AgentReply:
    """Handle a size-exchange conversation: relay eligibility, collect the size, create the
    request once both are confirmed, and answer status questions from existing requests."""
    fallback = copy_for("error_fallback", context.language)
    now = datetime.now(UTC)
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, now),
        existing_requests=_existing_requests_block(context.exchange_requests),
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

    data = extract_json_blob(result.text)
    reply_text = extract_reply_text(result.text, fallback)
    handoff = model_asked_for_handoff(data)

    validated = _validated_create_exchange(data, context.orders, now)
    if validated is not None:
        order_gid, order_name, size = validated
        await exchanges.create(order_gid, order_name, context.phone_e164, size)

    return AgentReply(text=reply_text, handoff=handoff)
