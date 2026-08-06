import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.admin.controls import load_controls
from app.agents import customer_support, order_tracking, policy, product_search, recommendations
from app.agents.base import AgentContext, AgentReply
from app.agents.router import Intent, classify_intent
from app.channels.copy import copy_for
from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import InboundText, extract_events
from app.channels.whatsapp_sender import send_text
from app.channels.whatsapp_signature import verify_meta_hmac
from app.config.crypto import VaultError
from app.core.memory import load_history, persist_turn
from app.core.order_resolver import resolve_by_phone
from app.core.phone import normalize_phone
from app.core.sanitize import strip_markdown
from app.deps import Container, active_llm, get_container
from app.knowledge.loader import SEEDS_DIR, KnowledgeLoader

router = APIRouter()
logger = logging.getLogger("app.channels.whatsapp")

MAX_WEBHOOK_BODY_BYTES = 1_048_576


def _ascii_compare(expected: str, provided: str) -> bool:
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided_bytes)


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


async def _handle_text_event(c: Container, event: InboundText) -> None:
    """Run one conversation turn for a fresh inbound text message and send the reply.

    Failures anywhere in this pipeline (Shopify, the LLM provider, sending the reply) are
    swallowed here -- the webhook must still ack 200 for a message it already deduped, and a
    failed reply is a degraded conversation, not a failed webhook delivery.
    """
    try:
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

        await persist_turn(c.conversations, conversation_id, event.text, reply_text)

        if controls.send_mode == "shadow":
            return
        if controls.send_mode == "allowlist":
            if phone is None or phone not in controls.allowlist_phones:
                return

        await send_text(c.http, wa_cfg, event.wa_id, reply_text)
    except Exception:
        logger.exception("conversation turn failed for a fresh inbound text message")


@router.get("/webhook/whatsapp")
async def verify_webhook(request: Request) -> Response:
    # Fail closed if a stored secret can't be decrypted (rotated/corrupt master key).
    try:
        cfg = await load_whatsapp_config(get_container().config)
    except VaultError:
        return PlainTextResponse("forbidden", status_code=403)
    if cfg is None:
        return PlainTextResponse("forbidden", status_code=403)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and challenge is not None and _ascii_compare(cfg.verify_token, token):
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhook/whatsapp")
async def receive_webhook(request: Request) -> Response:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > MAX_WEBHOOK_BODY_BYTES
        except ValueError:
            too_big = False
        if too_big:
            return PlainTextResponse("payload too large", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BODY_BYTES:
        return PlainTextResponse("payload too large", status_code=413)

    c = get_container()
    # Fail closed if a stored secret can't be decrypted (rotated/corrupt master key).
    try:
        cfg = await load_whatsapp_config(c.config)
    except VaultError:
        return PlainTextResponse("forbidden", status_code=403)
    if cfg is None or not verify_meta_hmac(
        raw, request.headers.get("X-Hub-Signature-256"), cfg.app_secret
    ):
        return PlainTextResponse("forbidden", status_code=403)

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return JSONResponse({"ok": True, "ignored": True})
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True, "ignored": True})

    # Tenant guard fails CLOSED: only messages from a change whose metadata
    # phone_number_id is present, a str, and equal to ours are extracted. Meta may
    # batch several messages into one delivery, so every message is processed and
    # deduped independently -- dropping any (and still 200-acking) is permanent
    # data loss, since Meta will not retry an acked delivery.
    events = extract_events(payload, expected_phone_number_id=cfg.phone_number_id)
    if not events:
        return JSONResponse({"ok": True, "ignored": True})

    results: list[dict[str, Any]] = []
    processed = 0
    duplicate = 0
    for event in events:
        is_new = await c.messages.record_if_new(event.message_id)
        if is_new:
            processed += 1
            if isinstance(event, InboundText):
                await _handle_text_event(c, event)
        else:
            duplicate += 1
        results.append(
            {
                "message_id": event.message_id,
                "duplicate": not is_new,
                "event_type": type(event).__name__,
            }
        )

    # Deterministic button-tap dispatch (order:confirm/cancel -> tagsAdd/orderCancel) is
    # Phase 5. InboundText already runs the full router + agent pipeline above.
    return JSONResponse(
        {"ok": True, "processed": processed, "duplicate": duplicate, "results": results}
    )
