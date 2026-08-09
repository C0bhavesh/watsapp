"""Deterministic confirm/cancel button-tap dispatch — the mutation-safety core (Flow B).

The LLM is NEVER in this path. A button tap is handled deterministically: every branch re-fetches
the order live and ownership-checks via ``resolve_by_gid`` (the only constructor of the
``AuthorizedOrder`` mutation gate, ADR-004) BEFORE any ``tagsAdd``/``orderCancel``. A non-owner or
unknown gid gets the same non-enumerable refusal with no order detail. Cancel is two-phase: the
first tap only asks "are you sure?", only the confirm tap calls ``orderCancel``, and a
fulfilled/dispatched order refuses. Nothing here ever raises — the signed webhook must still ack
200 (Rule 5.5); mutation/transport errors degrade to a safe reply.
"""

import json
import logging

from app.admin.controls import AdminControls, load_controls
from app.channels.copy import copy_for
from app.channels.shopify_orders import choose_language
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_inbound import InboundButton, InboundInteractive
from app.channels.whatsapp_sender import WhatsAppSendError, send_buttons, send_text
from app.core.order_resolver import resolve_by_gid
from app.deps import Container
from app.shopify.errors import ShopifyError, ShopifyGraphQLError
from app.shopify.models import AuthorizedOrder, Order

logger = logging.getLogger("app.core.order_actions")

Event = InboundButton | InboundInteractive

# Fulfillment states that still ALLOW a cancel (order not yet dispatched). Anything else is treated
# as dispatched and refuses to cancel — deliberately conservative for the IRREVERSIBLE orderCancel,
# and consistent with the Phase 4 order_tracking agent's own eligibility promise
# (fulfillment_status in {None, "UNFULFILLED"} = eligible). A false-refuse routes the customer to
# support (safe); a false-cancel of an already-shipped order is not.
_CANCELLABLE_FULFILLMENT: frozenset[str | None] = frozenset({None, "UNFULFILLED"})


def _is_dispatched(order: Order) -> bool:
    status = order.fulfillment_status
    normalized = status.upper() if isinstance(status, str) else status
    return normalized not in _CANCELLABLE_FULFILLMENT


def _has_any_tag(order_tags: tuple[str, ...], wanted: list[str]) -> bool:
    present = {t.strip().lower() for t in order_tags}
    return any(w.strip().lower() in present for w in wanted)


def _parse_payload(payload: str) -> tuple[str, str] | None:
    """('confirm'|'cancel'|'cancel_confirm'|'cancel_abort', gid) or None.

    Two-token cancel prefixes are matched BEFORE the bare cancel prefix. The gid itself contains
    ':' so the prefix is stripped whole; the remainder must be a real 'gid://...' or the parse
    fails (a malformed/foreign payload -> generic safe reply, never a mutation).
    """
    for prefix, action in (
        ("order:cancel:confirm:", "cancel_confirm"),
        ("order:cancel:abort:", "cancel_abort"),
        ("order:confirm:", "confirm"),
        ("order:cancel:", "cancel"),
    ):
        if payload.startswith(prefix):
            gid = payload[len(prefix):]
            return (action, gid) if gid.startswith("gid://") else None
    return None


def _payload_of(event: Event) -> str:
    return event.payload if isinstance(event, InboundButton) else event.button_id


async def _safe_send_text(c: Container, cfg: WhatsAppConfig, to: str, body: str) -> None:
    """Send a reply, swallowing a transport failure so dispatch never raises."""
    try:
        await send_text(c.http, cfg, to, body)
    except WhatsAppSendError:
        logger.warning("button dispatch: reply send failed (transport)")


async def _safe_send_buttons(
    c: Container, cfg: WhatsAppConfig, to: str, body_text: str, buttons: list[tuple[str, str]]
) -> None:
    try:
        await send_buttons(c.http, cfg, to, body_text, buttons)
    except WhatsAppSendError:
        logger.warning("button dispatch: buttons send failed (transport)")


