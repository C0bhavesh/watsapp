import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.shopify_orders import (
    choose_language,
    is_eligible_for_push,
    parse_order_created,
)
from app.channels.shopify_signature import verify_shopify_hmac
from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft

router = APIRouter()

TEMPLATE_NAME = "order_confirmation_cod"


@router.post("/webhooks/shopify")
async def shopify_webhook(request: Request) -> Response:
    raw = await request.body()
    c = get_container()
    secret = await c.config.get_secret("shopify:client_secret")
    if not secret or not verify_shopify_hmac(
        raw, request.headers.get("X-Shopify-Hmac-Sha256"), secret
    ):
        return PlainTextResponse("forbidden", status_code=403)

    topic = request.headers.get("X-Shopify-Topic", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    if topic != "orders/create" or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})

    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"ok": True, "ignored": True})
    incoming = parse_order_created(payload) if isinstance(payload, dict) else None
    if incoming is None:
        return JSONResponse({"ok": True, "ignored": True})

    language = choose_language(incoming.locale)
    mapping = MappingUpsert(
        order_gid=incoming.gid,
        order_name=incoming.name,
        order_number_int=incoming.order_number,
        phone_e164=incoming.phone_e164,
        customer_name=incoming.customer_name,
        email=incoming.email,
        language=language,
        financial_status_at_create=payload.get("financial_status"),
        is_cod=incoming.is_cod(),
    )

    outbound: OutboundDraft | None = None
    push_policy = await c.config.get_plain("push_policy") or "cod_only"
    staleness_raw = await c.config.get_plain("push_staleness_hours")
    staleness_hours = float(staleness_raw) if staleness_raw else 6.0
    if incoming.phone_e164 and is_eligible_for_push(
        incoming, datetime.now(UTC), push_policy, staleness_hours
    ):
        outbound = OutboundDraft(
            dedupe_key=f"order_created:{incoming.gid}",
            kind="order_confirmation",
            phone_e164=incoming.phone_e164,
            payload_json=json.dumps(
                {
                    "template": TEMPLATE_NAME,
                    "language": language,
                    "customer_name": incoming.customer_name or "",
                    "order_name": incoming.name,
                    "amount": str(payload.get("total_price") or ""),
                }
            ),
        )

    result = await c.ingest.ingest_order_created(webhook_id, topic, mapping, outbound)
    return JSONResponse({"ok": True, "duplicate": result.duplicate, "queued": result.queued})
