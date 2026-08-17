"""Gated real-Postgres proof of the new chat-aggregation read methods (Task 1). Skips cleanly
without TEST_DATABASE_URL (confirmed UNSET in this environment; never point it at production)."""

import os
import uuid

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresConversationStore, PostgresIngestStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


async def test_find_messages_by_user_id_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"919664290413-{uuid.uuid4()}"
    conv_id = await store.get_or_create(user_id)
    await store.append_message(conv_id, "user", "hi")

    messages = await store.find_messages_by_user_id(user_id, limit=10)

    assert [m.content for m in messages] == ["hi"]


async def test_find_messages_by_user_id_unknown_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)

    messages = await store.find_messages_by_user_id(f"no-such-user-{uuid.uuid4()}", limit=10)

    assert messages == []


async def test_get_user_id_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)

    assert await store.get_user_id(conv_id) == user_id
    assert await store.get_user_id(-1) is None


async def test_find_order_actions_by_wa_ids_dual_key_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    raw_wa_id = f"91{uuid.uuid4().int % 10**10:010d}"  # written RAW, no leading +
    await store.record_order_action(gid, "confirm", raw_wa_id, "wamid.x", "ok", None)

    # The read must find it via the normalized (+-prefixed) candidate too.
    rows = await store.find_order_actions_by_wa_ids([f"+{raw_wa_id}", raw_wa_id], limit=10)

    assert any(r.order_gid == gid for r in rows)


async def test_find_outbound_by_phone_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavaspg1", order_number_int=1,
        phone_e164=phone, customer_name="A", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json='{"template": "cod_confirmation"}',
    )
    await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", mapping, draft)

    rows = await store.find_outbound_by_phone(phone, limit=10)

    assert len(rows) == 1
    assert rows[0].payload_json == '{"template": "cod_confirmation"}'
