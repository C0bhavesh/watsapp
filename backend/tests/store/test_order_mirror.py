import os
import uuid
from datetime import datetime

import pytest

from app.shopify.models import Customer, LineItem, Money, Order
from app.store.memory import InMemoryIngestStore
from app.store.pg_factory import LazyPool
from app.store.postgres import (
    MAX_MIRROR_LINE_ITEMS,
    PostgresIngestStore,
    _order_number_from_name,
)

DSN = os.environ.get("TEST_DATABASE_URL", "")


def test_order_number_from_name_extracts_digits() -> None:
    assert _order_number_from_name("tavas3733") == 3733


def test_order_number_from_name_no_digits_is_none() -> None:
    assert _order_number_from_name("tavas") is None


def test_order_number_from_name_rejects_overlong_digit_runs() -> None:
    """orders.order_number is a Postgres `integer`; an oversized value raises at the DB layer.

    A date-prefixed or otherwise unusual order name is not a Tavas order number — return None
    rather than binding a value the column cannot hold.
    """
    assert _order_number_from_name("TV20260811-3733") is None
    assert _order_number_from_name("tavas123456789012345") is None
    # Nine digits still fits an int4 and is honoured.
    assert _order_number_from_name("tavas123456789") == 123456789


def test_order_number_from_name_ignores_unicode_digits() -> None:
    # `"²".isdigit()` is True but `int("²")` raises — an unguarded conversion would 500 the
    # signed webhook (same class as the push_staleness_hours ASCII-digit gate).
    assert _order_number_from_name("tavas²") is None
    assert _order_number_from_name("tavas٣٠") is None


def _customer(gid: str = "gid://shopify/Customer/1", **overrides: object) -> Customer:
    base = dict(
        gid=gid, first_name="Suman", last_name="Bayala", email="c@example.com",
        phone="+919999999999", address_line1="12 MG Road", address_line2=None,
        city="Bengaluru", state="Karnataka", postal_code="560001", country="India",
    )
    base.update(overrides)
    return Customer(**base)  # type: ignore[arg-type]


def _order(gid: str = "gid://shopify/Order/1", **overrides: object) -> Order:
    base = dict(
        gid=gid, name="tavas3733", email="c@example.com", phone=None,
        shipping_phone="+919999999999", billing_phone=None, financial_status="PENDING",
        fulfillment_status="UNFULFILLED", cancelled_at=None, tags=("COD",),
        payment_gateway_names=("Cash on Delivery (COD)",),
        total=Money("949.00", "INR"), customer_locale="en",
        line_items=(
            LineItem(title="Blue Kurti", quantity=1, variant_title="Blue / M",
                      price=Money("999.00", "INR")),
        ),
        customer=_customer(),
    )
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


async def test_upsert_customer_stores_a_new_row() -> None:
    store = InMemoryIngestStore()
    await store.upsert_customer(_customer())
    assert store.customers["gid://shopify/Customer/1"].city == "Bengaluru"  # type: ignore[attr-defined]


async def test_upsert_customer_updates_existing_row_in_place() -> None:
    store = InMemoryIngestStore()
    await store.upsert_customer(_customer())
    await store.upsert_customer(_customer(city="Mumbai"))
    assert len(store.customers) == 1  # type: ignore[attr-defined]
    assert store.customers["gid://shopify/Customer/1"].city == "Mumbai"  # type: ignore[attr-defined]


async def test_upsert_order_mirror_stores_order_and_items_and_customer() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order())
    assert store.orders["gid://shopify/Order/1"].name == "tavas3733"  # type: ignore[attr-defined]
    assert len(store.order_items["gid://shopify/Order/1"]) == 1  # type: ignore[attr-defined]
    assert store.customers["gid://shopify/Customer/1"].first_name == "Suman"  # type: ignore[attr-defined]


async def test_upsert_order_mirror_without_customer_leaves_no_customer_row() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order(customer=None))
    assert store.orders["gid://shopify/Order/1"].customer is None  # type: ignore[attr-defined]
    assert store.customers == {}  # type: ignore[attr-defined]


async def test_upsert_order_mirror_replaces_items_not_appends() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order())
    updated = _order(line_items=(
        LineItem(title="New Item", quantity=2, variant_title=None, price=None),
    ))
    await store.upsert_order_mirror(updated)
    items = store.order_items["gid://shopify/Order/1"]  # type: ignore[attr-defined]
    assert len(items) == 1
    assert items[0].title == "New Item"


async def test_customer_exists_reports_only_known_customers() -> None:
    store = InMemoryIngestStore()
    assert await store.customer_exists("gid://shopify/Customer/1") is False
    await store.upsert_customer(_customer())
    assert await store.customer_exists("gid://shopify/Customer/1") is True


class _FakeTx:
    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _RecordingConn:
    """Captures the SQL a mirror upsert runs, without needing a live Postgres."""

    def __init__(self, order_upsert_applied: bool = True) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.many: list[tuple[str, list[tuple[object, ...]]]] = []
        self._order_upsert_applied = order_upsert_applied

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((" ".join(sql.split()), args))
        return "INSERT 0 1"

    async def executemany(self, sql: str, args: list[tuple[object, ...]]) -> None:
        self.many.append((" ".join(sql.split()), list(args)))

    async def fetchval(self, sql: str, *args: object) -> object:
        self.executed.append((" ".join(sql.split()), args))
        return "gid" if self._order_upsert_applied else None

    @property
    def sql(self) -> str:
        return " ".join(sql for sql, _ in self.executed)


