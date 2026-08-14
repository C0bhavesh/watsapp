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


_CEILING = 24 * 60  # 24h, matching the reminder job's ceiling


async def _age_updated(pool: LazyPool, gid: str, minutes: int) -> None:
    # Age off updated_at -- the moment the mapping became template_sent (what the sweep filters on),
    # NOT created_at. set_mapping_status already stamped updated_at = now(); this backdates it.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE order_mappings SET updated_at = now() - make_interval(mins => $2)"
            " WHERE order_gid = $1",
            gid,
            minutes,
        )


async def _backdate_created_only(pool: LazyPool, gid: str, minutes: int) -> None:
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
    in_window = f"gid://shopify/Order/{uuid.uuid4()}"     # ~2h past template_sent -> stale
    recent_ts = f"gid://shopify/Order/{uuid.uuid4()}"     # just sent -> too recent, not stale
    too_old = f"gid://shopify/Order/{uuid.uuid4()}"       # 30h past -> ceiling excludes
    old_confirmed = f"gid://shopify/Order/{uuid.uuid4()}"  # old but tapped -> excluded
    for gid in (in_window, recent_ts, too_old, old_confirmed):
        await _seed(store, gid, phone, payload)
    await store.set_mapping_status(in_window, "template_sent")
    await store.set_mapping_status(recent_ts, "template_sent")
    await store.set_mapping_status(too_old, "template_sent")
    await store.set_mapping_status(old_confirmed, "confirmed")
    await _age_updated(pool, in_window, minutes=120)
    await _age_updated(pool, too_old, minutes=30 * 60)
    await _age_updated(pool, old_confirmed, minutes=120)
    # recent_ts keeps its now() updated_at -> newer than the 60-minute cutoff.

    stale_gids = {
        m.order_gid
        for m in await store.find_stale_template_sent(
            older_than_minutes=60, max_age_minutes=_CEILING
        )
    }

    assert in_window in stale_gids
    assert recent_ts not in stale_gids
    assert too_old not in stale_gids  # HIGH #2: ceiling excludes the historically-unanswered order
    assert old_confirmed not in stale_gids


async def test_find_stale_anchors_on_template_sent_not_created_at(pool: LazyPool) -> None:
    # HIGH #1: created 10h ago but template only just SENT (updated_at = now) -> NOT stale yet.
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await _seed(store, gid, phone, {"template": "order_confirmation_cod"})
    await _backdate_created_only(pool, gid, minutes=600)
    await store.set_mapping_status(gid, "template_sent")  # updated_at = now

    stale_gids = {
        m.order_gid
        for m in await store.find_stale_template_sent(
            older_than_minutes=60, max_age_minutes=_CEILING
        )
    }

    assert gid not in stale_gids


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


async def test_find_outbound_by_dedupe_keys_batches(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    a = f"gid://shopify/Order/{uuid.uuid4()}"
    b = f"gid://shopify/Order/{uuid.uuid4()}"
    await _seed(store, a, phone, {"template": "order_confirmation_cod"})
    await _seed(store, b, phone, {"template": "order_confirmation_cod"})

    found = await store.find_outbound_by_dedupe_keys(
        [f"order_created:{a}", f"order_created:{b}", f"order_created:{uuid.uuid4()}"]
    )

    assert set(found) == {f"order_created:{a}", f"order_created:{b}"}
    assert await store.find_outbound_by_dedupe_keys([]) == {}


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
