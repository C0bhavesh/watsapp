"""Gated real-Postgres proof of the new chat-aggregation read methods (Task 1). Skips cleanly
without TEST_DATABASE_URL (confirmed UNSET in this environment; never point it at production)."""

import os
import uuid

import pytest

from app.channels.shopify_orders import order_from_webhook_payload
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


# --- ConversationStore wamid + delivery-status tracking (Task 2) ---

async def test_append_message_returns_id_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    conv_id = await store.get_or_create(f"+91{uuid.uuid4().int % 10**10:010d}")
    msg_id = await store.append_message(conv_id, "assistant", "hello")
    assert isinstance(msg_id, int)


async def test_set_wamid_then_apply_delivery_status_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    msg_id = await store.append_message(conv_id, "assistant", "your order shipped")
    wamid = f"wamid.{uuid.uuid4()}"
    await store.set_message_wamid(msg_id, wamid)

    applied = await store.apply_message_delivery_status(wamid, "delivered")

    assert applied == "applied"
    messages = await store.find_messages_by_user_id(user_id)
    assert messages[-1].delivery_status == "delivered"


async def test_apply_delivery_status_unknown_wamid_returns_not_found_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    applied = await store.apply_message_delivery_status(f"wamid.{uuid.uuid4()}", "delivered")
    assert applied == "not_found"


async def test_apply_delivery_status_respects_ordering_guard_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    msg_id = await store.append_message(conv_id, "assistant", "hi")
    wamid = f"wamid.{uuid.uuid4()}"
    await store.set_message_wamid(msg_id, wamid)
    await store.apply_message_delivery_status(wamid, "read")

    # found but not applied: the row IS in this table but the guard blocks the read->delivered
    # regression, so it returns "unchanged".
    regressed = await store.apply_message_delivery_status(wamid, "delivered")

    assert regressed == "unchanged"
    messages = await store.find_messages_by_user_id(user_id)
    assert messages[-1].delivery_status == "read"


async def test_apply_message_delivery_status_duplicate_failed_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    msg_id = await store.append_message(conv_id, "assistant", "hi")
    wamid = f"wamid.{uuid.uuid4()}"
    await store.set_message_wamid(msg_id, wamid)

    first = await store.apply_message_delivery_status(wamid, "failed")
    second = await store.apply_message_delivery_status(wamid, "failed")

    assert first == "applied"
    assert second == "unchanged"


# --- ConversationStore message retry-info + retry-record (Task 3) ---

async def test_get_message_retry_info_returns_current_state_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    msg_id = await store.append_message(conv_id, "assistant", "your order shipped")
    wamid = f"wamid.{uuid.uuid4()}"
    await store.set_message_wamid(msg_id, wamid)

    info = await store.get_message_retry_info(wamid)

    assert info is not None
    assert info.id == msg_id
    assert info.conversation_id == conv_id
    assert info.content == "your order shipped"
    assert info.retry_count == 0


async def test_get_message_retry_info_unknown_wamid_returns_none_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    info = await store.get_message_retry_info(f"wamid.{uuid.uuid4()}")
    assert info is None


async def test_record_message_retry_with_new_wamid_resets_delivery_status_pg(
    pool: LazyPool,
) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    msg_id = await store.append_message(conv_id, "assistant", "your order shipped")
    wamid = f"wamid.{uuid.uuid4()}"
    await store.set_message_wamid(msg_id, wamid)
    await store.apply_message_delivery_status(wamid, "failed")

    new_wamid = f"wamid.{uuid.uuid4()}"
    await store.record_message_retry(msg_id, new_wamid)

    info = await store.get_message_retry_info(new_wamid)
    assert info is not None
    assert info.retry_count == 1
    assert await store.get_message_retry_info(wamid) is None
    messages = await store.find_messages_by_user_id(user_id)
    assert messages[-1].delivery_status is None


