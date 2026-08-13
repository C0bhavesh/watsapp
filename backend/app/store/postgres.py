import logging
import re
from dataclasses import replace
from datetime import datetime

import asyncpg

from app.core.phone import normalize_phone
from app.shopify.models import Customer, LineItem, Money, Order, normalize_order_name
from app.store.base import (
    DeletionResult,
    IngestResult,
    MappingUpsert,
    MappingView,
    OutboundClaim,
    OutboundDraft,
    OutboundView,
    StoredMessage,
)
from app.store.pg_factory import LazyPool

logger = logging.getLogger(__name__)


def _rows_affected(tag: str) -> int:
    """Rows from an asyncpg command tag, e.g. 'INSERT 0 10' -> 10 (parse suffix, not endswith)."""
    last = tag.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


# orders.order_number is an int4; nine digits is far beyond any real Tavas order number
# (four today) while staying inside the column's range.
_MAX_ORDER_NUMBER_DIGITS = 9

# One INSERT per line item, unbounded, runs inside a transaction holding a row lock on a
# 5-connection pool shared with the WhatsApp reply path — a bulk-operation webhook burst or a
# pathological order could stall live replies. Matches the `first: 50` cap the GraphQL
# line-items selection already uses.
MAX_MIRROR_LINE_ITEMS = 50


_MIRROR_ORDER_SELECT = (
    "SELECT o.gid, o.name, o.email, o.phone, o.shipping_phone, o.billing_phone, "
    "o.financial_status, o.fulfillment_status, o.cancelled_at, o.tags, "
    "o.payment_gateway_names, o.total_amount, o.total_currency, o.customer_locale, "
    "o.updated_at, "
    "c.gid AS c_gid, c.first_name AS c_first_name, c.last_name AS c_last_name, "
    "c.email AS c_email, c.phone AS c_phone, c.address_line1 AS c_address_line1, "
    "c.address_line2 AS c_address_line2, c.city AS c_city, c.state AS c_state, "
    "c.postal_code AS c_postal_code, c.country AS c_country, "
    "c.updated_at AS c_updated_at "
    "FROM orders o LEFT JOIN customers c ON c.gid = o.customer_gid "
)


def _order_from_row(row: asyncpg.Record, items: list[LineItem]) -> Order:
    customer = None
    if row["c_gid"] is not None:
        customer = Customer(
            gid=row["c_gid"], first_name=row["c_first_name"], last_name=row["c_last_name"],
            email=row["c_email"], phone=row["c_phone"], address_line1=row["c_address_line1"],
            address_line2=row["c_address_line2"], city=row["c_city"], state=row["c_state"],
            postal_code=row["c_postal_code"], country=row["c_country"],
            updated_at=row["c_updated_at"].isoformat() if row["c_updated_at"] else None,
        )
    total = None
    if row["total_amount"] is not None:
        total = Money(amount=row["total_amount"], currency=row["total_currency"])
    return Order(
        gid=row["gid"], name=row["name"], email=row["email"], phone=row["phone"],
        shipping_phone=row["shipping_phone"], billing_phone=row["billing_phone"],
        financial_status=row["financial_status"], fulfillment_status=row["fulfillment_status"],
        cancelled_at=row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
        tags=tuple(row["tags"] or ()),
        payment_gateway_names=tuple(row["payment_gateway_names"] or ()),
        total=total, customer_locale=row["customer_locale"],
        line_items=tuple(items), customer=customer,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )


def _line_item_from_row(r: asyncpg.Record) -> LineItem:
    # price_amount is `text`; compare `is not None` (not truthiness) so a stored "0" is kept and
    # the check stays correct if the column type ever changes. Single builder shared by the
    # per-order and the batched (`= ANY`) item reads so they cannot silently diverge.
    return LineItem(
        title=r["title"], quantity=r["quantity"], variant_title=r["variant_title"],
        price=(
            Money(r["price_amount"], r["price_currency"])
            if r["price_amount"] is not None
            else None
        ),
        sku=r["sku"],
    )


