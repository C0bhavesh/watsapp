"""Real-DB concurrency proof for the reconcile-cancel claim (parity with
`test_outbox_drain_pg.py`'s claim tests). `orders_awaiting_cancel_reconcile` must atomically flip
each returned order out of 'cancel_requested' to a transient 'cancel_reconciling' state under
`FOR UPDATE SKIP LOCKED`, so two overlapping reconcile runs can never both claim the same order and
double-send cod_cancel. Skipped unless TEST_DATABASE_URL points at a real Postgres."""

import asyncio
import os
import uuid

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresIngestStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


def _mapping(gid: str, phone: str) -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164=phone,
        customer_name="A", email="a@b.c", language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )


async def _seed_cancel_requested(store: PostgresIngestStore, gid: str) -> None:
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )
    await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", _mapping(gid, phone), draft
    )
    await store.set_mapping_status(gid, "cancel_requested")


async def _status(pool: LazyPool, gid: str) -> str:
    async with pool.acquire() as conn:
        return str(
            await conn.fetchval("SELECT status FROM order_mappings WHERE order_gid = $1", gid)
        )


async def test_reconcile_claim_flips_to_transient_and_returns_gid(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await _seed_cancel_requested(store, gid)

    claimed = await store.orders_awaiting_cancel_reconcile(limit=50)

    assert gid in claimed
    assert await _status(pool, gid) == "cancel_reconciling"
    # A second immediate claim never re-hands the same (fresh) transient row.
    assert gid not in await store.orders_awaiting_cancel_reconcile(limit=50)


async def test_concurrent_reconcile_claims_never_return_the_same_gid(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    tag = uuid.uuid4()
    seeded: list[str] = []
    for i in range(10):
        gid = f"gid://shopify/Order/{tag}-{i}"
        await _seed_cancel_requested(store, gid)
        seeded.append(gid)

    batches = await asyncio.gather(
        *(store.orders_awaiting_cancel_reconcile(limit=5) for _ in range(4))
    )

    claimed = [gid for batch in batches for gid in batch]
    assert len(claimed) == len(set(claimed))  # no gid handed to two callers
    mine = [gid for gid in claimed if gid in set(seeded)]
    assert sorted(mine) == sorted(seeded)  # each seeded order claimed exactly once
    for gid in set(mine):
        assert await _status(pool, gid) == "cancel_reconciling"


async def test_stale_reconciling_row_reclaimed_recent_is_not(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    stale_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    recent_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await _seed_cancel_requested(store, stale_gid)
    await _seed_cancel_requested(store, recent_gid)
    # Both into the transient state, then age only one past the 10-minute reclaim threshold.
    await store.orders_awaiting_cancel_reconcile(limit=200)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE order_mappings SET updated_at = now() - interval '15 minutes'"
            " WHERE order_gid = $1",
            stale_gid,
        )

    reclaimed = await store.orders_awaiting_cancel_reconcile(limit=200)

    assert stale_gid in reclaimed  # abandoned transient row recovered
    assert recent_gid not in reclaimed  # genuinely in-flight row left alone
