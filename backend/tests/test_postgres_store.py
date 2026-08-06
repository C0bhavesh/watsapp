import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresConfigRepo, PostgresIngestStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


def mapping(gid: str) -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1,
        phone_e164="+911111111111", customer_name="A", email="a@b.c",
        language="en", financial_status_at_create="PENDING", is_cod=True,
    )


async def test_config_repo_roundtrip_upsert(pool: LazyPool) -> None:
    repo = PostgresConfigRepo(pool)
    key = f"test:{uuid.uuid4()}"
    assert await repo.get(key) is None
    await repo.set(key, "v1")
    await repo.set(key, "v2")
    assert await repo.get(key) == "v2"


async def test_ingest_atomic_and_idempotent(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    wh = f"wh-{uuid.uuid4()}"
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164="+911111111111", payload_json="{}",
    )
    first = await store.ingest_order_created(wh, "orders/create", mapping(gid), draft)
    assert (first.duplicate, first.queued) == (False, True)
    again = await store.ingest_order_created(wh, "orders/create", mapping(gid), draft)
    assert (again.duplicate, again.queued) == (True, False)
    other = await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", mapping(gid), draft
    )
    assert (other.duplicate, other.queued) == (False, False)  # dedupe_key already used


async def test_delete_by_phone_purges_that_number(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    keep_phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    keep_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    m = MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164=phone,
        customer_name="A", email="a@b.c", language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    keep = MappingUpsert(
        order_gid=keep_gid, order_name="tavas2", order_number_int=2, phone_e164=keep_phone,
        customer_name="B", email="b@b.c", language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )
    await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", m, draft)
    await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", keep, None)

    result = await store.delete_by_phone(phone)

    assert result.order_mappings == 1
    assert result.outbound_messages == 1
    async with pool.acquire() as conn:
        gone = await conn.fetchval("SELECT 1 FROM order_mappings WHERE order_gid = $1", gid)
        kept = await conn.fetchval("SELECT 1 FROM order_mappings WHERE order_gid = $1", keep_gid)
    assert gone is None and kept == 1


async def test_purge_older_than_deletes_only_aged_rows(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    old_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    new_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    for gid in (old_gid, new_gid):
        m = MappingUpsert(
            order_gid=gid, order_name="tavas1", order_number_int=1,
            phone_e164=f"+91{uuid.uuid4().int % 10**10:010d}", customer_name="A",
            email="a@b.c", language="en", financial_status_at_create="PENDING", is_cod=True,
        )
        await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", m, None)
    # Backdate one row well past the cutoff.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE order_mappings SET created_at = $1 WHERE order_gid = $2",
            datetime.now(UTC) - timedelta(days=400), old_gid,
        )

    result = await store.purge_older_than(datetime.now(UTC) - timedelta(days=365))

    assert result.order_mappings >= 1
    async with pool.acquire() as conn:
        gone = await conn.fetchval("SELECT 1 FROM order_mappings WHERE order_gid = $1", old_gid)
        kept = await conn.fetchval("SELECT 1 FROM order_mappings WHERE order_gid = $1", new_gid)
    assert gone is None and kept == 1
