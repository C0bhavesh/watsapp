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


async def _seed(store: PostgresIngestStore, gid: str, phone: str) -> None:
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )
    await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", _mapping(gid, phone), draft
    )


async def test_claim_and_mark_sent(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    await _seed(store, gid, phone)
    claims = await store.claim_queued_outbound(limit=50)
    mine = [c for c in claims if c.dedupe_key == f"order_created:{gid}"]
    assert len(mine) == 1
    await store.mark_outbound_sent(mine[0].id, "wamid.abc")
    async with pool.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM outbound_messages WHERE id = $1", mine[0].id
        )
        wamid = await conn.fetchval(
            "SELECT template_wamid FROM outbound_messages WHERE id = $1", mine[0].id
        )
    assert state == "sent"
    assert wamid == "wamid.abc"


async def test_bump_reaches_failed_at_cap(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    await _seed(store, gid, phone)
    claim = next(
        c for c in await store.claim_queued_outbound(limit=50)
        if c.dedupe_key == f"order_created:{gid}"
    )
    states = [await store.bump_outbound_attempt(claim.id, "500", max_attempts=3) for _ in range(3)]
    assert states == ["queued", "queued", "failed"]


async def test_set_mapping_status_reconcile_and_action(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    await _seed(store, gid, phone)
    await store.set_mapping_status(gid, "cancel_requested")
    awaiting = await store.orders_awaiting_cancel_reconcile(limit=500)
    assert gid in awaiting
    await store.record_order_action(gid, "cancel_requested", phone, "wamid.x", "ok", None)
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM order_actions WHERE order_gid = $1", gid
        )
    assert count == 1
