"""New read-only chat-aggregation methods: ConversationStore + IngestStore additions."""

from datetime import UTC, datetime, timedelta

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
    # touch() (genuine activity) bumps the FIRST one so it becomes the most recently active.
    await store.touch("919664290413")

    summaries = await store.recent_conversations(limit=10)

    assert [s.user_id for s in summaries] == ["919664290413", "917000000000"]


async def test_get_or_create_does_not_bump_last_active_on_existing() -> None:
    # The bug: get_or_create used to bump last_active_at on every call (even an already-existing
    # row), so the display-only admin thread-list lookup corrupted recency on every page load.
    store = InMemoryConversationStore()
    await store.get_or_create("919664290413")
    first = (await store.recent_conversations())[0].last_active_at

    await store.get_or_create("919664290413")  # a genuine no-op for recency now
    second = (await store.recent_conversations())[0].last_active_at

    assert first is not None and first == second


async def test_touch_bumps_last_active_on_existing() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("919664290413")
    # Pin an old stamp so the assertion is deterministic on coarse clocks (two now() calls can
    # otherwise land in the same tick and compare equal).
    store._last_active_at[conv_id] = datetime(2020, 1, 1, tzinfo=UTC)  # type: ignore[attr-defined]
    before = (await store.recent_conversations())[0].last_active_at

    await store.touch("919664290413")
    after = (await store.recent_conversations())[0].last_active_at

    assert before is not None and after is not None and after > before


async def test_touch_missing_conversation_is_noop() -> None:
    store = InMemoryConversationStore()
    await store.touch("no-such-user")  # must not raise or create a row
    assert "no-such-user" not in store._conversations  # type: ignore[attr-defined]


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


# --- IngestStore outbound delivery-status tracking (Task 3) ---

async def _send_outbound(store: InMemoryIngestStore, gid: str, phone: str, wamid: str) -> None:
    await _seed_outbound(store, gid, phone)
    (claim,) = await store.claim_queued_outbound()
    await store.mark_outbound_sent(claim.id, wamid)


async def test_apply_outbound_delivery_status_updates_by_wamid() -> None:
    store = InMemoryIngestStore()
    await _send_outbound(store, "gid://shopify/Order/1", "+919664290413", "wamid.OUT123")

    applied = await store.apply_outbound_delivery_status("wamid.OUT123", "delivered")

    assert applied == "applied"
    entries = await store.find_outbound_by_phone("+919664290413")
    assert entries[-1].delivery_status == "delivered"


async def test_apply_outbound_delivery_status_unknown_wamid_returns_not_found() -> None:
    store = InMemoryIngestStore()
    applied = await store.apply_outbound_delivery_status("wamid.NEVER_SEEN", "delivered")
    assert applied == "not_found"


async def test_apply_outbound_delivery_status_respects_ordering_guard() -> None:
    store = InMemoryIngestStore()
    await _send_outbound(store, "gid://shopify/Order/1", "+919664290413", "wamid.OUT456")
    await store.apply_outbound_delivery_status("wamid.OUT456", "read")

    regressed = await store.apply_outbound_delivery_status("wamid.OUT456", "delivered")

    # found but not applied: the wamid IS in this table but the ordering guard blocks the
    # read->delivered regression, so it returns "unchanged" and the stored value stays "read".
    assert regressed == "unchanged"
    entries = await store.find_outbound_by_phone("+919664290413")
    assert entries[-1].delivery_status == "read"


async def test_apply_outbound_delivery_status_returns_unchanged_on_duplicate_failed() -> None:
    # Meta redelivers webhooks for reliability: a second 'failed' report for the same wamid must
    # report "unchanged" (already-terminal failed), so Task 5 never fires a second retry.
    store = InMemoryIngestStore()
    await _send_outbound(store, "gid://shopify/Order/1", "+919664290413", "wamid.OUT789")

    first = await store.apply_outbound_delivery_status("wamid.OUT789", "failed")
    second = await store.apply_outbound_delivery_status("wamid.OUT789", "failed")

    assert first == "applied"
    assert second == "unchanged"


# --- IngestStore outbound retry-info + retry-record (Task 2) ---

