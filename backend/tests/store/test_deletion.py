"""DPDP erasure/retention on the in-memory ingest store (delete_by_phone / purge_older_than).

Also exercises the Postgres SQL surface via a fake connection that records executed
statements — proving delete_by_phone / purge_older_than reach every phone-bearing table
(order_mappings, outbound_messages, conversations, messages, pending_actions, order_actions,
plus the age-only processed_messages purge) without needing a live database.
"""

from dataclasses import replace
from datetime import UTC, datetime

from app.shopify.models import Customer, LineItem, Order
from app.store.base import DeletionResult, MappingUpsert, OutboundDraft
from app.store.memory import InMemoryIngestStore
from app.store.postgres import PostgresIngestStore


def _mapping(gid: str, phone: str) -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid,
        order_name="tavas1",
        order_number_int=1,
        phone_e164=phone,
        customer_name="A",
        email="a@b.c",
        language="en",
        financial_status_at_create="PENDING",
        is_cod=True,
    )


def _draft(gid: str, phone: str) -> OutboundDraft:
    return OutboundDraft(
        dedupe_key=f"order_created:{gid}",
        kind="order_confirmation",
        phone_e164=phone,
        payload_json="{}",
    )


async def _seed(store: InMemoryIngestStore, gid: str, phone: str) -> None:
    await store.ingest_order_created(
        f"wh-{gid}", "orders/create", _mapping(gid, phone), _draft(gid, phone)
    )


async def test_delete_by_phone_removes_only_that_number() -> None:
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")
    await _seed(store, "gid://shopify/Order/2", "+919111111111")
    await _seed(store, "gid://shopify/Order/3", "+919222222222")

    result = await store.delete_by_phone("+919111111111")

    assert isinstance(result, DeletionResult)
    assert result.order_mappings == 2
    assert result.outbound_messages == 2
    # No conversation/action layer writes to the in-memory store yet.
    assert result.conversations == 0
    assert result.messages == 0
    assert result.pending_actions == 0
    assert result.order_actions == 0
    # The other number's rows survive.
    assert set(store.mappings) == {"gid://shopify/Order/3"}
    assert set(store.outbound) == {"order_created:gid://shopify/Order/3"}


async def test_delete_by_phone_no_match_returns_zeros() -> None:
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")

    result = await store.delete_by_phone("+910000000000")

    assert result == DeletionResult(
        order_mappings=0,
        outbound_messages=0,
        conversations=0,
        messages=0,
        pending_actions=0,
        order_actions=0,
    )
    assert set(store.mappings) == {"gid://shopify/Order/1"}


def _mirror_order(gid: str, phone: str) -> Order:
    return Order(
        gid=gid, name="tavas1", email="a@b.c", phone=None, shipping_phone=phone,
        billing_phone=None, financial_status="PENDING", fulfillment_status="UNFULFILLED",
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale="en",
        line_items=(
            LineItem(title="Blue Kurti", quantity=1, variant_title=None, price=None),
        ),
        customer=Customer(
            gid=f"{gid}/customer", first_name="A", last_name="B", email="a@b.c", phone=phone,
            address_line1="12 MG Road", address_line2=None, city="Pune", state=None,
            postal_code=None, country="India",
        ),
    )


async def test_delete_by_phone_removes_the_mirrored_order_customer_and_items() -> None:
    """The mirror tables hold name/email/phone/postal address — DPDP erasure must reach them."""
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_mirror_order("gid://shopify/Order/1", "+919111111111"))
    await store.upsert_order_mirror(_mirror_order("gid://shopify/Order/2", "+919222222222"))

    result = await store.delete_by_phone("+919111111111")

    assert result.orders == 1
    assert result.customers == 1
    assert set(store.orders) == {"gid://shopify/Order/2"}
    assert set(store.customers) == {"gid://shopify/Order/2/customer"}
    # order_items follow their order (FK ON DELETE CASCADE in Postgres).
    assert set(store.order_items) == {"gid://shopify/Order/2"}


async def test_delete_by_phone_removes_a_customer_linked_only_through_the_order() -> None:
    """The NORMAL COD shape: the customer resource carries no phone, the shipping address does.

    Matching customers on their own `phone` alone left name/email/full postal address behind
    while POST /admin/erasure still reported ok -- a false completeness signal on a legal
    obligation. The link is followed through the order's customer_gid instead.
    """
    store = InMemoryIngestStore()
    order = _mirror_order("gid://shopify/Order/1", "+919111111111")
    assert order.customer is not None
    await store.upsert_order_mirror(
        replace(order, customer=replace(order.customer, phone=None))
    )

    result = await store.delete_by_phone("+919111111111")

    assert result.orders == 1
    assert result.customers == 1
    assert store.orders == {}
    assert store.customers == {}


async def test_purge_older_than_inmemory_is_noop_without_timestamps() -> None:
    # In-memory rows carry no created_at, so age-based purge deletes nothing; the real
    # age filter is exercised against Postgres. It must still honour the Protocol cleanly.
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")

    result = await store.purge_older_than(datetime.now(UTC))

    assert result == DeletionResult(
        order_mappings=0,
        outbound_messages=0,
        conversations=0,
        messages=0,
        pending_actions=0,
        order_actions=0,
    )
    assert set(store.mappings) == {"gid://shopify/Order/1"}


