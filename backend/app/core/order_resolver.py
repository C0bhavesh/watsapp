import asyncio
from typing import Protocol

from app.core.phone import normalize_phone
from app.shopify.errors import ShopifyError
from app.shopify.models import AuthorizedOrder, Order
from app.store.base import IngestStore


class OrderSource(Protocol):
    """What order_resolver needs from Shopify -- an interface, not the concrete client
    (core depends on interfaces, not adapters). ``ShopifyClient`` already matches this
    shape structurally; no inheritance is required."""

    async def get_order(self, gid: str) -> Order | None: ...

    async def find_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]: ...


async def resolve_by_phone(
    shopify: OrderSource, ingest: IngestStore, wa_id: str
) -> list[AuthorizedOrder]:
    """Resolve every order this WhatsApp sender can be shown, re-fetched live from Shopify.

    Chain: our own order_mappings (fast path, built by the Phase 2 webhook) -> Shopify
    customer-by-phone fallback. Every candidate is re-fetched live (never trust the mapping
    snapshot) and re-verified through AuthorizedOrder's own ownership invariant, which raises
    if the live phone no longer matches -- a stale mapping is silently dropped, never
    surfaced. A Shopify outage degrades to whatever was already resolved (often empty) rather
    than raising, so a temporary Shopify blip does not stop the conversation.

    The mapped orders are re-fetched CONCURRENTLY: awaiting them one at a time added a full
    Shopify round-trip per extra order to the customer's wait for a reply.
    """
    phone = normalize_phone(wa_id)
    if phone is None:
        return []
    orders: list[AuthorizedOrder] = []
    try:
        mappings = await ingest.find_mappings_by_phone(phone)
        fetched = await asyncio.gather(
            *(shopify.get_order(mapping.order_gid) for mapping in mappings),
            return_exceptions=True,
        )
        degraded = False
        for item in fetched:
            if isinstance(item, BaseException):
                if isinstance(item, ShopifyError):
                    degraded = True
                    continue
                raise item
            if item is None:
                continue
            try:
                orders.append(AuthorizedOrder(order=item, verified_phone=phone))
            except ValueError:
                continue
        # `degraded` keeps the sequential version's behaviour: once Shopify has failed on this
        # turn, fall back to whatever resolved rather than spending another call on the
        # customer-by-phone lookup.
        if orders or degraded:
            return orders
        for order in await shopify.find_customer_orders_by_phone(phone):
            try:
                orders.append(AuthorizedOrder(order=order, verified_phone=phone))
            except ValueError:
                continue
    except ShopifyError:
        return orders
    return orders


def authorize_own_order(order: Order) -> AuthorizedOrder | None:
    """Wrap an order in an AuthorizedOrder for a SYSTEM operation on the order itself.

    Used by the reconcile job to apply the final ``cancelled`` tag to an order the bot already
    cancelled, where there is no WhatsApp tapper. The order's own best phone matches it by
    definition, so this satisfies AuthorizedOrder's ownership invariant without a sender. Kept
    here so ADR-004's "only order_resolver constructs AuthorizedOrder" holds. Returns None if the
    order has no phone to verify against (nothing to authorize).
    """
    phone = order.best_phone()
    if phone is None:
        return None
    try:
        return AuthorizedOrder(order=order, verified_phone=phone)
    except ValueError:
        return None


async def resolve_by_gid(
    shopify: OrderSource, wa_id: str, gid: str
) -> AuthorizedOrder | None:
    """Re-fetch an order by its gid live and ownership-check it against the tapper's phone.

    The deterministic button-dispatch path (Phase 5) calls this for every tap BEFORE any
    tag/cancel mutation: it never trusts the outbox snapshot or the button payload's gid.
    Returns None both when the order is missing/unfetchable AND when it belongs to a different
    phone, so a foreign or unknown gid yields the same non-enumerable refusal (Critical Rule 3,
    ADR-004). Only this module constructs the ``AuthorizedOrder`` mutation gate.
    """
    phone = normalize_phone(wa_id)
    if phone is None:
        return None
    try:
        order = await shopify.get_order(gid)
    except ShopifyError:
        return None
    if order is None:
        return None
    try:
        return AuthorizedOrder(order=order, verified_phone=phone)
    except ValueError:
        return None


async def resolve_by_order_name(
    shopify: OrderSource, wa_id: str, raw_name: str
) -> AuthorizedOrder | None:
    """Look up an order by the number the customer typed, ownership-checked against wa_id.

    Returns None both when the order does not exist AND when it exists but belongs to a
    different phone number, so a reply can never be used to enumerate whether an order
    number is valid (Critical Rule 3).
    """
    phone = normalize_phone(wa_id)
    if phone is None:
        return None
    try:
        order = await shopify.find_order_by_name(raw_name)
    except ShopifyError:
        return None
    if order is None:
        return None
    try:
        return AuthorizedOrder(order=order, verified_phone=phone)
    except ValueError:
        return None
