import hmac
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import extract_events
from app.channels.whatsapp_signature import verify_meta_hmac
from app.config.crypto import VaultError
from app.deps import get_container

router = APIRouter()

MAX_WEBHOOK_BODY_BYTES = 1_048_576


def _ascii_compare(expected: str, provided: str) -> bool:
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided_bytes)


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
        else:
            duplicate += 1
        results.append(
            {
                "message_id": event.message_id,
                "duplicate": not is_new,
                "event_type": type(event).__name__,
            }
        )

    # Routing each fresh event to the deterministic button dispatcher (Phase 5) and
    # the conversation engine / order_resolver (Phase 4) attaches here. Phase 3 is
    # the pipe only.
    return JSONResponse(
        {"ok": True, "processed": processed, "duplicate": duplicate, "results": results}
    )