async def _fetch_mirror_line_items(conn: asyncpg.Connection, order_gid: str) -> list[LineItem]:
    rows = await conn.fetch(
        "SELECT title, sku, quantity, variant_title, price_amount, price_currency "
        "FROM order_items WHERE order_gid = $1",
        order_gid,
    )
    return [_line_item_from_row(r) for r in rows]


def _order_number_from_name(name: str) -> int | None:
    """``Order`` has no ``order_number`` field (only ``IncomingOrder`` does, for the separate
    ``order_mappings`` flow) -- derive it from ``Order.name`` at write time instead of widening
    ``Order``'s shape for one column only the mirror needs (Shopify order names are the store
    prefix + this same number, e.g. ``"tavas3733"`` -> ``3733``).

    Two ways the naive version raised on a signed delivery (a 500 burns Shopify's 19-failure
    retry budget): ``str.isdigit()`` is Unicode-aware but ``int()`` is not (``"²"`` passes the
    filter and then raises), and a date-prefixed name (``"TV20260811-3733"``) yields a value
    too large for the int4 column, raising at the DB layer. Require ASCII digits, cap the run
    length, and keep the ``ValueError`` guard as a backstop.
    """
    digits = "".join(c for c in name if c.isascii() and c.isdigit())
    if not digits or len(digits) > _MAX_ORDER_NUMBER_DIGITS:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _e164(raw: str | None) -> str | None:
    """Best-effort E.164 for a mirror phone column, keeping the original when unparseable.

    Two writers reach the mirror: the webhook parsers (already normalized) and the GraphQL
    backfill (Shopify's raw string). ``delete_by_phone`` matches these columns by exact string
    equality, so an unnormalized row would survive a DPDP erasure that catches its webhook-
    synced twin. Normalizing at the single write choke point covers both writers; an
    unparseable value is stored verbatim rather than dropped (degrade, don't discard).
    """
    return None if raw is None else (normalize_phone(raw) or raw)


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Shopify sends ``cancelled_at``/``updated_at`` as raw ISO-8601 strs; the columns are
    timestamptz.

    asyncpg's timestamptz codec requires an actual ``datetime`` (or ``None``) — binding the raw
    string raises ``asyncpg.exceptions.DataError``. Mirrors
    ``channels/shopify_orders._parse_created_at``: malformed input degrades to ``None`` rather
    than raising, since every field on a signed-but-attacker-typed payload is untrusted.
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _upsert_customer_on_conn(conn: asyncpg.Connection, customer: Customer) -> None:
    """Shared by the standalone ``upsert_customer`` and ``upsert_order_mirror``'s transaction.

    Not called from both via the standalone method itself — ``upsert_order_mirror`` must run the
    customer upsert on the SAME connection/transaction as the order + line-item writes, so this
    takes an already-acquired ``conn`` rather than acquiring its own.

    ``customer.updated_at`` is whatever freshness stamp the CALLER decided governs this write:
    the customer's own for a genuine customers/update, the ORDER's for an order-embedded
    customer (see ``upsert_order_mirror``).
    """
    applied = await conn.fetchval(
        "INSERT INTO customers (gid, first_name, last_name, email, phone, "
        "address_line1, address_line2, city, state, postal_code, country, updated_at, "
        "synced_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now()) "
        "ON CONFLICT (gid) DO UPDATE SET first_name = $2, last_name = $3, email = $4, "
        "phone = $5, address_line1 = $6, address_line2 = $7, city = $8, state = $9, "
        "postal_code = $10, country = $11, updated_at = $12, synced_at = now() "
        # Out-of-order-delivery guard: a late RETRY of an older update must not overwrite a
        # newer row. A NULL on either side still writes (backfill/payload without the field).
        "WHERE customers.updated_at IS NULL OR EXCLUDED.updated_at IS NULL "
        "OR EXCLUDED.updated_at >= customers.updated_at "
        "RETURNING gid",
        customer.gid, customer.first_name, customer.last_name, customer.email,
        _e164(customer.phone), customer.address_line1, customer.address_line2, customer.city,
        customer.state, customer.postal_code, customer.country,
        _parse_timestamp(customer.updated_at),
    )
    if applied is None:
        # Expected for a replayed/out-of-order delivery, but logged so a guard malfunction
        # (e.g. a timestamp-parsing regression) shows up instead of silently freezing the row.
        logger.info("customer mirror upsert skipped as stale: gid=%s", customer.gid)