async def test_get_outbound_retry_info_returns_current_state() -> None:
    store = InMemoryIngestStore()
    await _send_outbound(store, "gid://shopify/Order/1", "+919664290413", "wamid.RETRY1")

    info = await store.get_outbound_retry_info("wamid.RETRY1")

    assert info is not None
    assert info.retry_count == 0
    assert info.phone_e164 == "+919664290413"
    assert info.payload_json == '{"template": "cod_confirmation"}'


async def test_get_outbound_retry_info_unknown_wamid_returns_none() -> None:
    store = InMemoryIngestStore()
    info = await store.get_outbound_retry_info("wamid.NEVER_SEEN")
    assert info is None


async def test_record_outbound_retry_with_new_wamid_resets_delivery_status() -> None:
    store = InMemoryIngestStore()
    await _send_outbound(store, "gid://shopify/Order/1", "+919664290413", "wamid.FAIL1")
    await store.apply_outbound_delivery_status("wamid.FAIL1", "failed")
    seeded = await store.get_outbound_retry_info("wamid.FAIL1")
    assert seeded is not None
    row_id = seeded.id

    await store.record_outbound_retry(row_id, "wamid.RESENT1")

    info = await store.get_outbound_retry_info("wamid.RESENT1")
    assert info is not None
    assert info.retry_count == 1
    # The old wamid no longer routes to the row (it was reassigned to the resend's wamid).
    assert await store.get_outbound_retry_info("wamid.FAIL1") is None
    entries = await store.find_outbound_by_phone("+919664290413")
    assert entries[-1].delivery_status is None  # reset, awaiting fresh confirmation


async def test_record_outbound_retry_with_no_new_wamid_increments_count_only() -> None:
    store = InMemoryIngestStore()
    await _send_outbound(store, "gid://shopify/Order/1", "+919664290413", "wamid.FAIL2")
    await store.apply_outbound_delivery_status("wamid.FAIL2", "failed")
    seeded = await store.get_outbound_retry_info("wamid.FAIL2")
    assert seeded is not None
    row_id = seeded.id

    # No new wamid (the resend itself could not be sent / was suppressed): count increments, but
    # the wamid and delivery_status are left as-is -- verify via the ORIGINAL wamid.
    await store.record_outbound_retry(row_id, None)

    info = await store.get_outbound_retry_info("wamid.FAIL2")
    assert info is not None
    assert info.retry_count == 1
    entries = await store.find_outbound_by_phone("+919664290413")
    assert entries[-1].delivery_status == "failed"  # unchanged


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

    # (phone, latest_iso) pairs; each pair carries a real recency stamp (never None here).
    assert {phone for phone, _ in phones} == {"+919664290413", "+911111111111"}
    assert all(ts is not None for _, ts in phones)


async def test_distinct_outbound_phones_ordered_by_recency_not_alphabetical() -> None:
    # Seed so alphabetical(phone) != recency, proving the LIMIT keeps the MOST RECENT phones
    # (Postgres MAX(created_at) DESC) rather than a lexicographic-first slice (the reported bug).
    store = InMemoryIngestStore()
    await _seed_outbound(store, "gid://shopify/Order/a", "+911111111111")  # alpha-first
    await _seed_outbound(store, "gid://shopify/Order/b", "+915555555555")  # alpha-middle
    await _seed_outbound(store, "gid://shopify/Order/c", "+919664290413")  # alpha-last
    base = datetime(2026, 8, 17, tzinfo=UTC)
    meta = store._outbound_meta
    meta["order_created:gid://shopify/Order/a"].created_at = base + timedelta(hours=1)
    meta["order_created:gid://shopify/Order/b"].created_at = base + timedelta(hours=3)
    meta["order_created:gid://shopify/Order/c"].created_at = base + timedelta(hours=2)

    result = await store.distinct_outbound_phones(limit=10)

    # Recency DESC (b@3h, c@2h, a@1h) -- NOT alphabetical (which would be 1.., 5.., 9..).
    assert [phone for phone, _ in result] == ["+915555555555", "+919664290413", "+911111111111"]
    # The LIMIT must drop the OLDEST (alphabetically-first) phone, not keep it.
    top2 = await store.distinct_outbound_phones(limit=2)
    assert [phone for phone, _ in top2] == ["+915555555555", "+919664290413"]


