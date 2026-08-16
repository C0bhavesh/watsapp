import asyncio
import json
import logging
import math
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.copy import EMPTY_PARAM_PLACEHOLDER
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
from app.jobs.outbox_drain import send_inline_outbound
from app.shopify.models import Customer, Fulfillment, Order
from app.shopify.subscriptions import REQUIRED_TOPICS
from app.store.base import MappingUpsert, OutboundDraft

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATE_NAME_COD = "cod_confirmation"
TEMPLATE_NAME_PREPAID = "prepaid_order"
# Both are Meta-approved in `en` ONLY on the new WABA, so the template send is pinned to en
# regardless of the customer's detected locale — sending hi/gu (no such approved locale) would
# make Meta reject the order, reintroducing the exact "every order fails to send" bug this change
# fixes. The customer's own language (mapping.language) still drives the free-form conversation
# replies; only this ONE template send is en. (Owner/client note: to localise the confirmation,
# hi/gu versions of both templates must first be approved in Meta.)
TEMPLATE_LANGUAGE = "en"
# Bound the live product-image fetch on the orders/create ack path. Best-effort and purely
# cosmetic: on timeout/failure the confirmation still sends, just without a header (Q19a). Kept
# deliberately SMALL (1.0s) because it runs BEFORE the ingest DB write AND before the inline send's
# own 3.0s bound (_INLINE_SEND_TIMEOUT_SECONDS in jobs.outbox_drain) — so the two network waits are
# sequential on the same <5s Shopify-ack handler. 1.0 + 3.0 = 4.0s worst-case network leaves real
# margin for the DB reads/writes under the 5s budget (was 2.0s, which left zero margin). See
# _resolve_product_image and the resolution call site.
_IMAGE_FETCH_TIMEOUT_SECONDS = 1.0
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


async def _resolve_product_image(c: Container, product_gid: str | None) -> str | None:
    """Resolve the first product's photo URL for the cod_confirmation header — best-effort.

    Q19a: the header is the live product image, but a Shopify hiccup must NEVER block the
    confirmation. ``get_product_image_url`` already swallows any ``ShopifyError`` (missing product,
    ACCESS_DENIED, outage) into None; this wraps it in a short total-elapsed bound
    (``asyncio.wait_for``) because it runs on the orders/create webhook's tight <5s ack path, and
    catches the timeout (and anything else) so the ack can never be delayed past budget or turned
    into a 5xx. Any failure -> None -> the template sends with no header.
    """
    if not product_gid:
        return None
    try:
        return await asyncio.wait_for(
            c.shopify.get_product_image_url(product_gid),
            timeout=_IMAGE_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "product image fetch failed: type=%s (confirmation header skipped)",
            type(exc).__name__,
        )
        return None


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


