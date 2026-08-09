"""Cancel reconciliation job — apply the FINAL `cancelled` tag once Shopify confirms.

``orderCancel`` is async: the button-dispatch path (``core.order_actions``) requests it and writes
only the PROVISIONAL ``cancel_requested`` tag + mapping status. This job re-fetches each order
still in ``cancel_requested`` and, only once Shopify actually reports it cancelled (``cancelledAt``
set), applies the final ``cancelled`` tag and advances the mapping to ``cancelled``. A false
``cancelled`` tag is therefore never written before Shopify has really cancelled the order
(ADR-004 #3). An order not yet cancelled is left in place for the next run; a transient Shopify
error on any order never aborts the whole job.
"""

import logging
from typing import Any

from app.admin.controls import load_controls
from app.core.order_resolver import authorize_own_order
from app.deps import Container
from app.shopify.errors import ShopifyError

logger = logging.getLogger("app.jobs.reconcile")

_RECONCILE_LIMIT = 50


async def run_reconcile_cancels(c: Container) -> dict[str, Any]:
    controls = await load_controls(c.config)
    gids = await c.ingest.orders_awaiting_cancel_reconcile(_RECONCILE_LIMIT)
    reconciled = pending = skipped = 0
    for gid in gids:
        try:
            order = await c.shopify.get_order(gid)
            if order is None:
                skipped += 1
                continue
            if not order.is_cancelled():
                pending += 1  # still awaiting Shopify -> leave for the next run
                continue
            auth = authorize_own_order(order)
            if auth is None:
                # No phone on the order to satisfy the ownership invariant -> cannot tag it.
                skipped += 1
                continue
            await c.shopify.add_tags(auth, controls.tags.cancelled)
            await c.ingest.set_mapping_status(gid, "cancelled")
            await c.ingest.record_order_action(gid, "cancelled", "system", None, "ok", None)
            reconciled += 1
        except ShopifyError:
            # Transient Shopify failure on this order: it stays in cancel_requested (status not
            # advanced) so the next run retries it.
            logger.warning("reconcile cancels: shopify error for %s (will retry next run)", gid)
            pending += 1
    return {
        "checked": len(gids),
        "reconciled": reconciled,
        "pending": pending,
        "skipped": skipped,
    }