async def test_record_message_retry_with_no_new_wamid_increments_count_only_pg(
    pool: LazyPool,
) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    msg_id = await store.append_message(conv_id, "assistant", "your order shipped")
    wamid = f"wamid.{uuid.uuid4()}"
    await store.set_message_wamid(msg_id, wamid)
    await store.apply_message_delivery_status(wamid, "failed")

    await store.record_message_retry(msg_id, None)

    info = await store.get_message_retry_info(wamid)
    assert info is not None
    assert info.retry_count == 1
    messages = await store.find_messages_by_user_id(user_id)
    assert messages[-1].delivery_status == "failed"


async def test_get_user_id_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)

    assert await store.get_user_id(conv_id) == user_id
    assert await store.get_user_id(-1) is None


async def _last_active_pg(store: PostgresConversationStore, user_id: str) -> str | None:
    for s in await store.recent_conversations(limit=1000):
        if s.user_id == user_id:
            return s.last_active_at
    return None


async def test_get_or_create_no_bump_then_touch_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    await store.append_message(conv_id, "user", "hi")  # qualify for recent_conversations
    first = await _last_active_pg(store, user_id)

    # Second get_or_create is a display-only lookup -- must NOT bump last_active_at.
    await store.get_or_create(user_id)
    assert await _last_active_pg(store, user_id) == first

    # touch() is genuine activity -- it MUST bump last_active_at.
    await store.touch(user_id)
    assert await _last_active_pg(store, user_id) != first


async def test_touch_missing_conversation_is_noop_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    # Touching a non-existent user must not raise or create a row.
    await store.touch(f"no-such-{uuid.uuid4()}")


async def test_recent_conversations_excludes_message_less_conversation_pg(
    pool: LazyPool,
) -> None:
    # The bug: a conversation row created via get_or_create for a customer who has only ever
    # received OUTBOUND messages (no inbound chat -> no rows in `messages`) picks up the column's
    # creation-time DEFAULT now() and used to surface in recent_conversations as if it were genuine
    # recent activity, permanently outranking real recency from other sources.
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    await store.get_or_create(user_id)  # no append_message call -- mirrors the real bug

    assert await _last_active_pg(store, user_id) is None


async def test_recent_conversations_includes_conversation_with_messages_pg(
    pool: LazyPool,
) -> None:
    # Regression guard: the message-less filter must not over-exclude a genuine chat thread.
    store = PostgresConversationStore(pool)
    user_id = f"+91{uuid.uuid4().int % 10**10:010d}"
    conv_id = await store.get_or_create(user_id)
    await store.append_message(conv_id, "user", "where is my order")

    assert await _last_active_pg(store, user_id) is not None


async def test_find_order_actions_by_wa_ids_dual_key_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    raw_wa_id = f"91{uuid.uuid4().int % 10**10:010d}"  # written RAW, no leading +
    await store.record_order_action(gid, "confirm", raw_wa_id, "wamid.x", "ok", None)

    # The read must find it via the normalized (+-prefixed) candidate too.
    rows = await store.find_order_actions_by_wa_ids([f"+{raw_wa_id}", raw_wa_id], limit=10)

    assert any(r.order_gid == gid for r in rows)


async def test_distinct_outbound_phones_recency_pg(pool: LazyPool) -> None:
    # Two phones enqueued oldest-first; the newer one must lead (MAX(created_at) DESC), and each
    # returned pair carries a real ISO recency stamp -- not the identifier-string ordering.
    store = PostgresIngestStore(pool)
    older = f"+91{uuid.uuid4().int % 10**10:010d}"
    newer = f"+91{uuid.uuid4().int % 10**10:010d}"
    for phone in (older, newer):
        gid = f"gid://shopify/Order/{uuid.uuid4()}"
        mapping = MappingUpsert(
            order_gid=gid, order_name="tavaspg", order_number_int=1,
            phone_e164=phone, customer_name="A", email=None, language="en",
            financial_status_at_create="PENDING", is_cod=True,
        )
        draft = OutboundDraft(
            dedupe_key=f"order_created:{gid}", kind="order_confirmation",
            phone_e164=phone, payload_json="{}",
        )
        await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", mapping, draft)

    rows = await store.distinct_outbound_phones(limit=100)
    seen = [(p, ts) for p, ts in rows if p in (older, newer)]
    ranks = {p: i for i, (p, _) in enumerate(seen)}
    assert ranks[newer] < ranks[older]
    assert all(ts is not None for _, ts in seen)


