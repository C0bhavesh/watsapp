import json
import logging
import math
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.shopify_orders import (
    choose_language,
    clip,
    customer_from_webhook_payload,
    fulfillment_from_webhook_payload,
    is_eligible_for_push,
    order_from_webhook_payload,
    parse_order_created,
)
from app.channels.shopify_signature import verify_shopify_hmac
from app.config.crypto import VaultError
from app.deps import Container, get_container
from app.shopify.models import Customer, Fulfillment, Order
from app.shopify.subscriptions import REQUIRED_TOPICS
from app.store.base import MappingUpsert, OutboundDraft

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATE_NAME = "order_confirmation_cod"
MAX_WEBHOOK_BODY_BYTES = 1_048_576

def _topic_header_name(topic: str) -> str:
    """GraphQL subscription enum -> the X-Shopify-Topic header form.

    Only the FIRST underscore becomes a slash: it separates the resource from the event, and a
    multi-word event keeps its own (``ORDERS_PARTIALLY_FULFILLED`` ->
    ``orders/partially_fulfilled``).
    """
    return topic.lower().replace("_", "/", 1)


# Derived from the subscribed topics so the two can never drift: a topic we subscribe to but
# do not handle is a delivery silently dropped, and the reverse is code that can never run.
HANDLED_TOPICS = frozenset(_topic_header_name(t) for t in REQUIRED_TOPICS)


DEFAULT_STALENESS_HOURS = 6.0
# Mirror AdminControls.push_staleness_hours' Field(ge=1, le=168) — the panel can only ever store
# a value in this range, so anything outside it (or non-finite, or non-ASCII-digit) is corrupt.
_MIN_STALENESS_HOURS = 1.0
_MAX_STALENESS_HOURS = 168.0


async def _mirror_order(c: Container, order: Order) -> bool:
    """Sync one order into the mirror. Never fatal to the ack.

    The mirror has no live reader yet, while a 500 on a signed delivery makes Shopify retry
    (against a mirror that is still broken) and burns its 19-failure budget before it deletes
    the subscription. A failed sync degrades to "stale mirror, logged".
    """
    try:
        await c.ingest.upsert_order_mirror(order)
    except Exception as exc:
        # Log only the exception TYPE, never the rendered exception/traceback (2026-08-13
        # error_learning): now that upsert_order_mirror writes fulfillment rows, asyncpg's error
        # text can echo a tracking number/URL, which must not reach the logs.
        logger.error(
            "order mirror sync failed: gid=%s type=%s", order.gid, type(exc).__name__
        )
        return False
    return True


async def _mirror_customer(c: Container, customer: Customer) -> bool:
    """Refresh a KNOWN customer's mirror row (see ``_mirror_order`` on why failures are soft).

    Owner decision 2026-08-11: customers/update only refreshes customers the bot already knows
    (a row exists because we mirrored one of their orders). Shopify sends this topic for every
    customer in the store, and importing people who have never ordered or messaged the bot
    would hold personal data we have no reason to keep.
    """
    try:
        if not await c.ingest.customer_exists(customer.gid):
            return False
        await c.ingest.upsert_customer(customer)
    except Exception:
        logger.exception("customer mirror sync failed: gid=%s", customer.gid)
        return False
    return True


async def _mirror_fulfillment(
    c: Container, order_gid: str, fulfillment: Fulfillment
) -> bool:
    """Sync one fulfillment's tracking into the mirror. Never fatal to the ack (see
    ``_mirror_order`` on why a signed-delivery 500 must be avoided).

    ``upsert_fulfillment`` itself skips a fulfillment whose order is not mirrored (the FK would
    otherwise reject it), so a fulfillment arriving for an unknown order degrades to a logged
    skip, not an error.
    """
    try:
        await c.ingest.upsert_fulfillment(order_gid, fulfillment)
    except Exception as exc:
        # Log only the exception TYPE, never the rendered exception/traceback (2026-08-13
        # error_learning): asyncpg's error text can echo the row's tracking number/URL, which
        # must not reach the logs.
        logger.error(
            "fulfillment mirror sync failed: gid=%s type=%s",
            fulfillment.gid,
            type(exc).__name__,
        )
        return False
    return True


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
    if topic not in HANDLED_TOPICS or not webhook_id:
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
        return JSONResponse({"ok": True, "ignored": not await _mirror_customer(c, customer)})

    if topic == "orders/updated":
        order = order_from_webhook_payload(payload)
        if order is None:
            return JSONResponse({"ok": True, "ignored": True})
        return JSONResponse({"ok": True, "ignored": not await _mirror_order(c, order)})

    if topic in ("fulfillments/create", "fulfillments/update"):
        parsed = fulfillment_from_webhook_payload(payload)
        if parsed is None:
            return JSONResponse({"ok": True, "ignored": True})
        order_gid, fulfillment = parsed
        return JSONResponse(
            {"ok": True, "ignored": not await _mirror_fulfillment(c, order_gid, fulfillment)}
        )

    # Explicit, not an implicit else: HANDLED_TOPICS is DERIVED from REQUIRED_TOPICS, so a
    # future subscription topic passes the gate above automatically. Falling through would run
    # it against the orders/create mapping + outbox logic and could queue a duplicate
    # order-confirmation template to a real customer for an unrelated event.
    if topic != "orders/create":
        return JSONResponse({"ok": True, "ignored": True})

    incoming = parse_order_created(payload)
    if incoming is None:
        return JSONResponse({"ok": True, "ignored": True})

    language = choose_language(incoming.locale)
    order_name = clip(incoming.name) or ""
    customer_name = clip(incoming.customer_name)
    email = clip(incoming.email)
    amount = clip(str(payload.get("total_price") or "")) or ""
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

    # The mapping + outbox write goes FIRST: that row IS the customer's confirmation message,
    # while the mirror has no live reader yet. Mirroring afterwards (and non-fatally, inside
    # _mirror_order) means a mirror hiccup costs a stale row, never the customer's message.
    #
    # The webhook ONLY queues the order and acknowledges Shopify — it never sends the WhatsApp
    # message inline and never via a background task. On Vercel's Python runtime, FastAPI
    # BackgroundTasks do NOT reliably run after the response reaches the client (they execute
    # inside the request cycle and can be killed on instance reclaim — ADR-001, and the
    # 2026-07-28 "no reliable process-after-response" error_learning). Delivery is handled
    # entirely by the 1-minute cron sender (`/internal/jobs/outbox_drain`), which atomically
    # claims queued rows and sends them; `dedupe_key UNIQUE` keeps it exactly-once per order.
    result = await c.ingest.ingest_order_created(webhook_id, topic, mapping, outbound)

    mirror_order = order_from_webhook_payload(payload)
    if mirror_order is not None:
        await _mirror_order(c, mirror_order)
    return JSONResponse({"ok": True, "duplicate": result.duplicate, "queued": result.queued})
