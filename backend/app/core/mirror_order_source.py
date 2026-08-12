"""Adapter over OrderSource that answers order-tracking chat Q&A from our own database mirror
(customers/orders/order_items), falling back to a live Shopify call on a miss or any database
error.

Used ONLY by the Q&A pipeline (core/conversation.py). The Confirm/Cancel mutation path
(core/order_actions.py, resolve_by_gid) keeps talking to the real Shopify client directly and
never sees this class -- Critical Rule 3 (always re-fetch live before any mutation) applies only
to that path, and this module does not touch it.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

import asyncpg

from app.core.order_resolver import OrderSource
from app.shopify.models import Order

# Genuine I/O / DB failures that legitimately degrade to a Shopify fallback (a transient infra
# hiccup on this read path must not break the customer's turn). Anything else -- e.g. a KeyError
# from a row-mapping bug -- is a programming error and must surface, not masquerade as a cache
# miss forever. asyncpg.PostgresError (server errors) + asyncpg.InterfaceError (connection/usage)
# mirror the DB-error surface the rest of the store layer raises.
_FALLBACK_ERRORS = (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class MirrorReadSource(Protocol):
    """The narrow slice of IngestStore this adapter needs -- not the full Protocol, so a test
    double only has to implement these three methods to be usable here. The real IngestStore
    (Postgres or in-memory) already satisfies this structurally once Tasks 1-2 land."""

    async def get_mirrored_order(self, gid: str) -> Order | None: ...

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]: ...


class MirrorOrderSource:
    """Structurally satisfies OrderSource. Database first, live Shopify as the fallback."""

    def __init__(self, ingest: MirrorReadSource, shopify: OrderSource) -> None:
        self._ingest = ingest
        self._shopify = shopify

    async def get_order(self, gid: str) -> Order | None:
        order = await self._safe(self._ingest.get_mirrored_order, gid)
        if order is not None:
            return order
        return await self._shopify.get_order(gid)

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        order = await self._safe(self._ingest.find_mirrored_order_by_name, raw_name)
        if order is not None:
            return order
        return await self._shopify.find_order_by_name(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        orders = await self._safe(self._ingest.find_mirrored_orders_by_phone, phone_e164)
        if orders:
            return orders
        return await self._shopify.find_customer_orders_by_phone(phone_e164)

    async def _safe(self, fn: Callable[..., Awaitable[T]], *args: object) -> T | None:
        # Any database error degrades to "treat it as a miss" -- same posture as
        # _mirror_order/_mirror_customer in channels/shopify_webhook.py: an infra hiccup on this
        # read path must never break the customer's turn, it just costs one extra Shopify
        # round-trip.
        try:
            return await fn(*args)
        except _FALLBACK_ERRORS as exc:
            # Log only the exception TYPE name, never its text: an asyncpg error message can echo
            # the bound argument (here the customer's phone number) -- logging.exception would
            # render that PII.
            logger.warning(
                "mirror order-source read failed (%s); falling back to Shopify",
                type(exc).__name__,
            )
            return None