async def test_distinct_order_action_wa_ids_recency_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    older = f"91{uuid.uuid4().int % 10**10:010d}"
    newer = f"91{uuid.uuid4().int % 10**10:010d}"
    await store.record_order_action(f"gid://o/{uuid.uuid4()}", "confirm", older, "m1", "ok", None)
    await store.record_order_action(f"gid://o/{uuid.uuid4()}", "confirm", newer, "m2", "ok", None)

    rows = await store.distinct_order_action_wa_ids(limit=100)
    seen = [(w, ts) for w, ts in rows if w in (older, newer)]
    ranks = {w: i for i, (w, _) in enumerate(seen)}
    assert ranks[newer] < ranks[older]
    assert all(ts is not None for _, ts in seen)


async def _send_outbound_pg(
    store: PostgresIngestStore, phone: str, wamid: str
) -> None:
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavaspg2", order_number_int=1,
        phone_e164=phone, customer_name="A", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json='{"template": "cod_confirmation"}',
    )
    result = await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", mapping, draft)
    assert result.outbound_id is not None
    await store.mark_outbound_sent(result.outbound_id, wamid)


async def test_apply_outbound_delivery_status_updates_by_wamid_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    wamid = f"wamid.{uuid.uuid4()}"
    await _send_outbound_pg(store, phone, wamid)

    applied = await store.apply_outbound_delivery_status(wamid, "delivered")

    assert applied == "applied"
    entries = await store.find_outbound_by_phone(phone)
    assert entries[-1].delivery_status == "delivered"


