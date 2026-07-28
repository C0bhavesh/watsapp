import hmac
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import extract_event
from app.channels.whatsapp_signature import verify_meta_hmac
from app.deps import get_container

router = APIRouter()

MAX_WEBHOOK_BODY_BYTES = 1_048_576


def _ascii_compare(expected: str, provided: str) -> bool:
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided_bytes)


def _incoming_phone_number_id(payload: dict[str, Any]) -> str | None:
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        phone_id = (value.get("metadata") or {}).get("phone_number_id")
    except (KeyError, IndexError, TypeError):
        return None
    return phone_id if isinstance(phone_id, str) else None


@router.get("/webhook/whatsapp")
async def verify_webhook(request: Request) -> Response:
    cfg = await load_whatsapp_config(get_container().config)
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
    cfg = await load_whatsapp_config(c.config)
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

    # Defensive check mirroring the Shopify shop-domain guard: ignore deliveries for a
    # phone number we are not configured to serve.
    incoming_phone_id = _incoming_phone_number_id(payload)
    if incoming_phone_id is not None and incoming_phone_id != cfg.phone_number_id:
        return JSONResponse({"ok": True, "ignored": True})

    event = extract_event(payload)
    if event is None:
        return JSONResponse({"ok": True, "ignored": True})

    is_new = await c.messages.record_if_new(event.message_id)
    if not is_new:
        return JSONResponse({"ok": True, "duplicate": True})

    # Routing a fresh event to the deterministic button dispatcher (Phase 5) and the
    # conversation engine / order_resolver (Phase 4) attaches here. Phase 3 is the pipe only.
    return JSONResponse(
        {"ok": True, "duplicate": False, "event_type": type(event).__name__}
    )
