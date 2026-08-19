import os
from datetime import UTC, datetime, timedelta

import pytest

from app.store.memory import InMemoryConversationStore


async def test_get_or_create_is_stable_per_user() -> None:
    store = InMemoryConversationStore()
    id1 = await store.get_or_create("919999999999")
    id2 = await store.get_or_create("919999999999")
    assert id1 == id2


async def test_different_users_get_different_conversations() -> None:
    store = InMemoryConversationStore()
    id1 = await store.get_or_create("919999999999")
    id2 = await store.get_or_create("918888888888")
    assert id1 != id2


async def test_append_and_recent_messages_roundtrip() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    await store.append_message(conversation_id, "user", "hello")
    await store.append_message(conversation_id, "assistant", "hi there")
    messages = await store.recent_messages(conversation_id, 10)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]


async def test_recent_messages_respects_limit() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    for i in range(5):
        await store.append_message(conversation_id, "user", f"msg{i}")
    messages = await store.recent_messages(conversation_id, 2)
    assert [m.content for m in messages] == ["msg3", "msg4"]


async def test_paused_until_roundtrip() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    assert await store.get_paused_until(conversation_id) is None
    until = datetime(2026, 1, 1, tzinfo=UTC)
    await store.pause_until(conversation_id, until)
    assert await store.get_paused_until(conversation_id) == until


async def test_handoff_attempted_roundtrip() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    assert await store.get_handoff_attempted_at(conversation_id) is None
    at = datetime(2026, 1, 1, tzinfo=UTC)
    await store.mark_handoff_attempted(conversation_id, at)
    assert await store.get_handoff_attempted_at(conversation_id) == at


async def test_append_message_defaults_sender_to_none() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("+919876500099")
    await store.append_message(conversation_id, "assistant", "hi")
    messages = await store.recent_messages(conversation_id, limit=10)
    assert messages[0].sender is None


async def test_append_message_records_admin_sender() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("+919876500098")
    await store.append_message(conversation_id, "assistant", "manual reply", sender="admin")
    messages = await store.recent_messages(conversation_id, limit=10)
    assert messages[0].sender == "admin"


async def test_find_messages_by_user_id_includes_sender() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("+919876500097")
    await store.append_message(conversation_id, "assistant", "manual reply", sender="admin")
    messages = await store.find_messages_by_user_id("+919876500097", limit=10)
    assert messages[0].sender == "admin"


async def test_manual_reply_row_is_visible_to_ai_memory() -> None:
    """A manual reply (role='assistant', sender='admin') must load into AI context exactly like
    an AI-generated row -- core/memory.py::load_history only branches on `role`, never `sender`.
    """
    from app.core.memory import load_history

    store = InMemoryConversationStore()
    user_id = "+919876500096"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "user", "where is my order")
    await store.append_message(
        conversation_id, "assistant", "It shipped yesterday.", sender="admin"
    )
    _, history = await load_history(store, user_id)
    assert any(m.role == "assistant" and m.content == "It shipped yesterday." for m in history)