async def dispatch_button(c: Container, event: Event) -> None:
    """Deterministically handle a confirm/cancel button tap. Never raises."""
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    if cfg is None:
        logger.warning("button dispatch: whatsapp not configured; tap ignored")
        return
    default_lang = controls.default_language

    parsed = _parse_payload(_payload_of(event))
    if parsed is None:
        await _safe_send_text(c, cfg, event.wa_id, copy_for("error_fallback", default_lang))
        return
    action, gid = parsed

    lang: str = default_lang  # replaced with the order's language once resolved
    try:
        auth = await resolve_by_gid(c.shopify, event.wa_id, gid)
        if auth is None:
            # Unknown or foreign gid -> the same non-enumerable refusal; NO order detail leaked.
            # Abort is a harmless no-op ack even when unresolved.
            key = "cancel_kept" if action == "cancel_abort" else "not_found"
            await _safe_send_text(c, cfg, event.wa_id, copy_for(key, default_lang))
            return
        lang = choose_language(auth.order.customer_locale, default_lang)

        if action == "confirm":
            await _handle_confirm(c, cfg, event, auth, controls, lang, gid)
        elif action == "cancel":
            await _handle_cancel_request(c, cfg, event, auth, lang, gid)
        elif action == "cancel_confirm":
            await _handle_cancel_confirm(c, cfg, event, auth, controls, lang, gid)
        else:  # cancel_abort
            await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_kept", lang))
    except ShopifyError:
        # A mutation/transport error mid-flow: tell the customer to retry; write nothing so a
        # re-tap safely retries. Never raise (the webhook must still ack 200).
        logger.warning("button dispatch: shopify error for action=%s", action)
        await _safe_send_text(c, cfg, event.wa_id, copy_for("error_fallback", lang))
    except Exception:
        logger.exception("button dispatch: unexpected error for action=%s", action)
        await _safe_send_text(c, cfg, event.wa_id, copy_for("error_fallback", lang))


async def _handle_confirm(
    c: Container, cfg: WhatsAppConfig, event: Event, auth: AuthorizedOrder,
    controls: AdminControls, lang: str, gid: str,
) -> None:
    order = auth.order
    if order.is_cancelled():
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_cancelled", lang))
        return
    if _has_any_tag(order.tags, controls.tags.confirmed):
        # Idempotent re-tap: the confirmed tag is already on the live order -> no second mutation.
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_confirmed", lang))
        return
    await c.shopify.add_tags(auth, controls.tags.confirmed)
    await c.ingest.record_order_action(gid, "confirm", event.wa_id, event.message_id, "ok", None)
    await c.ingest.set_mapping_status(gid, "confirmed")
    await _safe_send_text(c, cfg, event.wa_id, copy_for("confirm_success", lang))


async def _handle_cancel_request(
    c: Container, cfg: WhatsAppConfig, event: Event, auth: AuthorizedOrder, lang: str, gid: str,
) -> None:
    order = auth.order
    if order.is_cancelled():
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_cancelled", lang))
        return
    if _is_dispatched(order):
        # Cancel-before-dispatch only: a shipped order cannot be cancelled here (handoff).
        await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_too_late", lang))
        return
    # Two-phase: the first tap ONLY asks. No mutation until the confirm tap.
    await _safe_send_buttons(
        c, cfg, event.wa_id, copy_for("cancel_are_you_sure", lang),
        [
            (f"order:cancel:confirm:{gid}", copy_for("cancel_yes_title", lang)),
            (f"order:cancel:abort:{gid}", copy_for("cancel_no_title", lang)),
        ],
    )


async def _handle_cancel_confirm(
    c: Container, cfg: WhatsAppConfig, event: Event, auth: AuthorizedOrder,
    controls: AdminControls, lang: str, gid: str,
) -> None:
    order = auth.order
    if order.is_cancelled():
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_cancelled", lang))
        return
    if _is_dispatched(order):
        await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_too_late", lang))
        return
    if _has_any_tag(order.tags, controls.tags.cancel_requested):
        # The provisional tag is already on the order: a prior tap requested the (async)
        # orderCancel which has not yet reflected as cancelledAt. Do NOT fire a second
        # orderCancel — confirm the pending state instead (idempotency).
        await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_requested", lang))
        return
    try:
        await c.shopify.cancel_order(auth)
    except ShopifyGraphQLError as exc:
        errors_json = json.dumps({"messages": exc.messages, "codes": list(exc.codes)})
        await c.ingest.record_order_action(
            gid, "cancel_requested", event.wa_id, event.message_id, "error", errors_json
        )
        await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_failed", lang))
        return
    # Cancel accepted (async job). The provisional tag is best-effort — a tag failure must NOT
    # reverse the outcome we report; the reconcile job applies the final `cancelled` tag once
    # Shopify confirms cancelledAt.
    await _add_tags_best_effort(c, auth, controls.tags.cancel_requested)
    await c.ingest.record_order_action(
        gid, "cancel_requested", event.wa_id, event.message_id, "ok", None
    )
    await c.ingest.set_mapping_status(gid, "cancel_requested")
    await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_requested", lang))


async def _add_tags_best_effort(c: Container, auth: AuthorizedOrder, tags: list[str]) -> None:
    try:
        await c.shopify.add_tags(auth, tags)
    except ShopifyError:
        logger.warning("button dispatch: provisional cancel tag failed (cancel already requested)")