class _FakeAcquire:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _pg(conn: _RecordingConn) -> PostgresIngestStore:
    return PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]


async def test_pg_mirror_upserts_carry_an_out_of_order_delivery_guard() -> None:
    """Shopify does not guarantee webhook ordering, and a retry can arrive after a newer
    update — without this guard a late replay silently reverts fulfillment_status/cancelled_at.
    A NULL on either side still writes (backfill/payloads without the field)."""
    conn = _RecordingConn()
    await _pg(conn).upsert_order_mirror(_order())

    assert (
        "WHERE orders.updated_at IS NULL OR EXCLUDED.updated_at IS NULL "
        "OR EXCLUDED.updated_at >= orders.updated_at"
    ) in conn.sql
    assert (
        "WHERE customers.updated_at IS NULL OR EXCLUDED.updated_at IS NULL "
        "OR EXCLUDED.updated_at >= customers.updated_at"
    ) in conn.sql


async def test_pg_stale_order_upsert_leaves_the_existing_line_items_alone() -> None:
    # The guarded upsert returned no row => this delivery is older than what is stored, so the
    # DELETE+re-INSERT of order_items must not run either (it would install stale items).
    conn = _RecordingConn(order_upsert_applied=False)
    await _pg(conn).upsert_order_mirror(_order())

    assert "DELETE FROM order_items" not in conn.sql
    assert conn.many == []


async def test_pg_line_items_are_capped_and_inserted_in_one_batch() -> None:
    # One INSERT per item, unbounded, inside a transaction holding a row lock on a 5-connection
    # pool shared with the WhatsApp reply path: a huge order could stall live replies.
    conn = _RecordingConn()
    items = tuple(
        LineItem(title=f"Item {i}", quantity=1, variant_title=None, price=None)
        for i in range(MAX_MIRROR_LINE_ITEMS + 10)
    )
    await _pg(conn).upsert_order_mirror(_order(line_items=items))

    assert len(conn.many) == 1
    sql, rows = conn.many[0]
    assert sql.startswith("INSERT INTO order_items")
    assert len(rows) == MAX_MIRROR_LINE_ITEMS


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_upsert_order_mirror_pg_dedupes_customer_and_order_and_items(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    customer_gid = f"gid://shopify/Customer/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, customer=_customer(gid=customer_gid)))
    await store.upsert_order_mirror(
        _order(
            gid=gid,
            customer=_customer(gid=customer_gid, city="Mumbai"),
            line_items=(
                LineItem(title="New Item", quantity=2, variant_title=None, price=None),
            ),
        )
    )
    async with pool.acquire() as conn:
        customer_count = await conn.fetchval(
            "SELECT count(*) FROM customers WHERE gid = $1", customer_gid
        )
        order_count = await conn.fetchval(
            "SELECT count(*) FROM orders WHERE gid = $1", gid
        )
        items = await conn.fetch(
            "SELECT title FROM order_items WHERE order_gid = $1", gid
        )
    assert customer_count == 1
    assert order_count == 1
    assert [str(r["title"]) for r in items] == ["New Item"]


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_upsert_order_mirror_pg_older_update_does_not_revert_the_row(
    pool: LazyPool,
) -> None:
    """A late-arriving RETRY of an older orders/updated must not undo a newer state.

    For a terminal order (cancelled/fulfilled, no further updates coming) the reverted value
    would otherwise stick permanently.
    """
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    newer = "2026-08-11T12:00:00+00:00"
    older = "2026-08-11T09:00:00+00:00"
    await store.upsert_order_mirror(
        _order(gid=gid, customer=None, fulfillment_status="FULFILLED", updated_at=newer)
    )
    await store.upsert_order_mirror(
        _order(gid=gid, customer=None, fulfillment_status="UNFULFILLED", updated_at=older)
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT fulfillment_status, updated_at FROM orders WHERE gid = $1", gid
        )
    assert row is not None
    assert str(row["fulfillment_status"]) == "FULFILLED"
    assert row["updated_at"] == datetime.fromisoformat(newer)


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_upsert_customer_pg_older_update_does_not_revert_the_row(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Customer/{uuid.uuid4()}"
    await store.upsert_customer(
        _customer(gid=gid, city="Pune", updated_at="2026-08-11T12:00:00+00:00")
    )
    await store.upsert_customer(
        _customer(gid=gid, city="Mumbai", updated_at="2026-08-11T09:00:00+00:00")
    )
    async with pool.acquire() as conn:
        city = await conn.fetchval("SELECT city FROM customers WHERE gid = $1", gid)
    assert str(city) == "Pune"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_upsert_order_mirror_pg_cancelled_at_round_trips(pool: LazyPool) -> None:
    # Order.cancelled_at is a raw ISO-8601 str from Shopify; the orders.cancelled_at column
    # is timestamptz, so asyncpg requires an actual datetime — this must not raise DataError.
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    cancelled_at = "2026-07-28T00:00:00+00:00"
    await store.upsert_order_mirror(_order(gid=gid, customer=None, cancelled_at=cancelled_at))
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT cancelled_at FROM orders WHERE gid = $1", gid)
    assert row is not None
    assert row["cancelled_at"] == datetime.fromisoformat(cancelled_at)