async def test_manual_reply_row_is_eligible_for_delivery_retry_lookup() -> None:
    """A manual reply must pass delivery_retry.py's `role = 'assistant'` filter (added in
    sub-project 1e's final review) so a failed manual send still auto-retries.
    """
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("+919876500095")
    message_id = await store.append_message(
        conversation_id, "assistant", "Delayed by a day, sorry!", sender="admin"
    )
    await store.set_message_wamid(message_id, "wamid.MANUALRETRY1")
    info = await store.get_message_retry_info("wamid.MANUALRETRY1")
    assert info is not None
    assert info.content == "Delayed by a day, sorry!"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_conversation_roundtrip_postgres() -> None:
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "user", "hello")
    await store.append_message(conversation_id, "assistant", "hi there")
    messages = await store.recent_messages(conversation_id, 10)
    assert [m.content for m in messages] == ["hello", "hi there"]
    same_id = await store.get_or_create(user_id)
    assert same_id == conversation_id

    until = datetime.now(UTC) + timedelta(hours=1)
    await store.pause_until(conversation_id, until)
    fetched = await store.get_paused_until(conversation_id)
    assert fetched is not None and abs((fetched - until).total_seconds()) < 1

    attempted_at = datetime.now(UTC)
    await store.mark_handoff_attempted(conversation_id, attempted_at)
    fetched_attempt = await store.get_handoff_attempted_at(conversation_id)
    assert fetched_attempt is not None and abs((fetched_attempt - attempted_at).total_seconds()) < 1
    await pool.close()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_get_or_create_upsert_prevents_duplicate_rows_postgres() -> None:
    """Regression for the SELECT-then-INSERT race: get_or_create must be a single atomic
    ON CONFLICT upsert, so repeated calls for the same user_id never create a second row."""
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"

    id1 = await store.get_or_create(user_id)
    id2 = await store.get_or_create(user_id)
    assert id1 == id2

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM conversations WHERE user_id = $1", user_id
        )
    assert count == 1
    await pool.close()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_append_message_defaults_sender_to_none_postgres() -> None:
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "assistant", "hi")
    messages = await store.recent_messages(conversation_id, limit=10)
    assert messages[0].sender is None
    await pool.close()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_append_message_records_admin_sender_postgres() -> None:
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "assistant", "manual reply", sender="admin")
    messages = await store.recent_messages(conversation_id, limit=10)
    assert messages[0].sender == "admin"
    await pool.close()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_find_messages_by_user_id_includes_sender_postgres() -> None:
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "assistant", "manual reply", sender="admin")
    messages = await store.find_messages_by_user_id(user_id, limit=10)
    assert messages[0].sender == "admin"
    await pool.close()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_manual_reply_row_is_visible_to_ai_memory_postgres() -> None:
    """A manual reply (role='assistant', sender='admin') must load into AI context exactly like
    an AI-generated row -- core/memory.py::load_history only branches on `role`, never `sender`.
    """
    import uuid

    from app.core.memory import load_history
    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "user", "where is my order")
    await store.append_message(
        conversation_id, "assistant", "It shipped yesterday.", sender="admin"
    )
    _, history = await load_history(store, user_id)
    assert any(m.role == "assistant" and m.content == "It shipped yesterday." for m in history)
    await pool.close()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_manual_reply_row_is_eligible_for_delivery_retry_lookup_postgres() -> None:
    """A manual reply must pass delivery_retry.py's `role = 'assistant'` filter (added in
    sub-project 1e's final review) so a failed manual send still auto-retries.
    """
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    message_id = await store.append_message(
        conversation_id, "assistant", "Delayed by a day, sorry!", sender="admin"
    )
    await store.set_message_wamid(message_id, "wamid.MANUALRETRY1")
    info = await store.get_message_retry_info("wamid.MANUALRETRY1")
    assert info is not None
    assert info.content == "Delayed by a day, sorry!"
    await pool.close()


async def test_mark_read_defaults_to_creation_time_so_new_thread_has_no_unread() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    assert await store.count_unread_messages(conversation_id) == 0


async def test_unread_count_reflects_only_user_messages_after_last_read() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await store.mark_read(conversation_id, old)
    await store.append_message(conversation_id, "user", "hi")
    await store.append_message(conversation_id, "assistant", "hello")
    await store.append_message(conversation_id, "user", "still there?")
    assert await store.count_unread_messages(conversation_id) == 2


async def test_mark_read_clears_unread_count() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await store.mark_read(conversation_id, old)
    await store.append_message(conversation_id, "user", "hi")
    assert await store.count_unread_messages(conversation_id) == 1
    await store.mark_read(conversation_id, datetime.now(UTC))
    assert await store.count_unread_messages(conversation_id) == 0


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_unread_count_roundtrip_postgres() -> None:
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    assert await store.count_unread_messages(conversation_id) == 0

    old = datetime(2020, 1, 1, tzinfo=UTC)
    await store.mark_read(conversation_id, old)
    await store.append_message(conversation_id, "user", "hi")
    await store.append_message(conversation_id, "assistant", "hello")
    assert await store.count_unread_messages(conversation_id) == 1

    await store.mark_read(conversation_id, datetime.now(UTC))
    assert await store.count_unread_messages(conversation_id) == 0
    await pool.close()
