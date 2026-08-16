"""Cancel reconciliation job — apply the FINAL `cancelled` tag once Shopify confirms, and notify
the customer with `cod_cancel` once that happens.

``orderCancel`` is async: the button-dispatch path (``core.order_actions``) requests it and writes
only the PROVISIONAL ``cancel_requested`` tag + mapping status, with a deliberately soft plain-text
reply ("we have requested cancellation..."). This job re-fetches each order still in
``cancel_requested`` and, only once Shopify actually reports it cancelled (``cancelledAt`` set),
applies the final ``cancelled`` tag, advances the mapping, and sends the ``cod_cancel`` template
(whose wording asserts completion, so it must never fire before this point). A false ``cancelled``
tag is therefore never written before Shopify has really cancelled the order (ADR-004 #3). An
order not yet cancelled is left in place for the next run; a transient Shopify error on any order
never aborts the whole job. The notification is best-effort: a missing WhatsApp config or a
transport failure is logged and skipped, never rolls back the tag/status write, and never aborts
the job for other orders in the batch.
"""

import logging
from typing import Any, Literal

from app.admin.controls import AdminControls, load_controls
from app.channels.shopify_orders import customer_display_name
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_sender import WhatsAppSendError, send_template
from app.config.crypto import VaultError
from app.core.order_resolver import authorize_own_order
from app.core.phone import normalize_phone
from app.core.send_policy import send_decision
from app.deps import Container
from app.shopify.errors import ShopifyError
from app.shopify.models import AuthorizedOrder

logger = logging.getLogger("app.jobs.reconcile")

_RECONCILE_LIMIT = 50
_CANCEL_TEMPLATE = "cod_cancel"
# cod_cancel is Meta-approved in `en` ONLY (checked live during planning, same situation as
# cod_confirmmsg/cod_confirmation/prepaid_order, Q19c) -- pinned regardless of the order's language.
_CANCEL_TEMPLATE_LANGUAGE = "en"


async def _notify_cancelled(
    c: Container,
    cfg: WhatsAppConfig,
    controls: AdminControls,
    auth: AuthorizedOrder,
    gid: str,
) -> Literal["notified", "skipped"]:
    """Best-effort cod_cancel send. Returns "notified" on a confirmed 2xx, "skipped" for every
    non-send branch (suppressed by the kill switch, no deliverable phone, a >=400 Meta response,
    or a transport failure) so the caller can report notification counters."""
    # Route to the phone that actually held the WhatsApp conversation: the order's mapping phone
    # (already E.164), falling back to the order's own best phone NORMALIZED to E.164. auth
    # .verified_phone is a RAW Shopify string (Shopify supplies customer phones as free text, not
    # guaranteed E.164) -- sending it straight to Meta risks a bare 10-digit number being resolved
    # under the wrong country code and delivering the customer's name + order number to an
    # unrelated person. Skip entirely (gid-only log, no PII) if neither resolves to E.164.
    to = await c.ingest.get_mapping_phone(gid)
    if to is None:
        to = normalize_phone(auth.verified_phone)
    if to is None:
        logger.warning("reconcile cancels: no deliverable phone for %s; cod_cancel skipped", gid)
        return "skipped"
    decision = send_decision(controls.send_mode, controls.allowlist_phones, to)
    if decision == "suppress":
        return "skipped"
    try:
        result = await send_template(
            c.http, cfg, to, _CANCEL_TEMPLATE, _CANCEL_TEMPLATE_LANGUAGE,
            [customer_display_name(auth.order), auth.order.name],
        )
    except WhatsAppSendError:
        logger.warning(
            "reconcile cancels: cod_cancel notification failed for %s (transport)", gid
        )
        return "skipped"
    if not result.ok:
        # A >=400 Meta response (expired token, template paused, rate limit) does NOT raise; it
        # returns ok=False. Log it (result.error is already token-redacted by _safe_error) so a
        # failing send is visible instead of silently swallowed.
        logger.warning(
            "reconcile cancels: cod_cancel send failed for %s (status=%s error=%s)",
            gid, result.status_code, result.error,
        )
        return "skipped"
    return "notified"


async def run_reconcile_cancels(c: Container) -> dict[str, Any]:
    controls = await load_controls(c.config)
    # Loaded ONCE per run (not per order) -- config doesn't change mid-run, and a corrupt/missing
    # WhatsApp config must never crash this job (which has never touched WhatsApp before): it just
    # means notifications are skipped for this run while reconciliation proceeds normally.
    try:
        cfg = await load_whatsapp_config(c.config)
    except VaultError:
        logger.warning(
            "reconcile cancels: whatsapp config unreadable; notifications skipped this run"
        )
        cfg = None
    # orders_awaiting_cancel_reconcile atomically CLAIMS each returned order (flips it to a
    # transient in-progress state) so two overlapping runs never double-process it. Every claimed
    # gid must therefore be finalized below: advanced to 'cancelled' on success, or RELEASED back
    # to 'cancel_requested' on any skip/failure (via set_mapping_status) so the row is not lost.
    gids = await c.ingest.orders_awaiting_cancel_reconcile(_RECONCILE_LIMIT)
    reconciled = pending = skipped = 0
    notified = notify_skipped = 0
    for gid in gids:
        try:
            order = await c.shopify.get_order(gid)
            if order is None:
                skipped += 1
                await c.ingest.set_mapping_status(gid, "cancel_requested")  # release claim
                continue
            if not order.is_cancelled():
                pending += 1  # still awaiting Shopify -> leave for the next run
                await c.ingest.set_mapping_status(gid, "cancel_requested")  # release claim
                continue
            auth = authorize_own_order(order)
            if auth is None:
                # No phone on the order to satisfy the ownership invariant -> cannot tag it.
                skipped += 1
                await c.ingest.set_mapping_status(gid, "cancel_requested")  # release claim
                continue
            await c.shopify.add_tags(auth, controls.tags.cancelled)
            await c.ingest.set_mapping_status(gid, "cancelled")
            await c.ingest.record_order_action(gid, "cancelled", "system", None, "ok", None)
            if cfg is not None:
                outcome = await _notify_cancelled(c, cfg, controls, auth, gid)
                if outcome == "notified":
                    notified += 1
                else:
                    notify_skipped += 1
            else:
                # WhatsApp config unreadable this run -> notification skipped (job proceeds).
                notify_skipped += 1
            reconciled += 1
        except ShopifyError:
            # Transient Shopify failure on this order: release the claim back to cancel_requested
            # (status not advanced) so the next run retries it.
            logger.warning("reconcile cancels: shopify error for %s (will retry next run)", gid)
            pending += 1
            await c.ingest.set_mapping_status(gid, "cancel_requested")  # release claim
    return {
        "checked": len(gids),
        "reconciled": reconciled,
        "pending": pending,
        "skipped": skipped,
        "notified": notified,
        "notify_skipped": notify_skipped,
    }
