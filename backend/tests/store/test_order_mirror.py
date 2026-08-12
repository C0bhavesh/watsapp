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


async def test_upsert_order_mirror_normalizes_phones_on_write() -> None:
    # The in-memory store must normalize phones the same way Postgres's `_e164` does on write, so
    # the two IngestStore impls no longer diverge (previously only Postgres normalized). An
    # unparseable value is kept verbatim (degrade, don't discard).
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(
        _order(
            phone="+91 96642 90413", shipping_phone="09664290413", billing_phone=None,
            customer=_customer(phone="9664290413"),
        )
    )
    stored = store.orders["gid://shopify/Order/1"]
    assert stored.phone == "+919664290413"
    assert stored.shipping_phone == "+919664290413"
    assert stored.billing_phone is None
    assert store.customers["gid://shopify/Customer/1"].phone == "+919664290413"


async def test_upsert_order_mirror_keeps_an_unparseable_phone_verbatim_in_memory() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order(phone="not-a-phone", customer=None))
    assert store.orders["gid://shopify/Order/1"].phone == "not-a-phone"


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


async def test_pg_order_embedded_customer_is_guarded_by_the_orders_updated_at() -> None:
    """The customer row's address is a SNAPSHOT of the ORDER's shipping address.

    Two orders/updated deliveries for the same order can carry an identical customer
    `updated_at` (the customer resource itself did not change) but different addresses, so
    guarding that write on the customer's own stamp lets an older delivery revert the address.
    The order's freshness is the right comparison for order-derived data.
    """
    conn = _RecordingConn()
    await _pg(conn).upsert_order_mirror(
        _order(
            updated_at="2026-08-11T12:00:00+00:00",
            customer=_customer(updated_at="2026-01-01T00:00:00+00:00"),
        )
    )

    customer_sql = [
        (sql, args) for sql, args in conn.executed if sql.startswith("INSERT INTO customers")
    ]
    assert len(customer_sql) == 1
    # $12 is the bound updated_at: the ORDER's stamp, not the customer's own.
    assert customer_sql[0][1][11] == datetime.fromisoformat("2026-08-11T12:00:00+00:00")


async def test_pg_standalone_customer_upsert_keeps_the_customers_own_updated_at() -> None:
    # customers/update IS a genuine customer-resource change, so that path must keep comparing
    # against the customer's own stamp.
    conn = _RecordingConn()
    await _pg(conn).upsert_customer(_customer(updated_at="2026-01-01T00:00:00+00:00"))

    sql, args = conn.executed[0]
    assert sql.startswith("INSERT INTO customers")
    assert args[11] == datetime.fromisoformat("2026-01-01T00:00:00+00:00")


async def test_pg_mirror_normalizes_phones_at_the_write_choke_point() -> None:
    """delete_by_phone matches E.164 exactly, so an unnormalized row survives erasure.

    The webhook parser normalizes; the GraphQL/backfill path stores Shopify's raw string. The
    upsert is the one place both writers pass through, so it normalizes there.
    """
    conn = _RecordingConn()
    await _pg(conn).upsert_order_mirror(
        _order(
            phone="+91 96642 90413", shipping_phone="09664290413", billing_phone=None,
            customer=_customer(phone="9664290413"),
        )
    )

    orders_sql = [args for sql, args in conn.executed if sql.startswith("INSERT INTO orders")]
    assert orders_sql[0][5] == "+919664290413"       # $6  phone
    assert orders_sql[0][6] == "+919664290413"       # $7  shipping_phone
    assert orders_sql[0][7] is None                  # $8  billing_phone stays None
    customers_sql = [
        args for sql, args in conn.executed if sql.startswith("INSERT INTO customers")
    ]
    assert customers_sql[0][4] == "+919664290413"    # $5  customer phone


async def test_pg_mirror_keeps_an_unparseable_phone_verbatim() -> None:
    # Degrade, don't crash / don't discard: an unparseable value is stored as-is rather than
    # becoming NULL, matching this codebase's phone-handling convention.
    conn = _RecordingConn()
    await _pg(conn).upsert_order_mirror(_order(phone="not-a-phone", customer=None))

    orders_sql = [args for sql, args in conn.executed if sql.startswith("INSERT INTO orders")]
    assert orders_sql[0][5] == "not-a-phone"


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