async def test_apply_outbound_delivery_status_unknown_wamid_returns_not_found_pg(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    applied = await store.apply_outbound_delivery_status(f"wamid.{uuid.uuid4()}", "delivered")
    assert applied == "not_found"


async def test_apply_outbound_delivery_status_respects_ordering_guard_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    wamid = f"wamid.{uuid.uuid4()}"
    await _send_outbound_pg(store, phone, wamid)
    await store.apply_outbound_delivery_status(wamid, "read")

    # found but not applied: the row IS in this table but the guard blocks the read->delivered
    # regression, so it returns "unchanged".
    regressed = await store.apply_outbound_delivery_status(wamid, "delivered")

    assert regressed == "unchanged"
    entries = await store.find_outbound_by_phone(phone)
    assert entries[-1].delivery_status == "read"


async def test_apply_outbound_delivery_status_duplicate_failed_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    wamid = f"wamid.{uuid.uuid4()}"
    await _send_outbound_pg(store, phone, wamid)

    first = await store.apply_outbound_delivery_status(wamid, "failed")
    second = await store.apply_outbound_delivery_status(wamid, "failed")

    assert first == "applied"
    assert second == "unchanged"


# --- IngestStore outbound retry-info + retry-record (Task 2) ---

async def test_get_outbound_retry_info_returns_current_state_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    wamid = f"wamid.{uuid.uuid4()}"
    await _send_outbound_pg(store, phone, wamid)

    info = await store.get_outbound_retry_info(wamid)

    assert info is not None
    assert info.retry_count == 0
    assert info.phone_e164 == phone
    assert info.payload_json == '{"template": "cod_confirmation"}'


async def test_get_outbound_retry_info_unknown_wamid_returns_none_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    info = await store.get_outbound_retry_info(f"wamid.{uuid.uuid4()}")
    assert info is None


async def test_record_outbound_retry_with_new_wamid_resets_delivery_status_pg(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    wamid = f"wamid.{uuid.uuid4()}"
    await _send_outbound_pg(store, phone, wamid)
    await store.apply_outbound_delivery_status(wamid, "failed")
    seeded = await store.get_outbound_retry_info(wamid)
    assert seeded is not None

    new_wamid = f"wamid.{uuid.uuid4()}"
    await store.record_outbound_retry(seeded.id, new_wamid)

    info = await store.get_outbound_retry_info(new_wamid)
    assert info is not None
    assert info.retry_count == 1
    assert await store.get_outbound_retry_info(wamid) is None
    entries = await store.find_outbound_by_phone(phone)
    assert entries[-1].delivery_status is None


async def test_record_outbound_retry_with_no_new_wamid_increments_count_only_pg(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    wamid = f"wamid.{uuid.uuid4()}"
    await _send_outbound_pg(store, phone, wamid)
    await store.apply_outbound_delivery_status(wamid, "failed")
    seeded = await store.get_outbound_retry_info(wamid)
    assert seeded is not None

    await store.record_outbound_retry(seeded.id, None)

    info = await store.get_outbound_retry_info(wamid)
    assert info is not None
    assert info.retry_count == 1
    entries = await store.find_outbound_by_phone(phone)
    assert entries[-1].delivery_status == "failed"


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


# --- Keyset pagination (`before`) + server-side search (`phone_like` / search_order_phones) ---


async def test_distinct_outbound_phones_before_and_phone_like_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    marker = uuid.uuid4().int % 10**6
    old = f"+9188{marker:06d}00"
    new = f"+9188{marker:06d}99"
    for phone in (old, new):
        gid = f"gid://shopify/Order/{uuid.uuid4()}"
        mapping = MappingUpsert(
            order_gid=gid, order_name="tavaspg", order_number_int=1,
            phone_e164=phone, customer_name="A", email=None, language="en",
            financial_status_at_create="PENDING", is_cod=True,
        )
        draft = OutboundDraft(
            dedupe_key=f"order_created:{gid}", kind="order_confirmation",
            phone_e164=phone, payload_json="{}",
        )
        await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", mapping, draft)

    # phone_like narrows to just this pair's shared substring.
    hits = await store.distinct_outbound_phones(limit=100, phone_like=f"88{marker:06d}")
    assert {p for p, _ in hits} == {old, new}

    # before = the newer row's stamp -> only the strictly-older row returns.
    all_pairs = {p: ts for p, ts in hits}
    newest = max(all_pairs.values())  # type: ignore[type-var]
    older_page = await store.distinct_outbound_phones(
        limit=100, before=newest, phone_like=f"88{marker:06d}"
    )
    returned = {p for p, _ in older_page}
    assert newest not in [all_pairs[p] for p in returned]


async def test_recent_conversations_phone_like_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    marker = uuid.uuid4().hex[:8]
    user_id = f"+9199{marker}"
    conv_id = await store.get_or_create(user_id)
    await store.append_message(conv_id, "user", "hi")

    hits = await store.recent_conversations(limit=100, phone_like=marker)

    assert user_id in [s.user_id for s in hits]


async def test_search_order_phones_matches_name_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    order_name = f"tavas{uuid.uuid4().int % 10**7}"
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": gid,
        "name": order_name,
        "phone": phone,
        "updated_at": "2026-08-15T10:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": gid.replace("Order", "Customer"),
            "first_name": "Amita", "last_name": "Chadha",
        },
    })
    assert order is not None
    if order.customer is not None:
        await store.upsert_customer(order.customer)
    await store.upsert_order_mirror(order)

    by_name = await store.search_order_phones(order_name, limit=10)
    assert phone in [p for p, _ in by_name]

    by_customer = await store.search_order_phones("amita", limit=100)
    assert phone in [p for p, _ in by_customer]


# --- IngestStore.save_inbound_image / find_inbound_images_by_phone / get_inbound_image ---


async def test_save_and_find_inbound_images_by_phone_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    image_id = await store.save_inbound_image(phone, f"wamid.{uuid.uuid4()}", "image/jpeg", b"data")

    entries = await store.find_inbound_images_by_phone(phone)

    assert len(entries) == 1
    assert entries[0].id == image_id
    assert entries[0].mime_type == "image/jpeg"


async def test_get_inbound_image_returns_bytes_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    image_id = await store.save_inbound_image(
        phone, f"wamid.{uuid.uuid4()}", "image/png", b"\x89PNGfakepng"
    )

    stored = await store.get_inbound_image(image_id)

    assert stored is not None
    assert stored.phone_e164 == phone
    assert stored.mime_type == "image/png"
    assert stored.bytes == b"\x89PNGfakepng"
