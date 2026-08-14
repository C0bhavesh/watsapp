"""Gated real-Postgres proof of the Q17 reminder-sweep store primitives. Skips cleanly without
TEST_DATABASE_URL (confirmed UNSET in this environment; never point it at production)."""

import json
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
        customer_name="A", email="a@b.c", language="hi",
        financial_status_at_create="PENDING", is_cod=True,
    )


async def _seed(store: PostgresIngestStore, gid: str, phone: str, payload: dict[str, str]) -> None:
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json=json.dumps(payload),
    )
    await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", _mapping(gid, phone), draft
    )


async def _backdate_created(pool: LazyPool, gid: str, minutes: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE order_mappings SET created_at = now() - make_interval(mins => $2)"
            " WHERE order_gid = $1",
            gid,
            minutes,
        )


async def test_find_stale_template_sent_age_and_status_filter(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    payload = {"template": "order_confirmation_cod", "language": "hi"}
    old_ts = f"gid://shopify/Order/{uuid.uuid4()}"       # old + template_sent -> stale
    recent_ts = f"gid://shopify/Order/{uuid.uuid4()}"    # old age but recent -> not stale
    old_confirmed = f"gid://shopify/Order/{uuid.uuid4()}"  # old but tapped -> excluded
    for gid in (old_ts, recent_ts, old_confirmed):
        await _seed(store, gid, phone, payload)
    await store.set_mapping_status(old_ts, "template_sent")
    await store.set_mapping_status(recent_ts, "template_sent")
    await store.set_mapping_status(old_confirmed, "confirmed")
    await _backdate_created(pool, old_ts, minutes=90)
    await _backdate_created(pool, old_confirmed, minutes=90)
    # recent_ts keeps its now() created_at -> newer than the 60-minute cutoff.

    stale_gids = {m.order_gid for m in await store.find_stale_template_sent(older_than_minutes=60)}

    assert old_ts in stale_gids
    assert recent_ts not in stale_gids
    assert old_confirmed not in stale_gids


async def test_find_outbound_by_dedupe_key_roundtrips_payload(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    payload = {"template": "order_confirmation_cod", "customer_name": "Suman", "amount": "949"}
    await _seed(store, gid, phone, payload)

    original = await store.find_outbound_by_dedupe_key(f"order_created:{gid}")

    assert original is not None
    assert json.loads(original.payload_json) == payload
    assert original.phone_e164 == phone
    assert original.kind == "order_confirmation"
    assert await store.find_outbound_by_dedupe_key(f"order_created:{uuid.uuid4()}") is None


async def test_enqueue_outbound_on_conflict_do_nothing(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    reminder = OutboundDraft(
        dedupe_key=f"order_reminder:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )
    assert await store.enqueue_outbound(reminder) is True
    assert await store.enqueue_outbound(reminder) is False  # UNIQUE dedupe_key = exactly-once
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM outbound_messages WHERE dedupe_key = $1", reminder.dedupe_key
        )
    assert count == 1