async def _hmac_verifies(c: Container, key: str, raw: bytes, header: str | None) -> bool:
    """True iff the secret stored under ``key`` exists and validates the body's Shopify HMAC.

    Fails CLOSED on every failure mode — an unset key (``get_secret`` returns None), an
    unreadable/corrupt secret (``VaultError`` from a rotated/corrupt master key), or a genuine
    signature mismatch — so a caller can OR several secrets together and a broken/absent one
    never crashes the request nor short-circuits the others.
    """
    try:
        secret = await c.config.get_secret(key)
    except VaultError:
        return False
    if not secret:
        return False
    return verify_shopify_hmac(raw, header, secret)


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
    # Transition period: accept EITHER signing secret. App-created subscriptions
    # (webhookSubscriptionCreate) are signed with our app's `shopify:client_secret`; webhooks the
    # owner configures in Admin -> Settings -> Notifications are signed with the per-store
    # `shopify:webhook_signing_secret`. Both must verify during the cutover, without an atomic
    # switch. Try the live app secret first (the current real-order path); fall through to the
    # signing secret only if configured. Each check fails CLOSED independently — an unset key
    # (get_secret None) or an unreadable/corrupt one (VaultError) just doesn't verify, and never
    # crashes the request nor blocks the other secret. If NEITHER verifies -> 403, as today.
    header = request.headers.get("X-Shopify-Hmac-Sha256")
    if not (
        await _hmac_verifies(c, "shopify:client_secret", raw, header)
        or await _hmac_verifies(c, "shopify:webhook_signing_secret", raw, header)
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

    # Owner-requested debug visibility (2026-08-15) — logs the raw payload incl. customer PII
    # (name/phone/email/address). This is an INTENTIONAL, owner-approved exception to the project's
    # no-PII-logging rule (do NOT "fix" it by narrowing/redacting later — see error_learnings.md).
    # It sits AFTER HMAC verify + shop-domain check + json.loads, before topic branching, so it
    # fires uniformly for every handled topic and never logs an unverified/forged request's body.
    logger.info(
        "SHOPIFY WEBHOOK RECEIVED: topic=%s shop=%s webhook_id=%s",
        topic,
        shop_domain,
        webhook_id,
    )
    # Compact (no indent=2): pretty-printing amplifies the rendered size ~quadratically in nesting
    # depth (a validly-signed 1MB body can render to ~5GB), evaluated eagerly on every delivery.
    # The complete raw payload is still logged -- it is just not pretty-printed.
    logger.info("ORDER JSON: %s", json.dumps(payload))

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
        # Resolve the product photo HERE (ingest time), not at send time, and cache the URL in
        # payload_json. Rationale: the confirmation is now sent INLINE in this handler (primary,
        # ADR-001 amendment) with a tight timeout, and the SAME payload is replayed by the backstop
        # drain and the 1-hour reminder — resolving once at ingest means (a) the tight-budget send
        # path does ZERO extra Shopify work, (b) the reminder (which reuses payload_json verbatim)
        # needs no change and no re-fetch, and (c) the sender stays a pure formatter with no
        # Shopify coupling. The one added Shopify call is bounded + best-effort (see
        # _resolve_product_image); its worst-case latency shares the handler's pre-existing ack
        # headroom, and a rare overrun is a SAFE idempotent Shopify retry (webhook_id dedupe -> no
        # duplicate row; the inline send's claim-by-id -> no double send).
        image_url = await _resolve_product_image(c, incoming.product_gid)
        template_name = TEMPLATE_NAME_COD if incoming.is_cod() else TEMPLATE_NAME_PREPAID
        template_params: dict[str, object] = {
            "template": template_name,
            "language": TEMPLATE_LANGUAGE,
            "body_params": {
                "customer_name": customer_name or EMPTY_PARAM_PLACEHOLDER,
                "order_id": order_name or EMPTY_PARAM_PLACEHOLDER,
                "product_name": incoming.product_name or EMPTY_PARAM_PLACEHOLDER,
                "product_color": incoming.product_color or EMPTY_PARAM_PLACEHOLDER,
                "product_size": incoming.product_size or EMPTY_PARAM_PLACEHOLDER,
                "product_amount": amount or EMPTY_PARAM_PLACEHOLDER,
            },
        }
        # Only carry image_url when resolved: its absence is the drain/inline sender's signal to
        # send with no header (a stored non-https value would be dropped there anyway).
        if image_url is not None:
            template_params["image_url"] = image_url
        # prepaid_order has no BUTTONS component approved on the WABA -- only cod_confirmation gets
        # the quick-reply buttons, baked in HERE (ingest time, when incoming.gid is known) rather
        # than derived at send time from a template-name string check.
        if template_name == TEMPLATE_NAME_COD:
            template_params["buttons"] = [
                f"order:confirm:{incoming.gid}", f"order:cancel:{incoming.gid}",
            ]
        outbound = OutboundDraft(
            dedupe_key=f"order_created:{incoming.gid}",
            kind="order_confirmation",
            phone_e164=incoming.phone_e164,
            payload_json=json.dumps(template_params),
        )

    # The mapping + outbox write goes FIRST: that row IS the customer's confirmation message,
    # while the mirror has no live reader yet. Mirroring afterwards (and non-fatally, inside
    # _mirror_order) means a mirror hiccup costs a stale row, never the customer's message.
    result = await c.ingest.ingest_order_created(webhook_id, topic, mapping, outbound)

    mirror_order = order_from_webhook_payload(payload)
    if mirror_order is not None:
        await _mirror_order(c, mirror_order)

    # Send the confirmation INLINE — a genuine `await` before the ack (ADR-001 amendment,
    # owner-directed 2026-08-15). The prior queue+external-cron design is kept as a backstop
    # (`/internal/jobs/outbox_drain` still exists), but the external scheduler stopped working, so
    # the PRIMARY send now runs here. This is NOT a BackgroundTask (which does not reliably run
    # after the response on Vercel's Python runtime); the ack is legitimately delayed by the send,
    # which is why send_inline_outbound bounds the WhatsApp call with a SHORT timeout so the whole
    # handler stays under Shopify's <5s ack budget. It claims ONLY the row just queued (by id, an
    # atomic queued->processing flip), so the backstop drain can never double-send it; it NEVER
    # raises, so a send failure/timeout can never turn the 200 ack into a 500.
    await send_inline_outbound(c, result.outbound_id)
    return JSONResponse({"ok": True, "duplicate": result.duplicate, "queued": result.queued})