async def test_distinct_order_action_wa_ids_dedupes_and_skips_none() -> None:
    store = InMemoryIngestStore()
    await store.record_order_action("gid://o/1", "confirm", "919664290413", "m1", "ok", None)
    await store.record_order_action("gid://o/2", "confirm", "919664290413", "m2", "ok", None)
    await store.record_order_action("gid://o/3", "cancelled", None, None, "ok", None)

    wa_ids = await store.distinct_order_action_wa_ids(limit=10)

    assert {wa for wa, _ in wa_ids} == {"919664290413"}
    assert all(ts is not None for _, ts in wa_ids)


async def test_distinct_order_action_wa_ids_ordered_by_recency_not_alphabetical() -> None:
    store = InMemoryIngestStore()
    await store.record_order_action("gid://o/a", "confirm", "911111111111", "m1", "ok", None)
    await store.record_order_action("gid://o/b", "confirm", "915555555555", "m2", "ok", None)
    await store.record_order_action("gid://o/c", "confirm", "919664290413", "m3", "ok", None)
    base = datetime(2026, 8, 17, tzinfo=UTC)
    store.order_actions[0]["created_at"] = (base + timedelta(hours=1)).isoformat()
    store.order_actions[1]["created_at"] = (base + timedelta(hours=3)).isoformat()
    store.order_actions[2]["created_at"] = (base + timedelta(hours=2)).isoformat()

    result = await store.distinct_order_action_wa_ids(limit=10)

    assert [wa for wa, _ in result] == ["915555555555", "919664290413", "911111111111"]
    top2 = await store.distinct_order_action_wa_ids(limit=2)
    assert [wa for wa, _ in top2] == ["915555555555", "919664290413"]


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


# --- ConversationStore wamid + delivery-status tracking (Task 2) ---

async def test_append_message_returns_the_new_message_id() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("+919876500050")
    msg_id = await store.append_message(conv_id, "assistant", "hello")
    assert isinstance(msg_id, int)


async def test_set_message_wamid_then_apply_delivery_status() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("+919876500051")
    msg_id = await store.append_message(conv_id, "assistant", "your order shipped")
    await store.set_message_wamid(msg_id, "wamid.TEST123")

    applied = await store.apply_message_delivery_status("wamid.TEST123", "delivered")

    assert applied == "applied"
    messages = await store.find_messages_by_user_id("+919876500051")
    assert messages[-1].delivery_status == "delivered"


async def test_apply_message_delivery_status_unknown_wamid_returns_not_found() -> None:
    store = InMemoryConversationStore()
    applied = await store.apply_message_delivery_status("wamid.NEVER_SEEN", "delivered")
    assert applied == "not_found"


async def test_apply_message_delivery_status_respects_ordering_guard() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("+919876500052")
    msg_id = await store.append_message(conv_id, "assistant", "hi")
    await store.set_message_wamid(msg_id, "wamid.TEST456")
    await store.apply_message_delivery_status("wamid.TEST456", "read")

    regressed = await store.apply_message_delivery_status("wamid.TEST456", "delivered")

    # found but not applied: the wamid IS in this table but the ordering guard blocks the
    # read->delivered regression, so it returns "unchanged" and the stored value stays "read".
    assert regressed == "unchanged"
    messages = await store.find_messages_by_user_id("+919876500052")
    assert messages[-1].delivery_status == "read"


async def test_apply_message_delivery_status_returns_unchanged_on_duplicate_failed() -> None:
    # A redelivered 'failed' status for the same wamid must report "unchanged" (already-terminal),
    # so retry wiring (Task 5) never double-fires on a Meta redelivery.
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("+919876500054")
    msg_id = await store.append_message(conv_id, "assistant", "hi")
    await store.set_message_wamid(msg_id, "wamid.TEST789")

    first = await store.apply_message_delivery_status("wamid.TEST789", "failed")
    second = await store.apply_message_delivery_status("wamid.TEST789", "failed")

    assert first == "applied"
    assert second == "unchanged"


async def test_user_role_messages_unaffected_by_delivery_status_default() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("+919876500053")
    await store.append_message(conv_id, "user", "hi")
    messages = await store.find_messages_by_user_id("+919876500053")
    assert messages[-1].delivery_status is None
