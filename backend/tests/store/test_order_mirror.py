import os
import uuid
from datetime import datetime

import pytest

from app.shopify.models import Customer, LineItem, Money, Order
from app.store.memory import InMemoryIngestStore
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresIngestStore

DSN = os.environ.get("TEST_DATABASE_URL", "")


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
