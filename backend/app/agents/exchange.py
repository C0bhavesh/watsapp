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

import logging
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

logger = logging.getLogger(__name__)

_SYSTEM_TEMPLATE = """{personality}

The customer wants to exchange an item from their own order for a DIFFERENT SIZE. This store
does NOT offer a color or product exchange -- only a size exchange. If the customer asks for
anything other than a size change, tell them plainly that only size exchanges are available.

Below is each of the customer's orders and whether Thetavas' automated eligibility check
allows an exchange for it right now (delivered within the last 48 hours, order not
cancelled). This fact is already decided -- state the reason faithfully in the customer's own
language (English, Hindi, or Hinglish, per your instructions above), keeping the delivery date
and the 48-hour window exactly as given -- never recompute or guess the window yourself -- and
never offer an exchange for an order marked not eligible.

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
missing. When you do set "handoff" to true, your reply must also tell the customer you are
connecting them with the team -- the flag alone tells them nothing, and it is what actually
brings a teammate into this chat. Set "handoff" to false on every reply you handle yourself.
Set "create_exchange" ONLY on the turn where you have confirmed BOTH a specific ELIGIBLE order
and the exact size the customer wants -- otherwise it must be null. Never set both "handoff"
and "create_exchange" together.
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


_MAX_SIZE_LENGTH = 20


def _validated_create_exchange(
    data: dict[str, object] | None,
    orders: list[AuthorizedOrder],
    existing_requests: list[ExchangeRequest],
    now: datetime,
) -> tuple[str, str, str] | None:
    """Re-derive (order_gid, order_name, size) ONLY if the model's claim checks out against
    real data -- never trust the model's own claim of eligibility or of which order/gid it
    means. Returns None on any mismatch. Every rejection of an ACTUAL create_exchange claim
    (as opposed to a turn that simply made none) is logged with its reason, so a customer
    told "done" by the model's own reply text never fails silently with zero operator-visible
    signal (final-review fix wave, finding 1)."""
    if data is None:
        return None
    raw = data.get("create_exchange")
    if not isinstance(raw, dict):
        return None
    order_gid = raw.get("order_gid")
    size = raw.get("size")
    if not isinstance(order_gid, str) or not isinstance(size, str):
        logger.warning(
            "exchange create_exchange rejected: order_gid/size not both strings (order_gid=%r)",
            order_gid,
        )
        return None
    stripped_size = size.strip()
    if not stripped_size or len(stripped_size) > _MAX_SIZE_LENGTH:
        logger.warning(
            "exchange create_exchange rejected for order %s: size blank or over %d chars",
            order_gid, _MAX_SIZE_LENGTH,
        )
        return None
    matching = next((o for o in orders if o.order.gid == order_gid), None)
    if matching is None:
        logger.warning("exchange create_exchange rejected: unknown order_gid %s", order_gid)
        return None
    if not check_exchange_eligibility(matching.order, now).eligible:
        logger.warning(
            "exchange create_exchange rejected for order %s: order not eligible", order_gid,
        )
        return None
    # Repeat-write guard: an eligible order stays eligible for the rest of its 48h window, so
    # without this ANY later turn where the model re-emits create_exchange (a "yes"/"confirm",
    # or the model re-reading its own prior confirmation from history) would pass every check
    # above again and insert a second row -- a real duplicate courier pickup. There is no
    # cancel-and-re-request flow in scope, so any existing request for this order, regardless
    # of status, blocks a new one (a qc_failed predecessor included -- owner decision: that is
    # terminal for this order's bot-driven flow, a human handles any further exchange on it).
    # Backed by a DB-level unique index too, schema.sql.
    if any(r.order_gid == matching.order.gid for r in existing_requests):
        logger.warning(
            "exchange create_exchange rejected for order %s: an exchange request already "
            "exists for it",
            order_gid,
        )
        return None
    return matching.order.gid, matching.order.name, stripped_size


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

    # Never write on a turn where the customer is shown the generic fallback reply -- a
    # completion with a usable create_exchange but a missing/blank reply would otherwise create
    # a record the customer was never told succeeded (a phantom write behind an error message).
    if reply_text != fallback:
        validated = _validated_create_exchange(
            data, context.orders, context.exchange_requests, now,
        )
        if validated is not None:
            order_gid, order_name, size = validated
            try:
                await exchanges.create(order_gid, order_name, context.phone_e164, size)
            except Exception:
                # A store failure must not sacrifice the reply the model already composed for
                # this turn -- the customer still gets `reply_text`; whether the write actually
                # landed is unknown to them either way, same as any other silent-reject case
                # above. Logged (not swallowed) so it is operator-discoverable.
                logger.exception(
                    "exchange request write failed for order %s (%s)", order_name, order_gid,
                )
            else:
                # The admin panel only surfaces an exchange request by joining onto a MIRRORED
                # order (find_mirrored_orders_by_phone) -- but this agent resolves orders via
                # LIVE Shopify, not the mirror (see the mirror-first-breaks-eligibility error
                # learning), so a create can land for an order the mirror never ingested,
                # invisible to that view forever. Logging every successful create unconditionally
                # keeps it discoverable via log search regardless of mirror status (final-review
                # fix wave, finding 4).
                logger.info(
                    "exchange request created for order %s (%s)", order_name, order_gid,
                )

    return AgentReply(text=reply_text, handoff=handoff)
