import os

import pytest

from app.store.memory import InMemoryExchangeStore
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresExchangeStore

DSN = os.environ.get("TEST_DATABASE_URL", "")


async def test_memory_create_returns_a_requested_row() -> None:
    store = InMemoryExchangeStore()
    row = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    assert row.order_gid == "gid://o/1"
    assert row.order_name == "tavas1"
    assert row.phone_e164 == "+919999999999"
    assert row.requested_size == "M"
    assert row.status == "requested"
    assert row.return_tracking_url is None


async def test_memory_list_for_phone_returns_only_that_phones_requests() -> None:
    store = InMemoryExchangeStore()
    await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.create("gid://o/2", "tavas2", "+918888888888", "L")
    result = await store.list_for_phone("+919999999999")
    assert len(result) == 1
    assert result[0].order_gid == "gid://o/1"


async def test_memory_get_returns_none_for_unknown_id() -> None:
    store = InMemoryExchangeStore()
    assert await store.get(999) is None


async def test_memory_set_status_updates_the_row() -> None:
    store = InMemoryExchangeStore()
    created = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.set_status(created.id, "return_picked_up")
    updated = await store.get(created.id)
    assert updated is not None
    assert updated.status == "return_picked_up"


async def test_memory_set_return_tracking_url_updates_the_row() -> None:
    store = InMemoryExchangeStore()
    created = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.set_return_tracking_url(created.id, "https://track/abc")
    updated = await store.get(created.id)
    assert updated is not None
    assert updated.return_tracking_url == "https://track/abc"


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_create_and_get_round_trip(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    created = await store.create("gid://o/pg1", "tavas9001", "+919000000001", "S")
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.order_gid == "gid://o/pg1"
    assert fetched.status == "requested"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_list_for_phone_round_trips(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    await store.create("gid://o/pg2", "tavas9002", "+919000000002", "L")
    result = await store.list_for_phone("+919000000002")
    assert len(result) == 1
    assert result[0].order_name == "tavas9002"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_set_status_and_return_tracking_url_round_trip(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    created = await store.create("gid://o/pg3", "tavas9003", "+919000000003", "M")
    await store.set_status(created.id, "qc_passed")
    await store.set_return_tracking_url(created.id, "https://track/pg3")
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.status == "qc_passed"
    assert fetched.return_tracking_url == "https://track/pg3"