async def test_get_mirrored_order_returns_stored_order() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order())
    result = await store.get_mirrored_order("gid://shopify/Order/1")
    assert result is not None
    assert result.name == "tavas3733"


async def test_get_mirrored_order_missing_returns_none() -> None:
    store = InMemoryIngestStore()
    result = await store.get_mirrored_order("gid://shopify/Order/missing")
    assert result is None


async def test_find_mirrored_order_by_name_normalizes_bare_digits() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order(name="tavas3733"))
    result = await store.find_mirrored_order_by_name("3733")
    assert result is not None
    assert result.gid == "gid://shopify/Order/1"


async def test_find_mirrored_order_by_name_miss_returns_none() -> None:
    store = InMemoryIngestStore()
    result = await store.find_mirrored_order_by_name("tavas000000000")
    assert result is None


async def test_find_mirrored_orders_by_phone_matches_only_the_buyer_phone() -> None:
    # Q16 (docs/FR/client-decisions-all.md Part 6, ON HOLD): the chat-Q&A lookup is deliberately
    # narrowed to the buyer's own `o.phone`, so a gift recipient's shipping-contact number cannot
    # surface the buyer's order in chat. This is intentionally narrower than delete_by_phone's
    # erasure predicate (which correctly stays broad across all three columns).
    store = InMemoryIngestStore()
    phone = "+919876500000"
    await store.upsert_order_mirror(
        _order(gid="gid://buyer", phone=phone, shipping_phone=None, billing_phone=None,
               customer=None)
    )
    await store.upsert_order_mirror(
        _order(gid="gid://ship-only", phone=None, shipping_phone=phone, billing_phone=None,
               customer=None)
    )
    results = await store.find_mirrored_orders_by_phone(phone)
    assert {o.gid for o in results} == {"gid://buyer"}


async def test_find_mirrored_orders_by_phone_ignores_a_shipping_only_match() -> None:
    # Explicit negative case for Q16: an order where the number is ONLY the shipping contact
    # (not the buyer's own phone) must NOT come back from the chat-Q&A lookup.
    store = InMemoryIngestStore()
    phone = "+919876500000"
    await store.upsert_order_mirror(
        _order(gid="gid://ship-only", phone=None, shipping_phone=phone, billing_phone=None,
               customer=None)
    )
    assert await store.find_mirrored_orders_by_phone(phone) == []


async def test_find_mirrored_orders_by_phone_no_match_returns_empty() -> None:
    store = InMemoryIngestStore()
    results = await store.find_mirrored_orders_by_phone("+919000000000")
    assert results == []


async def test_find_mirrored_orders_by_phone_caps_at_ten() -> None:
    # A backfilled customer with a long order history must not return an unbounded list on the
    # 5-connection pool shared with the live reply path -- cap it at 10 (matching the Shopify
    # fallback's `first: 10`). In-memory mirrors the Postgres cap so the two do not diverge.
    store = InMemoryIngestStore()
    phone = "+919876500000"
    for i in range(15):
        await store.upsert_order_mirror(
            _order(
                gid=f"gid://{i}", name=f"tavas{i}", phone=phone,
                shipping_phone=None, billing_phone=None, customer=None,
            )
        )
    results = await store.find_mirrored_orders_by_phone(phone)
    assert len(results) == 10


def _fake_order_row(gid: str, name: str = "tavas1") -> dict[str, object]:
    """A minimal `orders LEFT JOIN customers` row (customer absent) for `_order_from_row`."""
    return {
        "gid": gid, "name": name, "email": None, "phone": None,
        "shipping_phone": None, "billing_phone": None, "financial_status": None,
        "fulfillment_status": None, "cancelled_at": None, "tags": None,
        "payment_gateway_names": None, "total_amount": None, "total_currency": None,
        "customer_locale": None, "updated_at": None, "c_gid": None,
    }