class _FakeTx:
    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeConn:
    """Records executed SQL (and its bound args) so we can assert what a purge touches."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, sql: str, *args: object) -> str:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        self.calls.append((normalized, args))
        return "DELETE 0"

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        # The orders delete uses RETURNING customer_gid, so it goes through fetch, not execute.
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        self.calls.append((normalized, args))
        return []


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _targets(conn: _FakeConn) -> set[str]:
    return {sql.split("DELETE FROM ", 1)[1].split(" ", 1)[0] for sql in conn.executed}


async def test_pg_delete_by_phone_targets_every_phone_bearing_table() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.delete_by_phone("+919111111111")

    tables = _targets(conn)
    assert "order_mappings" in tables
    assert "outbound_messages" in tables
    assert "conversations" in tables
    assert "messages" in tables
    assert "pending_actions" in tables
    assert "order_actions" in tables
    # pending_actions is phone-scoped on wa_id; order_actions on actor_wa_id.
    joined = " ".join(conn.executed)
    assert "DELETE FROM pending_actions WHERE wa_id = $1" in joined
    assert "DELETE FROM order_actions WHERE actor_wa_id = $1" in joined


async def test_pg_delete_by_phone_covers_the_order_mirror_tables() -> None:
    """The mirror's customers/orders rows hold personal data keyed to a phone number."""
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.delete_by_phone("+919111111111")

    tables = _targets(conn)
    assert "customers" in tables
    assert "orders" in tables
    joined = " ".join(conn.executed)
    # An order can carry the number in any of three columns; order_items cascade off orders.
    assert (
        "DELETE FROM orders WHERE phone = $1 OR shipping_phone = $1 OR billing_phone = $1 "
        "RETURNING customer_gid"
    ) in joined
    # ...and the customers it pointed at go too, even when the customer resource itself
    # carries no phone (the usual COD shape: the phone lives on the shipping address).
    assert "DELETE FROM customers WHERE phone = $1 OR gid = ANY($2::text[])" in joined


async def test_pg_delete_by_phone_binds_the_e164_phone_to_conversations_user_id() -> None:
    """The other half of the DPDP key match (see tests/admin/test_erasure.py).

    `core.conversation` stores a conversation under the customer's normalized E.164 phone; this
    pins the erasure delete to binding that exact same string to `conversations.user_id`. If
    either side drifts again, the delete silently matches zero rows and chat history survives an
    apparently-successful erasure.
    """
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.delete_by_phone("+919111111111")

    bound = {sql: args for sql, args in conn.calls}
    assert bound["DELETE FROM conversations WHERE user_id = $1"] == ("+919111111111",)
    assert bound[
        "DELETE FROM messages WHERE conversation_id IN "
        "(SELECT id FROM conversations WHERE user_id = $1)"
    ] == ("+919111111111",)


async def test_pg_delete_by_phone_returns_new_counts() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    result = await store.delete_by_phone("+919111111111")

    assert result.pending_actions == 0
    assert result.order_actions == 0


async def test_pg_purge_older_than_ages_actions_and_dedupe_tables() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.purge_older_than(datetime.now(UTC))

    joined = " ".join(conn.executed)
    assert "DELETE FROM pending_actions WHERE created_at < $1" in joined
    assert "DELETE FROM order_actions WHERE created_at < $1" in joined
    # processed_messages is a dedupe table: not phone-scoped, aged on its own received_at.
    assert "DELETE FROM processed_messages WHERE received_at < $1" in joined


class _PurgeOneConn(_FakeConn):
    """Every DELETE removes one row, so a table only shows a non-zero count if it was queried."""

    async def execute(self, sql: str, *_: object) -> str:
        self.executed.append(" ".join(sql.split()))
        return "DELETE 1"


async def test_pg_purge_older_than_never_deletes_order_or_outbound() -> None:
    """Client decision (round 3, 2026-08-06): order/customer data is kept INDEFINITELY.

    Seed conceptually collides an old conversation/message row and an old order row under one
    cutoff; the age-based purge must age out the conversation/operational tables while the
    order (order_mappings) and its outbound_messages survive untouched — those may only ever be
    removed by delete_by_phone (erasure-on-request), never by this automatic job.
    """
    conn = _PurgeOneConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    result = await store.purge_older_than(datetime.now(UTC))

    tables = _targets(conn)
    # Order/customer data survives the age-based job entirely — never even queried.
    assert "order_mappings" not in tables
    assert "outbound_messages" not in tables
    assert result.order_mappings == 0
    assert result.outbound_messages == 0
    # Conversation/operational rows still age out (purged, count reflects the deletion).
    assert "conversations" in tables
    assert "messages" in tables
    assert result.conversations == 1
    assert result.messages == 1
    joined = " ".join(conn.executed)
    assert "DELETE FROM conversations WHERE last_active_at < $1" in joined
