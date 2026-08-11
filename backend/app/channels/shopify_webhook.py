import json
import math
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.shopify_orders import (
    choose_language,
    customer_from_webhook_payload,
    is_eligible_for_push,
    order_from_webhook_payload,
    parse_order_created,
)
from app.channels.shopify_signature import verify_shopify_hmac
from app.config.crypto import VaultError
from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft

router = APIRouter()

TEMPLATE_NAME = "order_confirmation_cod"
MAX_WEBHOOK_BODY_BYTES = 1_048_576
MAX_FIELD_LEN = 256


DEFAULT_STALENESS_HOURS = 6.0
# Mirror AdminControls.push_staleness_hours' Field(ge=1, le=168) — the panel can only ever store
# a value in this range, so anything outside it (or non-finite, or non-ASCII-digit) is corrupt.
_MIN_STALENESS_HOURS = 1.0
_MAX_STALENESS_HOURS = 168.0


def _clip(v: str | None) -> str | None:
    return v if v is None else v[:MAX_FIELD_LEN]


def _staleness_hours(raw: str | None) -> float:
    """Parse the stored push_staleness_hours; a corrupt/typed value degrades to the default.

    On the signed webhook hot path an unguarded ``float()`` on a bad config value is a 500, which
    burns Shopify's 19-failure retry budget before it deletes the subscription. Beyond that, a
    plain ``float()`` guard is not enough:
    - ``float("٣٠") == 30.0`` (Unicode digits) — accept a corrupt value; require ASCII digits.
    - ``float("nan"/"inf"/"Infinity"/"1e400")`` never raises, and a nan/inf makes the eligibility
      check ``age > staleness*3600`` always False — that fully DISABLES the staleness guard, so
      every historical order looks fresh and push-eligible again (mass unwanted re-sends). Require
      ``math.isfinite`` and clamp to the field's own valid range; anything else -> safe default.
    """
    if not raw or not (raw.isascii() and raw.isdigit()):
        return DEFAULT_STALENESS_HOURS
    value = float(raw)
    if not math.isfinite(value) or not (_MIN_STALENESS_HOURS <= value <= _MAX_STALENESS_HOURS):
        return DEFAULT_STALENESS_HOURS
    return value


@router.post("/webhooks/shopify")
async def shopify_webhook(request: Request) -> Response:
    # Reject oversized bodies pre-auth: declared Content-Length first, then actual bytes.
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
    # Fail closed if the secret can't be read/decrypted (rotated/corrupt master key).
    try:
        secret = await c.config.get_secret("shopify:client_secret")
    except VaultError:
        return PlainTextResponse("forbidden", status_code=403)
    if not secret or not verify_shopify_hmac(
        raw, request.headers.get("X-Shopify-Hmac-Sha256"), secret
    ):
        return PlainTextResponse("forbidden", status_code=403)

    # The client secret is per-app, not per-store: reject deliveries from a foreign shop.
    shop_domain = request.headers.get("X-Shopify-Shop-Domain")
    if shop_domain is not None and shop_domain != c.settings.shop_domain:
        return PlainTextResponse("forbidden", status_code=403)

    topic = request.headers.get("X-Shopify-Topic", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    handled_topics = {"orders/create", "orders/updated", "customers/update"}
    if topic not in handled_topics or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return JSONResponse({"ok": True, "ignored": True})
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True, "ignored": True})

    # orders/updated and customers/update deliberately do NOT go through the
    # processed_webhooks dedupe table the way orders/create does below: upsert_order_mirror /
    # upsert_customer are ON CONFLICT DO UPDATE, so replaying the same webhook twice just
    # re-writes identical data -- a no-op in effect, unlike orders/create's outbound-message
    # queuing, which has a real side effect that duplication would actually break.
    if topic == "customers/update":
        customer = customer_from_webhook_payload(payload)
        if customer is None:
            return JSONResponse({"ok": True, "ignored": True})
        await c.ingest.upsert_customer(customer)
        return JSONResponse({"ok": True, "ignored": False})

    if topic == "orders/updated":
        order = order_from_webhook_payload(payload)
        if order is None:
            return JSONResponse({"ok": True, "ignored": True})
        await c.ingest.upsert_order_mirror(order)
        return JSONResponse({"ok": True, "ignored": False})

    incoming = parse_order_created(payload)
    if incoming is None:
        return JSONResponse({"ok": True, "ignored": True})
    mirror_order = order_from_webhook_payload(payload)
    if mirror_order is not None:
        await c.ingest.upsert_order_mirror(mirror_order)

    language = choose_language(incoming.locale)
    order_name = _clip(incoming.name) or ""
    customer_name = _clip(incoming.customer_name)
    email = _clip(incoming.email)
    amount = _clip(str(payload.get("total_price") or "")) or ""
    mapping = MappingUpsert(
        order_gid=incoming.gid,
        order_name=order_name,
        order_number_int=incoming.order_number,
        phone_e164=incoming.phone_e164,
        customer_name=customer_name,
        email=email,
        language=language,
        financial_status_at_create=incoming.financial_status,
        is_cod=incoming.is_cod(),
    )

    outbound: OutboundDraft | None = None
    push_policy = await c.config.get_plain("push_policy") or "cod_only"
    staleness_hours = _staleness_hours(await c.config.get_plain("push_staleness_hours"))
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
                    "customer_name": customer_name or "",
                    "order_name": order_name,
                    "amount": amount,
                }
            ),
        )

    result = await c.ingest.ingest_order_created(webhook_id, topic, mapping, outbound)
    return JSONResponse({"ok": True, "duplicate": result.duplicate, "queued": result.queued})