class PostgresConfigRepo:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def get(self, key: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM app_config WHERE key = $1", key)
        return None if row is None else str(row["value"])

    async def set(self, key: str, value: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES ($1, $2, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
                key,
                value,
            )

    async def get_knowledge_override(self, kind: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content FROM knowledge_overrides WHERE kind = $1", kind
            )
        return None if row is None else str(row["content"])

    async def set_knowledge_override(self, kind: str, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO knowledge_overrides (kind, content, updated_at)"
                " VALUES ($1, $2, now())"
                " ON CONFLICT (kind) DO UPDATE"
                " SET content = EXCLUDED.content, updated_at = now()",
                kind,
                content,
            )

    async def get_knowledge_overrides(self, kinds: list[str]) -> dict[str, str | None]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind, content FROM knowledge_overrides WHERE kind = ANY($1::text[])",
                kinds,
            )
        found = {str(r["kind"]): str(r["content"]) for r in rows}
        return {k: found.get(k) for k in kinds}

    async def bump_config_int(self, key: str) -> None:
        # value must remain a decimal integer string; only our code writes this key
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO app_config (key, value) VALUES ($1, '1')"
                " ON CONFLICT (key) DO UPDATE"
                " SET value = ((app_config.value)::bigint + 1)::text, updated_at = now()",
                key,
            )


class PostgresIngestStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.execute(
                    "INSERT INTO processed_webhooks (webhook_id, topic) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    webhook_id,
                    topic,
                )
                if _rows_affected(inserted) == 0:
                    return IngestResult(duplicate=True, queued=False)
                await conn.execute(
                    "INSERT INTO order_mappings (order_gid, order_name, order_number_int, "
                    "phone_e164, customer_name, email, language, financial_status_at_create, "
                    "is_cod) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (order_gid) DO UPDATE SET phone_e164 = $4, "
                    "customer_name = $5, email = $6, language = $7, updated_at = now()",
                    mapping.order_gid,
                    mapping.order_name,
                    mapping.order_number_int,
                    mapping.phone_e164,
                    mapping.customer_name,
                    mapping.email,
                    mapping.language,
                    mapping.financial_status_at_create,
                    mapping.is_cod,
                )
                queued = False
                if outbound is not None:
                    result = await conn.execute(
                        "INSERT INTO outbound_messages (dedupe_key, kind, phone_e164, "
                        "payload_json) VALUES ($1, $2, $3, $4) ON CONFLICT (dedupe_key) "
                        "DO NOTHING",
                        outbound.dedupe_key,
                        outbound.kind,
                        outbound.phone_e164,
                        outbound.payload_json,
                    )
                    queued = _rows_affected(result) > 0
                return IngestResult(duplicate=False, queued=queued)

    async def upsert_customer(self, customer: Customer) -> None:
        async with self._pool.acquire() as conn:
            await _upsert_customer_on_conn(conn, customer)

    async def customer_exists(self, gid: str) -> bool:
        async with self._pool.acquire() as conn:
            found = await conn.fetchval("SELECT 1 FROM customers WHERE gid = $1", gid)
        return found is not None

    async def get_mirrored_order(self, gid: str) -> Order | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_MIRROR_ORDER_SELECT + "WHERE o.gid = $1", gid)
            if row is None:
                return None
            items = await _fetch_mirror_line_items(conn, gid)
        return _order_from_row(row, items)

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        # Parity with ShopifyClient.find_order_by_name: a normalized name that is not [a-z0-9]+ is
        # not a valid lookup key -- reject it before querying so both OrderSource impls agree.
        if re.fullmatch(r"[a-z0-9]+", name) is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_MIRROR_ORDER_SELECT + "WHERE o.name = $1", name)
            if row is None:
                return None
            items = await _fetch_mirror_line_items(conn, str(row["gid"]))
        return _order_from_row(row, items)

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        # Backfilled history never writes order_mappings, so resolve_by_phone's fast path is
        # empty for it and falls through here -- a customer with a long history could otherwise
        # scan every order + round-trip once per order for line items on the 5-connection pool
        # shared with the live reply path. Cap at 10 most-recent (matching the Shopify fallback's
        # `first: 10`) and batch-fetch all items in one query keyed by order gid (no N+1).
        #
        # Q16 (docs/FR/client-decisions-all.md, ANSWERED 2026-08-12): this chat-Q&A lookup matches
        # the buyer's own `o.phone` OR its `o.shipping_phone` (the single $1 reused for both). This
        # store's Shopflo checkout frequently leaves the order's top-level phone empty, but the
        # shipping contact number is reliably present -- the same reason the order-confirmation push
        # falls back to it -- so restricting to o.phone alone would return nothing for many real
        # orders. Still NARROWER than delete_by_phone's three-column erasure predicate: billing
        # stays excluded (never asked for), and erasure vs disclosure have different safety
        # directions so their match sets legitimately differ.
        #
        # NULLS LAST: o.updated_at is nullable, and a plain DESC sorts NULLs FIRST -- that would
        # push genuinely-recent orders out of the LIMIT 10. o.gid DESC is a deterministic tiebreak.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                _MIRROR_ORDER_SELECT
                + "WHERE o.phone = $1 OR o.shipping_phone = $1 "
                "ORDER BY o.updated_at DESC NULLS LAST, o.gid DESC LIMIT 10",
                phone_e164,
            )
            if not rows:
                return []
            gids = [str(row["gid"]) for row in rows]
            item_rows = await conn.fetch(
                "SELECT order_gid, title, sku, quantity, variant_title, price_amount, "
                "price_currency FROM order_items WHERE order_gid = ANY($1)",
                gids,
            )
        items_by_gid: dict[str, list[LineItem]] = {}
        for r in item_rows:
            items_by_gid.setdefault(str(r["order_gid"]), []).append(_line_item_from_row(r))
        return [_order_from_row(row, items_by_gid.get(str(row["gid"]), [])) for row in rows]

    async def upsert_order_mirror(self, order: Order) -> None:
        # LOCK ORDER: customers, then orders. `delete_by_phone` takes them the other way round
        # (it needs RETURNING customer_gid from the orders delete first), so a sync racing an
        # erasure for the same customer can deadlock; Postgres aborts one side, which is a
        # failed statement, never corruption. Erasure is a rare manual admin action, so the
        # inversion is accepted rather than designed around -- but do not deepen it.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if order.customer is not None:
                    # The embedded customer's address fields are a SNAPSHOT of THIS order's
                    # shipping address, so the order's freshness governs them -- the customer
                    # resource's own updated_at does not change when an order does, and two
                    # deliveries for the same order can carry an identical customer stamp with
                    # different addresses (an older one would otherwise revert the address).
                    await _upsert_customer_on_conn(
                        conn, replace(order.customer, updated_at=order.updated_at)
                    )
                customer_gid = order.customer.gid if order.customer is not None else None
                total_amount = order.total.amount if order.total is not None else None
                total_currency = order.total.currency if order.total is not None else None
                applied = await conn.fetchval(
                    "INSERT INTO orders (gid, name, order_number, customer_gid, email, "
                    "phone, shipping_phone, billing_phone, financial_status, "
                    "fulfillment_status, cancelled_at, tags, payment_gateway_names, "
                    "total_amount, total_currency, customer_locale, updated_at, synced_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, "
                    "$16, $17, now()) ON CONFLICT (gid) DO UPDATE SET name = $2, "
                    "order_number = $3, "
                    "customer_gid = $4, email = $5, phone = $6, shipping_phone = $7, "
                    "billing_phone = $8, financial_status = $9, fulfillment_status = $10, "
                    "cancelled_at = $11, tags = $12, payment_gateway_names = $13, "
                    "total_amount = $14, total_currency = $15, customer_locale = $16, "
                    "updated_at = $17, synced_at = now() "
                    # Out-of-order-delivery guard (see _upsert_customer_on_conn): a late RETRY
                    # of an older orders/updated must not revert newer state -- on a terminal
                    # order (cancelled/fulfilled) nothing would ever correct it again.
                    "WHERE orders.updated_at IS NULL OR EXCLUDED.updated_at IS NULL "
                    "OR EXCLUDED.updated_at >= orders.updated_at "
                    "RETURNING gid",
                    order.gid, order.name, _order_number_from_name(order.name),
                    customer_gid, order.email, _e164(order.phone),
                    _e164(order.shipping_phone), _e164(order.billing_phone),
                    order.financial_status,
                    order.fulfillment_status, _parse_timestamp(order.cancelled_at),
                    list(order.tags),
                    list(order.payment_gateway_names), total_amount, total_currency,
                    order.customer_locale, _parse_timestamp(order.updated_at),
                )
                if applied is None:
                    # The guard rejected this write as stale; leave the stored items alone
                    # rather than replacing them with older ones. Logged (see
                    # _upsert_customer_on_conn) so a guard malfunction is visible.
                    logger.info("mirror upsert skipped as stale: gid=%s", order.gid)
                    return
                # ORDERING IS LOAD-BEARING: the orders upsert above takes the row-exclusive
                # lock that serializes concurrent same-gid syncs. Only because it runs FIRST is
                # this delete + re-insert safe from interleaving into duplicated line items
                # (there is no unique constraint on order_items enforcing that independently).
                # Do not reorder these statements or move them out of this transaction.
                await conn.execute(
                    "DELETE FROM order_items WHERE order_gid = $1", order.gid
                )
                rows = [
                    (
                        order.gid, item.title, item.sku, item.quantity, item.variant_title,
                        item.price.amount if item.price is not None else None,
                        item.price.currency if item.price is not None else None,
                    )
                    for item in order.line_items[:MAX_MIRROR_LINE_ITEMS]
                ]
                if rows:
                    await conn.executemany(
                        "INSERT INTO order_items (order_gid, title, sku, quantity, "
                        "variant_title, price_amount, price_currency) VALUES "
                        "($1, $2, $3, $4, $5, $6, $7)",
                        rows,
                    )

    async def recent_mappings(self, limit: int) -> list[MappingView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid, order_name, phone_e164, status, is_cod, created_at"
                " FROM order_mappings ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            MappingView(
                order_gid=str(r["order_gid"]),
                order_name=str(r["order_name"]),
                phone_e164=None if r["phone_e164"] is None else str(r["phone_e164"]),
                status=str(r["status"]),
                is_cod=bool(r["is_cod"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    async def recent_outbound(self, limit: int) -> list[OutboundView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT dedupe_key, state, kind, phone_e164, attempts, last_error_code,"
                " created_at FROM outbound_messages ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            OutboundView(
                dedupe_key=str(r["dedupe_key"]),
                state=str(r["state"]),
                kind=str(r["kind"]),
                phone_e164=str(r["phone_e164"]),
                attempts=int(r["attempts"]),
                last_error_code=(
                    None if r["last_error_code"] is None else str(r["last_error_code"])
                ),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid, order_name, phone_e164, status, is_cod, created_at"
                " FROM order_mappings WHERE phone_e164 = $1 ORDER BY created_at DESC LIMIT $2",
                phone_e164,
                limit,
            )
        return [
            MappingView(
                order_gid=str(r["order_gid"]),
                order_name=str(r["order_name"]),
                phone_e164=None if r["phone_e164"] is None else str(r["phone_e164"]),
                status=str(r["status"]),
                is_cod=bool(r["is_cod"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult:
        """DPDP right-to-erasure: purge every row keyed to one phone number, atomically.

        Covers order_mappings, outbound_messages, conversations, messages, pending_actions,
        order_actions and the order-mirror tables. The mirror needs two passes: orders are
        deleted first (matching any of phone/shipping_phone/billing_phone) with
        ``RETURNING customer_gid``, then customers are deleted by their own ``phone`` OR by one
        of those returned gids. Matching customers on ``phone`` alone was not enough — a
        Shopify customer resource usually carries NO phone (the number lives on the shipping
        address), so the customer's name/email/postal address survived erasure on the ordinary
        COD order while the endpoint still reported success. ``order_items`` follow their order
        via the FK's ON DELETE CASCADE, so they need no statement of their own.

        Children (messages) are deleted before parents (conversations) for FK integrity.
        conversations/messages have no repo writer yet (Phase 4) but are cleaned defensively
        so erasure is complete the moment that layer starts persisting. pending_actions is
        scoped by ``wa_id`` and order_actions by ``actor_wa_id`` (both the requester's number).

        Known residuals:
        - processed_messages retains dedupe rows whose message_id embeds the requester's phone
          in Meta's wamid encoding; these age out via purge_older_than's received_at cutoff but
          are not covered by an on-demand phone-scoped delete (decoding wamids to recover the
          sender number is deliberately out of scope).
        - an order whose ONLY phone lives on the linked customer resource (none of its own
          phone/shipping_phone/billing_phone columns set) is not matched by the orders delete;
          its customer row still goes (matched on that phone), and the FK's ON DELETE SET NULL
          leaves the order behind minus the link. Rare for a COD store, whose orders reliably
          carry a shipping phone, but not impossible.

        LOCK ORDER: orders, then customers -- the reverse of ``upsert_order_mirror`` (see the
        note there); a concurrent erasure and mirror sync on the same customer can deadlock,
        which Postgres resolves by aborting one side.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                msgs = await conn.execute(
                    "DELETE FROM messages WHERE conversation_id IN "
                    "(SELECT id FROM conversations WHERE user_id = $1)",
                    phone_e164,
                )
                convs = await conn.execute(
                    "DELETE FROM conversations WHERE user_id = $1", phone_e164
                )
                outbound = await conn.execute(
                    "DELETE FROM outbound_messages WHERE phone_e164 = $1", phone_e164
                )
                mappings = await conn.execute(
                    "DELETE FROM order_mappings WHERE phone_e164 = $1", phone_e164
                )
                pending = await conn.execute(
                    "DELETE FROM pending_actions WHERE wa_id = $1", phone_e164
                )
                actions = await conn.execute(
                    "DELETE FROM order_actions WHERE actor_wa_id = $1", phone_e164
                )
                order_rows = await conn.fetch(
                    "DELETE FROM orders WHERE phone = $1 OR shipping_phone = $1 "
                    "OR billing_phone = $1 RETURNING customer_gid",
                    phone_e164,
                )
                linked_customer_gids = [
                    str(r["customer_gid"]) for r in order_rows if r["customer_gid"] is not None
                ]
                customers = await conn.execute(
                    "DELETE FROM customers WHERE phone = $1 OR gid = ANY($2::text[])",
                    phone_e164,
                    linked_customer_gids,
                )
        return DeletionResult(
            order_mappings=_rows_affected(mappings),
            outbound_messages=_rows_affected(outbound),
            conversations=_rows_affected(convs),
            messages=_rows_affected(msgs),
            pending_actions=_rows_affected(pending),
            order_actions=_rows_affected(actions),
            customers=_rows_affected(customers),
            orders=len(order_rows),
        )

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult:
        """DPDP age-based retention: delete only the deletable tables older than ``cutoff``.

        ``order_mappings`` and ``outbound_messages`` are DELIBERATELY EXCLUDED and are never
        touched by this automatic job — the client decided (round 3, 2026-08-06,
        docs/FR/client-decisions-all.md Q15) that customer/order data (profile, name, phone,
        order history/number/date, products, SKU, payment method, status, spend, count, tags)
        is kept INDEFINITELY. Do NOT re-add DELETEs for those tables here: on-demand
        right-to-erasure (``delete_by_phone`` / POST /admin/erasure) is the only path allowed to
        remove order/outbound rows, and only for a specific number on request.

        Only the "AI conversation history / temporary AI context / processed-message logs /
        operational data" category (client-approved for deletion after 365 days) ages out:
        conversations/messages on ``last_active_at``, pending_actions/order_actions on
        ``created_at``, processed_messages on ``received_at``. Children first (FK). The
        ``order_mappings``/``outbound_messages`` counts in the returned DeletionResult are
        therefore always 0 (kept indefinitely); processed_messages' blanket count is not
        reported (no phone column, not attributable).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                msgs = await conn.execute(
                    "DELETE FROM messages WHERE conversation_id IN "
                    "(SELECT id FROM conversations WHERE last_active_at < $1)",
                    cutoff,
                )
                convs = await conn.execute(
                    "DELETE FROM conversations WHERE last_active_at < $1", cutoff
                )
                pending = await conn.execute(
                    "DELETE FROM pending_actions WHERE created_at < $1", cutoff
                )
                actions = await conn.execute(
                    "DELETE FROM order_actions WHERE created_at < $1", cutoff
                )
                # Blanket age-out of the dedupe table (no phone column, not attributable).
                await conn.execute(
                    "DELETE FROM processed_messages WHERE received_at < $1", cutoff
                )
        return DeletionResult(
            order_mappings=0,  # kept indefinitely — never age-purged (client Q15)
            outbound_messages=0,  # kept indefinitely — never age-purged (client Q15)
            conversations=_rows_affected(convs),
            messages=_rows_affected(msgs),
            pending_actions=_rows_affected(pending),
            order_actions=_rows_affected(actions),
        )

    async def count_orders_by_phone(self, phone_e164: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS n FROM order_mappings WHERE phone_e164 = $1", phone_e164
            )
        return int(row["n"]) if row else 0

    # --- Phase 5: outbox drain + mutation audit + mapping status ---

    async def claim_queued_outbound(self, limit: int = 20) -> list[OutboundClaim]:
        # Atomic claim: SELECT the oldest queued rows AND flip them to 'processing' in ONE
        # round trip. FOR UPDATE SKIP LOCKED locks the selected rows and makes a concurrent
        # claim skip them, so two overlapping cron runs (one still in flight when the next
        # 1-minute tick fires) can NEVER both receive the same row -> no double-send. The CTE
        # with a row-locking SELECT must run inside an explicit transaction on the connection
        # (same pattern as ingest_order_created above).
        #
        # The predicate also RECLAIMS stale in-flight rows: a row a killed invocation flipped to
        # 'processing' but never finished sending (Vercel timeout / instance reclaim) would
        # otherwise be stranded there forever — nothing else re-queues a 'processing' row. Any
        # 'processing' row whose updated_at is older than 10 minutes is treated as abandoned and
        # re-claimed. That threshold is two orders of magnitude above send_template's 20s timeout,
        # so a genuinely in-flight row (claimed moments ago, fresh updated_at) can NEVER collide
        # with this reclaim; only rows truly abandoned by a dead invocation are recovered.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "WITH claimed AS ("
                    " SELECT id FROM outbound_messages"
                    " WHERE state = 'queued'"
                    " OR (state = 'processing' AND updated_at < now() - interval '10 minutes')"
                    " ORDER BY created_at"
                    " LIMIT $1"
                    " FOR UPDATE SKIP LOCKED"
                    ")"
                    " UPDATE outbound_messages SET state = 'processing', updated_at = now()"
                    " WHERE id IN (SELECT id FROM claimed)"
                    " RETURNING id, dedupe_key, phone_e164, payload_json, attempts",
                    limit,
                )
        return [
            OutboundClaim(
                id=int(r["id"]),
                dedupe_key=str(r["dedupe_key"]),
                phone_e164=str(r["phone_e164"]),
                payload_json=str(r["payload_json"]),
                attempts=int(r["attempts"]),
            )
            for r in rows
        ]

    async def mark_outbound_sent(self, id: int, wamid: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbound_messages SET state = 'sent', template_wamid = $2,"
                " updated_at = now() WHERE id = $1",
                id,
                wamid,
            )

    async def mark_outbound_suppressed(self, id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbound_messages SET state = 'suppressed', updated_at = now()"
                " WHERE id = $1",
                id,
            )

    async def mark_outbound_undeliverable(self, id: int, code: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbound_messages SET state = 'undeliverable', last_error_code = $2,"
                " updated_at = now() WHERE id = $1",
                id,
                code,
            )

    async def bump_outbound_attempt(self, id: int, code: str, max_attempts: int = 5) -> str:
        # `attempts` in the CASE/SET refers to the pre-update value, so `attempts + 1` is the new
        # count. RETURNING gives the resulting state so the caller learns 'queued' vs 'failed'.
        # A retryable failure moves the row 'processing' -> 'queued' so a LATER cron run can
        # re-claim and re-send it; only once attempts hit max_attempts does it go to 'failed'.
        # It must NOT be left in 'processing' (that would strand it — nothing re-claims a
        # processing row). Explicit ELSE 'queued', never ELSE state, is the whole point.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE outbound_messages"
                " SET attempts = attempts + 1, last_error_code = $2,"
                " state = CASE WHEN attempts + 1 >= $3 THEN 'failed' ELSE 'queued' END,"
                " updated_at = now()"
                " WHERE id = $1 RETURNING state",
                id,
                code,
                max_attempts,
            )
        return str(row["state"]) if row else "failed"

    async def set_mapping_status(self, order_gid: str, status: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE order_mappings SET status = $2, updated_at = now() WHERE order_gid = $1",
                order_gid,
                status,
            )

    async def record_order_action(
        self,
        order_gid: str,
        action: str,
        actor_wa_id: str | None,
        source_wamid: str | None,
        result: str,
        user_errors_json: str | None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO order_actions (order_gid, action, actor_wa_id, source_wamid,"
                " result, user_errors_json) VALUES ($1, $2, $3, $4, $5, $6)",
                order_gid,
                action,
                actor_wa_id,
                source_wamid,
                result,
                user_errors_json,
            )

    async def orders_awaiting_cancel_reconcile(self, limit: int = 50) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid FROM order_mappings WHERE status = 'cancel_requested'"
                " ORDER BY updated_at LIMIT $1",
                limit,
            )
        return [str(r["order_gid"]) for r in rows]


class PostgresMessageStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def record_if_new(self, message_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO processed_messages (message_id) VALUES ($1) "
                "ON CONFLICT DO NOTHING",
                message_id,
            )
        return _rows_affected(result) > 0


class PostgresConversationStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def get_or_create(self, user_id: str) -> int:
        # Single atomic upsert (relies on the ux_conversations_user_id unique index) instead
        # of SELECT-then-INSERT: two concurrent first-contact messages from the same sender
        # can otherwise both miss the SELECT and both INSERT, splitting history across two
        # conversation ids. ON CONFLICT makes this one round trip with no race window.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO conversations (user_id) VALUES ($1)"
                " ON CONFLICT (user_id) DO UPDATE SET last_active_at = now()"
                " RETURNING id",
                user_id,
            )
        return int(row["id"])

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = $1"
                " ORDER BY created_at DESC LIMIT $2",
                conversation_id,
                limit,
            )
        ordered = list(reversed(rows))
        return [
            StoredMessage(
                role=str(r["role"]),
                content=str(r["content"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in ordered
        ]

    async def append_message(self, conversation_id: int, role: str, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)",
                conversation_id,
                role,
                content,
            )

    async def pause_until(self, conversation_id: int, until: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET paused_until = $1 WHERE id = $2", until, conversation_id
            )

    async def get_paused_until(self, conversation_id: int) -> datetime | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT paused_until FROM conversations WHERE id = $1", conversation_id
            )
        return None if row is None else row["paused_until"]

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET handoff_attempted_at = $1 WHERE id = $2",
                at,
                conversation_id,
            )

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT handoff_attempted_at FROM conversations WHERE id = $1", conversation_id
            )
        return None if row is None else row["handoff_attempted_at"]