class _FakeReadConn:
    """Serves the two reads `find_mirrored_orders_by_phone` makes, recording every SQL sent."""

    def __init__(
        self, order_rows: list[dict[str, object]], item_rows: list[dict[str, object]]
    ) -> None:
        self._order_rows = order_rows
        self._item_rows = item_rows
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((" ".join(sql.split()), args))
        if "order_items" in sql:
            return self._item_rows
        return self._order_rows


async def test_find_mirrored_orders_by_phone_pg_caps_and_orders_the_query() -> None:
    conn = _FakeReadConn([_fake_order_row("gid://a")], [])
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.find_mirrored_orders_by_phone("+919876500000")

    order_sql = conn.fetch_calls[0][0]
    # Q16: the WHERE clause matches ONLY the buyer's own o.phone (no shipping/billing OR-clause);
    # NULLS LAST so genuinely-recent orders are not pushed out of the LIMIT by NULL updated_at rows
    # sorting first; o.gid DESC is a deterministic tiebreaker. (shipping_phone/billing_phone still
    # appear in the SELECT column list -- assert on the WHERE..LIMIT span, not raw substrings.)
    assert (
        "WHERE o.phone = $1 ORDER BY o.updated_at DESC NULLS LAST, o.gid DESC LIMIT 10"
    ) in order_sql


async def test_find_mirrored_orders_by_phone_pg_batch_fetches_items_without_n_plus_one() -> None:
    # One SELECT for the orders, then ONE batched SELECT for every order's items via
    # `= ANY($1)` -- not one round-trip per order (the N+1 the review flagged).
    order_rows = [_fake_order_row("gid://a"), _fake_order_row("gid://b")]
    item_rows = [
        {"order_gid": "gid://a", "title": "A-item", "sku": None, "quantity": 1,
         "variant_title": None, "price_amount": None, "price_currency": None},
        {"order_gid": "gid://b", "title": "B-item", "sku": None, "quantity": 2,
         "variant_title": None, "price_amount": "10.00", "price_currency": "INR"},
    ]
    conn = _FakeReadConn(order_rows, item_rows)
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    results = await store.find_mirrored_orders_by_phone("+919876500000")

    assert len(conn.fetch_calls) == 2  # orders + ONE batched items query, not 1 + N
    assert "= ANY($1)" in conn.fetch_calls[1][0]
    by_gid = {o.gid: o for o in results}
    # Items land on the right order -- no cross-contamination between orders.
    assert [li.title for li in by_gid["gid://a"].line_items] == ["A-item"]
    assert [li.title for li in by_gid["gid://b"].line_items] == ["B-item"]


async def test_find_mirrored_orders_by_phone_pg_skips_item_query_when_no_orders() -> None:
    conn = _FakeReadConn([], [])
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    results = await store.find_mirrored_orders_by_phone("+919000000000")

    assert results == []
    assert len(conn.fetch_calls) == 1  # no orders => no wasted batch items round-trip


