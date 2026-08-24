"""The Phase 4 conversation engine -- turns one fresh inbound WhatsApp text into a persisted,
conditionally-sent reply.

Lives in ``core/``, not ``channels/``, so ``channels/whatsapp.py`` stays limited to webhook
parsing/HMAC/idempotency/sending (``fastapi-layering.md``): the webhook handler's only entry
point into this module is ``run_turn``. This module is the orchestrator -- it necessarily wires
together admin controls, order resolution, knowledge, the intent router, the five specialist
agents, and the WhatsApp sender, so (unlike ``order_resolver``/``memory``/``sanitize``) it does
import concrete adapters rather than depending only on Protocols; that is the intended shape of
an orchestration module, not an oversight.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from app.admin.controls import AdminControls, load_controls
from app.agents import (
    customer_support,
    exchange,
    order_tracking,
    policy,
    product_search,
    recommendations,
)
from app.agents.base import AgentContext, AgentReply
from app.agents.router import Intent, classify_intent
from app.channels.copy import copy_for
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_inbound import InboundText
from app.channels.whatsapp_sender import WhatsAppSendError, send_text
from app.core.exchange_models import ExchangeRequest
from app.core.memory import load_history, persist_turn
from app.core.mirror_order_source import MirrorOrderSource
from app.core.order_resolver import OrderSource, resolve_by_order_name, resolve_by_phone
from app.core.phone import normalize_phone
from app.core.sanitize import strip_markdown
from app.deps import Container, active_llm
from app.knowledge.loader import SEEDS_DIR, KnowledgeLoader
from app.providers.base import LLMProvider, Message
from app.shopify.models import ORDER_NUMBER_DIGIT_LENGTH, AuthorizedOrder

logger = logging.getLogger("app.core.conversation")

# Defense-in-depth against the platform-level function timeout, which is NOT set in this repo's
# vercel.json (Vercel treats `functions` and `builds` as mutually exclusive -- `builds`/`routes`
# is what makes this ASGI app's catch-all routing work, so `maxDuration` must instead be set via
# the Vercel dashboard -- Project Settings -> Functions -- once the project is connected; see
# error_learnings.md and _pipeline_status.md). 55s is chosen to sit comfortably under whatever
# dashboard duration is configured there (60s was the original reasoning and is still the
# expected range) so a near-deadline turn is cancelled and logged here (see `except TimeoutError`
# in `run_turn`) well before Vercel itself would kill the function and return an uncaught 504.
# That distinction matters even once the dashboard limit is raised: `record_if_new` has already
# marked the inbound message as seen by the time this runs, so an uncaught 504 makes Meta's
# retry of the same message get silently dropped as a duplicate -- the customer never gets a
# reply, and nothing is logged. A caught TimeoutError here at least leaves a WARNING.
TURN_TIMEOUT_SECONDS = 55.0

# How long the AI stays silent after a handoff, so a human can take the conversation over in
# the same chat. Self-expiring -- no admin action is needed to resume (design spec).
HANDOFF_PAUSE_WINDOW = timedelta(hours=24)

# Thetavas order names look like "tavas3733" -- the store prefix + the order number, with NO
# "#" prefix (see find_order_by_name / phase0-verification-results). Tried in priority order:
# an explicit "tavas<digits>" or "#<digits>" token is an unambiguous order-number attempt at
# any digit count; a bare run of digits only counts as a candidate at 3+ digits, since shorter
# numbers (dates, quantities) are far more likely to be something else. `[0-9]` (not `\d`) and
# a single search each keep this linear on arbitrarily long / non-ASCII input -- no
# catastrophic backtracking and no Unicode-digit surprises. `resolve_by_order_name` re-guards
# whatever candidate is extracted here (injection guard + live ownership check), so this only
# has to surface a plausible token.
_TAVAS_PREFIXED_RE = re.compile(r"tavas[0-9]+", re.IGNORECASE | re.ASCII)
_HASH_PREFIXED_RE = re.compile(r"#[0-9]+")
_BARE_DIGITS_RE = re.compile(r"\b[0-9]{3,}\b")


def _extract_order_number_candidate(text: str) -> str | None:
    """Find the best plausible order-number token in free text.

    Collects every candidate across all three forms (explicit "tavas<digits>", "#<digits>",
    and a bare 3+ digit run) instead of stopping at the first match: a message that also
    contains a pincode, phone number, or date fragment must not let one of those outrank the
    real order number just because it happens to appear earlier in the text. Prefers whichever
    candidate's digit count matches the store's actual order-number length; only when none does
    it falls back to the first candidate found (tavas/#/bare priority order), so the caller can
    still produce a "please recheck" hint instead of treating the message as having no number
    at all.
    """
    candidates = (
        [m.group(0) for m in _TAVAS_PREFIXED_RE.finditer(text)]
        + [m.group(0) for m in _HASH_PREFIXED_RE.finditer(text)]
        + [m.group(0) for m in _BARE_DIGITS_RE.finditer(text)]
    )
    if not candidates:
        return None
    for candidate in candidates:
        if len(re.sub(r"[^0-9]", "", candidate)) == ORDER_NUMBER_DIGIT_LENGTH:
            return candidate
    return candidates[0]

# Internal notification sent to AdminControls.owner_alert_number when a handoff triggers. Plain
# text, no emojis (client-facing copy rule), and carries only what the owner needs to act: which
# customer to reply to and how long the AI will stay out of the way.
OWNER_ALERT_TEMPLATE = (
    "Thetavas bot: {customer_phone} has asked for a person. The AI is paused for that chat for "
    "the next {hours} hours -- please reply to them on WhatsApp directly."
)


async def _run_agent(context: AgentContext, intent: Intent, c: Container) -> AgentReply:
    if intent == "order_tracking":
        return await order_tracking.run(context)
    if intent == "product_search":
        return await product_search.run(context, c.shopify)
    if intent == "policy":
        return await policy.run(context)
    if intent == "recommendations":
        return await recommendations.run(context, c.shopify)
    if intent == "exchange":
        return await exchange.run(context, c.exchanges)
    return await customer_support.run(context)


async def run_turn(c: Container, event: InboundText, budget_seconds: float | None = None) -> None:
    """Run one conversation turn for a fresh inbound text message and send the reply.

    Failures anywhere in this pipeline (Shopify, the LLM provider, sending the reply) are
    swallowed here -- the webhook must still ack 200 for a message it already deduped, and a
    failed reply is a degraded conversation, not a failed webhook delivery. A turn that runs
    past its timeout is cancelled and logged as a WARNING rather than left to an uncaught
    platform-level 504 (see the module docstring for why that distinction matters).

    ``budget_seconds`` is what is left of the CALLER's whole-request budget (the webhook
    handler batches several messages per delivery); the turn gets whichever of that and
    ``TURN_TIMEOUT_SECONDS`` is smaller, so neither ceiling can be exceeded.
    """
    timeout = (
        TURN_TIMEOUT_SECONDS
        if budget_seconds is None
        else min(budget_seconds, TURN_TIMEOUT_SECONDS)
    )
    try:
        async with asyncio.timeout(timeout):
            await _run_turn(c, event)
    except TimeoutError:
        logger.warning("conversation turn timed out after %.2fs", timeout)
    except Exception:
        logger.exception("conversation turn failed for a fresh inbound text message")


async def _run_turn(c: Container, event: InboundText) -> None:
    controls = await load_controls(c.config)
    if controls.send_mode == "off":
        return
    wa_cfg = await load_whatsapp_config(c.config)
    if wa_cfg is None:
        return

    phone = normalize_phone(event.wa_id)
    # DPDP right-to-erasure depends on this key: POST /admin/erasure deletes conversations and
    # messages with `WHERE user_id = $1` bound to the customer's E.164 `phone_e164`. Keying the
    # conversation on the raw Meta `wa_id` ("919876543210") instead made that delete match zero
    # rows -- the endpoint reported success while the whole chat history survived. Fall back to
    # the raw wa_id only when normalization fails, so an unparseable number degrades to a
    # working (if un-erasable) conversation rather than crashing the turn.
    conversation_id, history = await load_history(c.conversations, phone or event.wa_id)
    now = datetime.now(UTC)
    paused_until = await c.conversations.get_paused_until(conversation_id)
    if paused_until is not None and now < paused_until:
        await c.conversations.append_message(conversation_id, "user", event.text)
        logger.info(
            "inbound message stored but not answered: conversation %s is paused until %s",
            conversation_id, paused_until,
        )
        return

    # A single cheap DB count -- unlike order resolution below, it costs no Shopify call, so
    # it stays unconditional and every agent's prompt can use it.
    order_count = await c.ingest.count_orders_by_phone(phone) if phone else 0
    is_vip = order_count >= controls.vip_order_count_threshold

    llm = await active_llm(c.settings, c.config)
    handoff = False
    if llm is None:
        reply_text = copy_for("error_fallback", controls.default_language)
    else:
        try:
            reply_text, handoff = await _agent_reply(
                c, event, history, phone, is_vip, llm, controls
            )
        except Exception:
            # Each agent catches ProviderError itself; anything ELSE raised in this section
            # (a KeyError in prompt formatting, an unexpected store error) used to reach
            # run_turn's blanket handler, which logs and sends nothing. A specialist failure
            # must degrade to the fixed copy and still be delivered -- never silence.
            logger.exception("agent dispatch failed; degrading to the fixed fallback reply")
            reply_text = copy_for("error_fallback", controls.default_language)
            handoff = False

    if not reply_text.strip():
        # A completion that's e.g. only a code fence, or an agent's own degraded reply, can
        # strip down to nothing -- never persist/send a blank WhatsApp message.
        reply_text = copy_for("error_fallback", controls.default_language)

    if handoff:
        # All five specialists ask the model for the same {"reply", "handoff"} contract and
        # parse the flag, so a model-judged "I can't resolve this" (the multilingual path the
        # English phrase list cannot match) escalates from whichever one answered -- previously
        # only customer_support requested the flag, so the other four told the customer they
        # were connecting the team while nothing paused. The reply itself is still sent below.
        await c.conversations.pause_until(conversation_id, now + HANDOFF_PAUSE_WINDOW)
        await c.conversations.mark_handoff_attempted(conversation_id, now)

    assistant_message_id = await persist_turn(
        c.conversations, conversation_id, event.text, reply_text
    )

    if controls.send_mode == "shadow":
        return
    if controls.send_mode == "allowlist":
        if phone is None or phone not in controls.allowlist_phones:
            return

    result = await send_text(c.http, wa_cfg, event.wa_id, reply_text)
    if not result.ok:
        # SendResult.error is already secret-redacted by whatsapp_sender._safe_error, so it is
        # safe to log directly.
        logger.warning(
            "whatsapp send failed: status=%s error=%s", result.status_code, result.error
        )
    elif result.wamid:
        # Only after a genuine, successful send: attach WhatsApp's wamid to the assistant row so
        # a later delivery/read status callback can be routed back to this message. A transient
        # failure here must NOT propagate: the customer's reply already went out, and an
        # unhandled exception would abort the handoff owner-alert below (the customer would be
        # told "connecting you to our team" while nobody is ever paged). Delivery-status tracking
        # is best-effort telemetry -- swallow and warn, never block the alert.
        try:
            await c.conversations.set_message_wamid(assistant_message_id, result.wamid)
        except Exception:
            logger.exception("failed to record wamid for delivery-status tracking")

    if handoff:
        # Deliberately after the customer's reply and inside the same send_mode gating above:
        # off/shadow suppress all outbound traffic, and in allowlist mode a conversation that
        # was never answered must not page the owner either.
        await _alert_owner(c, wa_cfg, controls.owner_alert_number, phone or event.wa_id)


async def _alert_owner(
    c: Container, wa_cfg: WhatsAppConfig, owner_number: str, customer_phone: str
) -> None:
    """Tell the store owner a customer is waiting for a human, if an alert number is configured.

    ``HANDOFF_MESSAGE`` promises the customer that the team will continue in this same chat, and
    the AI then stays silent for ``HANDOFF_PAUSE_WINDOW`` -- without this notification nobody
    ever learns the customer is waiting, so the promise is false and the chat simply goes dead.
    An unset ``owner_alert_number`` degrades silently (same posture as an empty
    ``allowlist_phones``), and a failed alert only warns: the customer's reply already went out
    and must not be re-sent by a retry of the whole turn.
    """
    if not owner_number:
        return
    alert = OWNER_ALERT_TEMPLATE.format(
        customer_phone=customer_phone,
        hours=int(HANDOFF_PAUSE_WINDOW.total_seconds() // 3600),
    )
    try:
        result = await send_text(c.http, wa_cfg, owner_number, alert)
    except WhatsAppSendError:
        logger.warning("owner handoff alert failed to send (transport error)")
        return
    if not result.ok:
        logger.warning(
            "owner handoff alert failed: status=%s error=%s", result.status_code, result.error
        )


async def _recover_order_by_name(
    shopify: OrderSource, wa_id: str, text: str
) -> tuple[list[AuthorizedOrder], str | None]:
    """Recover an order the sender OWNS from an order-name token in their own message, and flag
    a number-shaped token that doesn't match the store's order-ID format.

    Runs on every order_tracking turn, not only when the phone lookup found nothing: a customer
    can own more than one order, or one placed under different contact info, so a message
    mentioning a different order number should still be checked even when the phone path already
    found something. ``resolve_by_order_name`` re-fetches the order live and enforces ownership,
    returning None both for "no such order" AND for "belongs to a different number" -- so
    nothing is revealed for an order the sender does not own, and a reply can never be used to
    enumerate order numbers.

    Returns ``(orders, format_hint)``: ``format_hint`` is set (and ``orders`` is empty) when the
    extracted candidate's digit count doesn't match ``ORDER_NUMBER_DIGIT_LENGTH`` -- Shopify is
    never queried for a token already known to be the wrong shape; the caller threads the hint
    to the agent so it can ask the customer to double-check their order ID instead of silently
    finding nothing, which reads to the customer as "that order doesn't exist."

    ``format_hint`` is ALSO set (again with ``orders`` empty) when a shape-valid candidate does
    not resolve to an order the sender owns -- so this case is distinct from "no order number
    was mentioned at all" (both had previously returned ``([], None)``) and the caller is not
    left guessing which happened. The hint stays non-enumerable: it never implies whether the
    order belongs to a different number or does not exist.
    """
    candidate = _extract_order_number_candidate(text)
    if candidate is None:
        return [], None
    digits = re.sub(r"[^0-9]", "", candidate)
    if len(digits) != ORDER_NUMBER_DIGIT_LENGTH:
        if len(digits) == ORDER_NUMBER_DIGIT_LENGTH + 1:
            # A same-shaped-but-one-digit-longer candidate is the leading signal that the
            # store's order numbers have grown past ORDER_NUMBER_DIGIT_LENGTH digits (Shopify
            # order numbers are sequential) -- surfaced here so that day is observable in logs
            # instead of silently telling every genuine customer their order ID is malformed.
            logger.warning(
                "order-number candidate is %d digits, one more than ORDER_NUMBER_DIGIT_LENGTH "
                "(%d) -- the store's order numbers may have grown past the configured length",
                len(digits), ORDER_NUMBER_DIGIT_LENGTH,
            )
        return [], (
            f"The customer mentioned a number that doesn't match our order ID format (ours "
            f"are exactly {ORDER_NUMBER_DIGIT_LENGTH} digits, e.g. "
            f"tavas{'9' * ORDER_NUMBER_DIGIT_LENGTH}) -- ask them to double-check and resend "
            f"their order ID."
        )
    order = await resolve_by_order_name(shopify, wa_id, candidate)
    if order is not None:
        return [order], None
    return [], (
        f"The customer gave the order number '{candidate}', but it is not linked to this "
        f"WhatsApp number -- it may belong to a different phone, or may not exist at all; "
        f"you cannot tell which, and must not imply either. Tell them, in your own words: "
        f"you could not find that order linked to this WhatsApp number, and ask if they "
        f"used a different phone number when placing the order -- if so, invite them to "
        f"message from that number, or share the order email so you can look into it "
        f"another way. Do not ask them to resend their order number (they already gave a "
        f"valid one). Guiding them to the different-phone or order-email path is itself how "
        f"you help here: it counts as answering the customer, and is NOT the reply "
        f"contract's case of 'genuinely cannot answer or resolve their request with what "
        f"you know' (you have told them exactly what to say and do next) -- so keep "
        f"'handoff' false for this reason alone; only set 'handoff' to true if the customer "
        f"explicitly asks to speak with a person."
    )


async def _agent_reply(
    c: Container,
    event: InboundText,
    history: list[Message],
    phone: str | None,
    is_vip: bool,
    llm: tuple[LLMProvider, str, str, dict[str, object] | None],
    controls: AdminControls,
) -> tuple[str, bool]:
    """Classify, dispatch to the specialist, and return its (reply text, handoff) pair."""
    provider, model, api_key, extra_params = llm
    intent = await classify_intent(
        provider, model, api_key, event.text, history=history, extra_params=extra_params
    )
    # Classify FIRST, then resolve: resolve_by_phone re-fetches every mapped order live from
    # Shopify, and order_tracking/exchange are the only agents that read context.orders. Doing
    # it unconditionally put that latency on every "hi".
    orders: list[AuthorizedOrder] = []
    order_number_format_hint: str | None = None
    exchange_requests: list[ExchangeRequest] = []
    if intent in ("order_tracking", "exchange"):
        # order_tracking Q&A reads from our database first (near-real-time via the
        # orders/create, orders/updated, customers/update webhooks), falling back to live
        # Shopify on a miss or database error. This does NOT apply to the Confirm/Cancel
        # mutation path (order_actions.py's resolve_by_gid) -- that keeps re-fetching from
        # Shopify directly, per Critical Rule 3.
        #
        # exchange CANNOT use the mirror-first path: check_exchange_eligibility depends
        # entirely on Fulfillment.delivered_at being accurate, and that field is populated
        # ONLY by a genuine live Shopify GraphQL read (see its docstring in shopify/models.py)
        # -- the webhook/mirror-ingestion path never sets it. A mirror-served order would
        # almost always show delivered_at=None, wrongly telling an eligible customer their
        # order "hasn't been delivered yet". So exchange always resolves via c.shopify
        # directly, accepting the extra live-Shopify latency on this rarer intent in exchange
        # for eligibility correctness.
        order_source: OrderSource = (
            c.shopify if intent == "exchange" else MirrorOrderSource(c.ingest, c.shopify)
        )
        orders = await resolve_by_phone(order_source, c.ingest, event.wa_id)
        # Always attempted, not only when the phone path found nothing: a customer can own
        # more than one order, or ask about one placed under different contact info.
        # Ownership re-checked, live-refetched, non-enumerable inside resolve_by_order_name.
        extra_orders, order_number_format_hint = await _recover_order_by_name(
            order_source, event.wa_id, event.text
        )
        for extra in extra_orders:
            if not any(o.order.name == extra.order.name for o in orders):
                orders.append(extra)
        if intent == "exchange":
            # A cheap DB-only read of our own store -- no mirror/live distinction applies here,
            # unlike order resolution above. Guarded exactly like the admin thread-detail lookup
            # (admin/router.py get_conversation_thread): a transient exchange-store failure (DB
            # blip, schema drift, connection issue) must never crash the live customer's turn.
            # The store sits behind the ExchangeStore Protocol, so the concrete failure (asyncpg
            # PostgresError, OSError, ...) is caught as Exception here. On failure we degrade to
            # an empty list -- identical to "no existing exchange requests found", which is how
            # exchange_requests is consumed downstream (context.exchange_requests) -- and let the
            # conversation flow continue rather than raising.
            try:
                exchange_requests = await c.exchanges.list_for_phone(phone or event.wa_id)
            except Exception as exc:
                logger.warning(
                    "failed to load exchange requests for message %s: type=%s",
                    event.message_id,
                    type(exc).__name__,
                )
    loader = KnowledgeLoader(c.config_repo, SEEDS_DIR)
    knowledge = await loader.assemble_all()
    context = AgentContext(
        wa_id=event.wa_id,
        phone_e164=phone or event.wa_id,
        user_text=event.text,
        history=history,
        orders=orders,
        is_vip=is_vip,
        knowledge=knowledge,
        provider=provider,
        model=model,
        api_key=api_key,
        extra_params=extra_params,
        # The admin's disclosure control, carried per-turn exactly like is_vip: order_tracking
        # renders only these fields into its prompt, so an unticked field never reaches the model.
        reveal_fields=tuple(controls.reveal_fields),
        language=controls.default_language,
        order_number_format_hint=order_number_format_hint,
        exchange_requests=exchange_requests,
    )
    agent_reply = await _run_agent(context, intent, c)
    return strip_markdown(agent_reply.text), agent_reply.handoff
