"""New read-only chat-aggregation methods: ConversationStore + IngestStore additions."""

from app.store.base import MappingUpsert, OutboundDraft
from app.store.memory import InMemoryConversationStore, InMemoryIngestStore

# --- ConversationStore.find_messages_by_user_id ---

async def test_find_messages_by_user_id_returns_history_without_creating() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("919664290413")
    await store.append_message(conv_id, "user", "where is my order")
    await store.append_message(conv_id, "assistant", "let me check")

    messages = await store.find_messages_by_user_id("919664290413", limit=10)

    assert [m.content for m in messages] == ["where is my order", "let me check"]


async def test_find_messages_by_user_id_unknown_user_returns_empty_no_create() -> None:
    store = InMemoryConversationStore()

    messages = await store.find_messages_by_user_id("911111111111", limit=10)

    assert messages == []
    # The read must not have created a conversation row as a side effect.
    assert "911111111111" not in store._conversations  # type: ignore[attr-defined]


# --- ConversationStore.recent_conversations ---

async def test_recent_conversations_ordered_by_last_active_desc() -> None:
    store = InMemoryConversationStore()
    await store.get_or_create("919664290413")
    await store.get_or_create("917000000000")
    # Touch the FIRST one again so it becomes the most recently active.
    await store.get_or_create("919664290413")

    summaries = await store.recent_conversations(limit=10)

    assert [s.user_id for s in summaries] == ["919664290413", "917000000000"]


# --- IngestStore.find_outbound_by_phone ---

async def _seed_outbound(store: InMemoryIngestStore, gid: str, phone: str) -> None:
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164=phone,
        customer_name="Suman", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json='{"template": "cod_confirmation"}',
    )
    await store.ingest_order_created(f"wh-{gid}", "orders/create", mapping, draft)


async def test_find_outbound_by_phone_returns_matching_rows_with_payload() -> None:
    store = InMemoryIngestStore()
    await _seed_outbound(store, "gid://shopify/Order/1", "+919664290413")
    await _seed_outbound(store, "gid://shopify/Order/2", "+911111111111")

    rows = await store.find_outbound_by_phone("+919664290413", limit=10)

    assert len(rows) == 1
    assert rows[0].dedupe_key == "order_created:gid://shopify/Order/1"
    assert rows[0].payload_json == '{"template": "cod_confirmation"}'
    assert rows[0].state == "queued"


async def test_find_outbound_by_phone_no_match_returns_empty() -> None:
    store = InMemoryIngestStore()
    await _seed_outbound(store, "gid://shopify/Order/1", "+919664290413")

    rows = await store.find_outbound_by_phone("+910000000000", limit=10)

    assert rows == []


# --- IngestStore.find_order_actions_by_wa_ids (multi-key) ---

async def test_find_order_actions_by_wa_ids_returns_matching_rows() -> None:
    store = InMemoryIngestStore()
    await store.record_order_action(
        "gid://shopify/Order/1", "confirm", "919664290413", "m1", "ok", None
    )
    await store.record_order_action(
        "gid://shopify/Order/2", "confirm", "911111111111", "m2", "ok", None
    )

    rows = await store.find_order_actions_by_wa_ids(["919664290413"], limit=10)

    assert len(rows) == 1
    assert rows[0].order_gid == "gid://shopify/Order/1"
    assert rows[0].action == "confirm"
    assert rows[0].result == "ok"
    assert rows[0].created_at is not None


async def test_find_order_actions_by_wa_ids_matches_any_candidate_key() -> None:
    # The write side stores actor_wa_id RAW (no leading +). The read must find it whether the
    # caller passes the raw or the normalized (+-prefixed) candidate, so it merges into the same
    # thread as the normalized-keyed AI chat / outbound rows.
    store = InMemoryIngestStore()
    await store.record_order_action(
        "gid://shopify/Order/1", "confirm", "919664290413", "m1", "ok", None
    )

    rows = await store.find_order_actions_by_wa_ids(
        ["+919664290413", "919664290413"], limit=10
    )

    assert len(rows) == 1
    assert rows[0].order_gid == "gid://shopify/Order/1"


async def test_find_order_actions_by_wa_ids_no_actor_never_matches() -> None:
    # actor_wa_id can be None (system-recorded actions, e.g. reconcile's "system" actor uses a
    # literal string, but other paths may pass None) -- must not raise or false-match.
    store = InMemoryIngestStore()
    await store.record_order_action(
        "gid://shopify/Order/1", "cancelled", None, None, "ok", None
    )

    rows = await store.find_order_actions_by_wa_ids(["919664290413"], limit=10)

    assert rows == []


# --- IngestStore.distinct_outbound_phones / distinct_order_action_wa_ids ---

async def test_distinct_outbound_phones_dedupes() -> None:
    store = InMemoryIngestStore()
    await _seed_outbound(store, "gid://shopify/Order/1", "+919664290413")
    await _seed_outbound(store, "gid://shopify/Order/2", "+919664290413")
    await _seed_outbound(store, "gid://shopify/Order/3", "+911111111111")

    phones = await store.distinct_outbound_phones(limit=10)

    assert set(phones) == {"+919664290413", "+911111111111"}


async def test_distinct_order_action_wa_ids_dedupes_and_skips_none() -> None:
    store = InMemoryIngestStore()
    await store.record_order_action("gid://o/1", "confirm", "919664290413", "m1", "ok", None)
    await store.record_order_action("gid://o/2", "confirm", "919664290413", "m2", "ok", None)
    await store.record_order_action("gid://o/3", "cancelled", None, None, "ok", None)

    wa_ids = await store.distinct_order_action_wa_ids(limit=10)

    assert set(wa_ids) == {"919664290413"}


# --- ConversationStore.get_user_id ---

async def test_get_user_id_returns_normalized_user_for_thread() -> None:
    store = InMemoryConversationStore()
    thread_id = await store.get_or_create("+919664290413")

    assert await store.get_user_id(thread_id) == "+919664290413"


async def test_get_user_id_unknown_thread_returns_none() -> None:
    store = InMemoryConversationStore()

    assert await store.get_user_id(99999) is None


# --- ConversationStore.append_message stamps created_at (dual-impl parity) ---

async def test_append_message_stamps_created_at() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("+919664290413")
    await store.append_message(conv_id, "user", "hi")

    messages = await store.find_messages_by_user_id("+919664290413", limit=10)

    assert messages[0].created_at is not None