async def test_find_mirrored_order_by_name_pg_rejects_invalid_name_without_querying() -> None:
    # Parity with ShopifyClient.find_order_by_name's `re.fullmatch(r"[a-z0-9]+", name)` guard: a
    # normalized name that is not a valid lookup key returns None early, never touching the pool.
    class _ExplodingPool:
        def acquire(self) -> object:
            raise AssertionError("pool must not be acquired for an invalid lookup name")

    store = PostgresIngestStore(_ExplodingPool())  # type: ignore[arg-type]
    assert await store.find_mirrored_order_by_name("bad name!") is None


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
        items = await conn.fetch("SELECT title FROM order_items WHERE order_gid = $1", gid)
    assert row is not None
    assert str(row["fulfillment_status"]) == "FULFILLED"
    assert row["updated_at"] == datetime.fromisoformat(newer)
    # The stale delivery must not have replaced the items either (the DELETE + re-INSERT is
    # skipped when the guarded upsert returns no row).
    assert [str(r["title"]) for r in items] == ["Blue Kurti"]


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_upsert_order_mirror_pg_older_delivery_does_not_revert_the_address(
    pool: LazyPool,
) -> None:
    """Same order, same UNCHANGED customer stamp, different shipping address.

    Guarding the order-embedded customer write on the customer's own updated_at would let the
    older delivery win here; guarding it on the ORDER's updated_at keeps the newer address.
    """
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    customer_gid = f"gid://shopify/Customer/{uuid.uuid4()}"
    unchanged = "2026-01-01T00:00:00+00:00"
    await store.upsert_order_mirror(
        _order(
            gid=gid, updated_at="2026-08-11T12:00:00+00:00",
            customer=_customer(gid=customer_gid, city="Pune", updated_at=unchanged),
        )
    )
    await store.upsert_order_mirror(
        _order(
            gid=gid, updated_at="2026-08-11T09:00:00+00:00",
            customer=_customer(gid=customer_gid, city="Mumbai", updated_at=unchanged),
        )
    )
    async with pool.acquire() as conn:
        city = await conn.fetchval("SELECT city FROM customers WHERE gid = $1", customer_gid)
    assert str(city) == "Pune"


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


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_get_mirrored_order_pg_returns_full_order_with_items_and_customer(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    customer_gid = f"gid://shopify/Customer/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, customer=_customer(gid=customer_gid)))

    result = await store.get_mirrored_order(gid)

    assert result is not None
    assert result.gid == gid
    assert result.name == "tavas3733"
    assert len(result.line_items) == 1
    assert result.line_items[0].title == "Blue Kurti"
    assert result.customer is not None
    assert result.customer.first_name == "Suman"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_get_mirrored_order_pg_missing_gid_returns_none(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    result = await store.get_mirrored_order(f"gid://shopify/Order/{uuid.uuid4()}")
    assert result is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_get_mirrored_order_pg_no_customer_returns_none_customer(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, customer=None))

    result = await store.get_mirrored_order(gid)

    assert result is not None
    assert result.customer is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_order_by_name_pg_normalizes_bare_digits(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, name="tavas3733", customer=None))

    result = await store.find_mirrored_order_by_name("3733")

    assert result is not None
    assert result.gid == gid


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_order_by_name_pg_miss_returns_none(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    result = await store.find_mirrored_order_by_name("tavas000000000")
    assert result is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_orders_by_phone_pg_matches_only_the_buyer_phone(
    pool: LazyPool,
) -> None:
    # Q16 (docs/FR/client-decisions-all.md Part 6, ON HOLD): narrowed to o.phone only, so a
    # shipping/billing-only contact number does not surface the buyer's order in chat.
    store = PostgresIngestStore(pool)
    phone = "+919876500000"
    gid_buyer = f"gid://shopify/Order/{uuid.uuid4()}"
    gid_ship = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(
        _order(gid=gid_buyer, phone=phone, shipping_phone=None, billing_phone=None, customer=None)
    )
    await store.upsert_order_mirror(
        _order(gid=gid_ship, phone=None, shipping_phone=phone, billing_phone=None, customer=None)
    )

    results = await store.find_mirrored_orders_by_phone(phone)

    assert {o.gid for o in results} == {gid_buyer}  # shipping-only order NOT returned


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_orders_by_phone_pg_no_match_returns_empty(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    results = await store.find_mirrored_orders_by_phone("+919000000000")
    assert results == []


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_orders_by_phone_pg_caps_at_ten_most_recent(pool: LazyPool) -> None:
    # A backfilled customer with >10 orders (the fast-path order_mappings table is empty for
    # backfilled history) must return only the 10 most-recently-updated, ordered DESC.
    store = PostgresIngestStore(pool)
    phone = "+919222200000"
    for i in range(12):
        await store.upsert_order_mirror(
            _order(
                gid=f"gid://shopify/Order/{uuid.uuid4()}", phone=phone,
                shipping_phone=None, billing_phone=None, customer=None,
                updated_at=f"2026-08-{i + 1:02d}T00:00:00+00:00",
            )
        )

    results = await store.find_mirrored_orders_by_phone(phone)

    assert len(results) == 10
    returned = [o.updated_at for o in results]
    assert returned == sorted(returned, reverse=True)  # newest first
