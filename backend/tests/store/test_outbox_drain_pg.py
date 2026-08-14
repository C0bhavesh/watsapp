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


async def _seed(store: PostgresIngestStore, gid: str, phone: str) -> None:
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )
    await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", _mapping(gid, phone), draft
    )


async def _state(pool: LazyPool, id_: int) -> str:
    async with pool.acquire() as conn:
        return str(await conn.fetchval("SELECT state FROM outbound_messages WHERE id = $1", id_))


async def test_claim_transitions_to_processing_then_mark_sent(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    await _seed(store, gid, phone)
    claims = await store.claim_queued_outbound(limit=50)
    mine = [c for c in claims if c.dedupe_key == f"order_created:{gid}"]
    assert len(mine) == 1
    # The claim atomically flipped the row out of 'queued'.
    assert await _state(pool, mine[0].id) == "processing"
    await store.mark_outbound_sent(mine[0].id, "wamid.abc")
    async with pool.acquire() as conn:
        wamid = await conn.fetchval(
            "SELECT template_wamid FROM outbound_messages WHERE id = $1", mine[0].id
        )
    assert await _state(pool, mine[0].id) == "sent"
    assert wamid == "wamid.abc"


async def test_concurrent_claims_never_return_the_same_row(pool: LazyPool) -> None:
    # The real duplicate-send guard: two overlapping cron runs claiming at the same time must
    # partition the queued rows, never both receive one. FOR UPDATE SKIP LOCKED + the atomic
    # 'queued' -> 'processing' flip make every claimed id unique across concurrent callers.
    store = PostgresIngestStore(pool)
    tag = uuid.uuid4()
    seeded: list[str] = []
    for i in range(10):
        gid = f"gid://shopify/Order/{tag}-{i}"
        phone = f"+91{uuid.uuid4().int % 10**10:010d}"
        await _seed(store, gid, phone)
        seeded.append(f"order_created:{gid}")

    batches = await asyncio.gather(*(store.claim_queued_outbound(limit=5) for _ in range(4)))

    claimed_ids = [c.id for batch in batches for c in batch]
    # No id was handed to two callers.
    assert len(claimed_ids) == len(set(claimed_ids))
    # Every one of our seeded rows was claimed exactly once and is now 'processing'.
    mine = [c for batch in batches for c in batch if c.dedupe_key in set(seeded)]
    assert sorted(c.dedupe_key for c in mine) == sorted(seeded)
    for c in mine:
        assert await _state(pool, c.id) == "processing"


async def test_claim_skips_non_queued_states(pool: LazyPool) -> None:
    # A mixed-state table: only a 'queued' row is claimed; sent/failed/undeliverable/processing
    # rows are all skipped by the WHERE state='queued' predicate -> never re-sent.
    store = PostgresIngestStore(pool)
    tag = uuid.uuid4()
    ids: dict[str, int] = {}
    for state in ("sent", "failed", "undeliverable", "processing", "queued"):
        gid = f"gid://shopify/Order/{tag}-{state}"
        phone = f"+91{uuid.uuid4().int % 10**10:010d}"
        await _seed(store, gid, phone)
        async with pool.acquire() as conn:
            ids[state] = int(
                await conn.fetchval(
                    "UPDATE outbound_messages SET state = $2 WHERE dedupe_key = $1 RETURNING id",
                    f"order_created:{gid}",
                    state,
                )
            )

    claims = await store.claim_queued_outbound(limit=50)

    claimed_ids = {c.id for c in claims}
    assert ids["queued"] in claimed_ids
    for terminal in ("sent", "failed", "undeliverable", "processing"):
        assert ids[terminal] not in claimed_ids


async def test_stale_processing_row_reclaimed_recent_is_not(pool: LazyPool) -> None:
    # The stranded-row guard: a 'processing' row abandoned by a killed invocation (updated_at
    # older than the 10-minute threshold) IS reclaimed by a later claim, while a 'processing'
    # row with a recent updated_at (still plausibly in-flight) is NOT — so a genuinely-in-flight
    # send is never double-sent, but a permanently-stranded one is recovered.
    store = PostgresIngestStore(pool)
    stale_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    recent_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    for gid in (stale_gid, recent_gid):
        phone = f"+91{uuid.uuid4().int % 10**10:010d}"
        await _seed(store, gid, phone)
    async with pool.acquire() as conn:
        stale_id = int(
            await conn.fetchval(
                "UPDATE outbound_messages SET state = 'processing',"
                " updated_at = now() - interval '15 minutes' WHERE dedupe_key = $1 RETURNING id",
                f"order_created:{stale_gid}",
            )
        )
        recent_id = int(
            await conn.fetchval(
                "UPDATE outbound_messages SET state = 'processing', updated_at = now()"
                " WHERE dedupe_key = $1 RETURNING id",
                f"order_created:{recent_gid}",
            )
        )

    claimed_ids = {c.id for c in await store.claim_queued_outbound(limit=500)}

    assert stale_id in claimed_ids  # abandoned in-flight row recovered
    assert recent_id not in claimed_ids  # genuinely in-flight row left alone
    # The reclaim re-stamps updated_at, so the recovered row is 'processing' again (fresh).
    assert await _state(pool, stale_id) == "processing"


async def test_ingest_returns_outbound_id_and_claim_by_id_is_atomic(pool: LazyPool) -> None:
    # The inline send path: ingest_order_created returns the freshly-queued row's id, and
    # claim_outbound_by_id atomically flips ONLY that row 'queued' -> 'processing'. A second claim
    # of the same id returns None (a racing backstop drain can never double-send it), and an
    # UNRELATED queued row is left untouched (the inline path never touches the generic queue).
    store = PostgresIngestStore(pool)
    mine_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    other_gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    other_phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    draft = OutboundDraft(
        dedupe_key=f"order_created:{mine_gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )
    result = await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", _mapping(mine_gid, phone), draft
    )
    assert result.outbound_id is not None
    await _seed(store, other_gid, other_phone)  # unrelated queued row

    claim = await store.claim_outbound_by_id(result.outbound_id)

    assert claim is not None
    assert claim.dedupe_key == f"order_created:{mine_gid}"
    assert await _state(pool, result.outbound_id) == "processing"
    # A second claim of the same row -> None (already out of 'queued').
    assert await store.claim_outbound_by_id(result.outbound_id) is None
    # The unrelated queued row was never touched.
    async with pool.acquire() as conn:
        other_state = await conn.fetchval(
            "SELECT state FROM outbound_messages WHERE dedupe_key = $1",
            f"order_created:{other_gid}",
        )
    assert other_state == "queued"


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
