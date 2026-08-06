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
from datetime import UTC, datetime

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

logger = logging.getLogger("app.core.conversation")

# Comfortably under vercel.json's function `maxDuration` (60s) so a near-deadline turn is
# cancelled and logged (see `except TimeoutError` in `run_turn`) well before Vercel itself would
# kill the function and return an uncaught 504. That distinction matters: `record_if_new` has
# already marked the inbound message as seen by the time this runs, so an uncaught 504 makes
# Meta's retry of the same message get silently dropped as a duplicate -- the customer never
# gets a reply, and nothing is logged. A caught TimeoutError here at least leaves a WARNING.
TURN_TIMEOUT_SECONDS = 50.0


async def _run_agent(
    context: AgentContext, intent: Intent, c: Container, conversation_id: int, now: datetime
) -> AgentReply:
    if intent == "order_tracking":
        return await order_tracking.run(context)
    if intent == "product_search":
        return await product_search.run(context, c.shopify)
    if intent == "policy":
        return await policy.run(context)
    if intent == "recommendations":
        return await recommendations.run(context, c.shopify)
    return await customer_support.run(context, c.conversations, conversation_id, now)


async def run_turn(c: Container, event: InboundText) -> None:
    """Run one conversation turn for a fresh inbound text message and send the reply.

    Failures anywhere in this pipeline (Shopify, the LLM provider, sending the reply) are
    swallowed here -- the webhook must still ack 200 for a message it already deduped, and a
    failed reply is a degraded conversation, not a failed webhook delivery. A turn that runs
    past ``TURN_TIMEOUT_SECONDS`` is cancelled and logged as a WARNING rather than left to an
    uncaught platform-level 504 (see the module docstring for why that distinction matters).
    """
    try:
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            await _run_turn(c, event)
    except TimeoutError:
        logger.warning("conversation turn timed out after %.0fs", TURN_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("conversation turn failed for a fresh inbound text message")


async def _run_turn(c: Container, event: InboundText) -> None:
    controls = await load_controls(c.config)
    if controls.send_mode == "off":
        return
    wa_cfg = await load_whatsapp_config(c.config)
    if wa_cfg is None:
        return

    conversation_id, history = await load_history(c.conversations, event.wa_id)
    now = datetime.now(UTC)
    paused_until = await c.conversations.get_paused_until(conversation_id)
    if paused_until is not None and now < paused_until:
        await c.conversations.append_message(conversation_id, "user", event.text)
        return

    phone = normalize_phone(event.wa_id)
    orders = await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
    order_count = await c.ingest.count_orders_by_phone(phone) if phone else 0
    is_vip = order_count >= controls.vip_order_count_threshold

    llm = await active_llm(c.settings, c.config)
    if llm is None:
        reply_text = copy_for("error_fallback", "en")
    else:
        provider, model, api_key, extra_params = llm
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
        )
        intent = await classify_intent(
            provider, model, api_key, event.text, extra_params=extra_params
        )
        agent_reply = await _run_agent(context, intent, c, conversation_id, now)
        reply_text = strip_markdown(agent_reply.text)

    if not reply_text.strip():
        # A completion that's e.g. only a code fence, or an agent's own degraded reply, can
        # strip down to nothing -- never persist/send a blank WhatsApp message.
        reply_text = copy_for("error_fallback", "en")

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
