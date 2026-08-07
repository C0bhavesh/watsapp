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
from datetime import UTC, datetime, timedelta

from app.admin.controls import load_controls
from app.agents import customer_support, order_tracking, policy, product_search, recommendations
from app.agents.base import AgentContext, AgentReply
from app.agents.router import Intent, classify_intent
from app.channels.copy import copy_for
from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import InboundText
from app.channels.whatsapp_sender import send_text
from app.core.memory import load_history, persist_turn
from app.core.order_resolver import resolve_by_phone
from app.core.phone import normalize_phone
from app.core.sanitize import strip_markdown
from app.deps import Container, active_llm
from app.knowledge.loader import SEEDS_DIR, KnowledgeLoader
from app.providers.base import LLMProvider, Message

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


async def _run_agent(context: AgentContext, intent: Intent, c: Container) -> AgentReply:
    if intent == "order_tracking":
        return await order_tracking.run(context)
    if intent == "product_search":
        return await product_search.run(context, c.shopify)
    if intent == "policy":
        return await policy.run(context)
    if intent == "recommendations":
        return await recommendations.run(context, c.shopify)
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
                c, event, history, phone, is_vip, llm, controls.default_language
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
        # Honored for EVERY agent, not just customer_support: a model-judged "I can't resolve
        # this" (the multilingual path the English phrase list cannot match) escalates from
        # whichever specialist answered. The reply itself is still sent below.
        await c.conversations.pause_until(conversation_id, now + HANDOFF_PAUSE_WINDOW)
        await c.conversations.mark_handoff_attempted(conversation_id, now)

    await persist_turn(c.conversations, conversation_id, event.text, reply_text)

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


async def _agent_reply(
    c: Container,
    event: InboundText,
    history: list[Message],
    phone: str | None,
    is_vip: bool,
    llm: tuple[LLMProvider, str, str, dict[str, object] | None],
    language: str,
) -> tuple[str, bool]:
    """Classify, dispatch to the specialist, and return its (reply text, handoff) pair."""
    provider, model, api_key, extra_params = llm
    intent = await classify_intent(
        provider, model, api_key, event.text, extra_params=extra_params
    )
    # Classify FIRST, then resolve: resolve_by_phone re-fetches every mapped order live from
    # Shopify, and order_tracking is the only agent that reads context.orders. Doing it
    # unconditionally put that latency on every "hi".
    orders = (
        await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
        if intent == "order_tracking"
        else []
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
        language=language,
    )
    agent_reply = await _run_agent(context, intent, c)
    return strip_markdown(agent_reply.text), agent_reply.handoff
