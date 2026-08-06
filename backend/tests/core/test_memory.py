from app.core.memory import load_history, persist_turn
from app.store.memory import InMemoryConversationStore


async def test_first_load_creates_conversation_with_empty_history() -> None:
    store = InMemoryConversationStore()
    conversation_id, history = await load_history(store, "919999999999")
    assert conversation_id is not None
    assert history == []


async def test_persist_then_load_returns_turns_in_order() -> None:
    store = InMemoryConversationStore()
    conversation_id, _ = await load_history(store, "919999999999")
    await persist_turn(store, conversation_id, "where is my order", "let me check")
    _, history = await load_history(store, "919999999999")
    assert [m.content for m in history] == ["where is my order", "let me check"]
    assert [m.role for m in history] == ["user", "assistant"]


async def test_same_wa_id_reuses_conversation() -> None:
    store = InMemoryConversationStore()
    id1, _ = await load_history(store, "919999999999")
    id2, _ = await load_history(store, "919999999999")
    assert id1 == id2


async def test_window_limits_history_length() -> None:
    store = InMemoryConversationStore()
    conversation_id, _ = await load_history(store, "919999999999")
    for i in range(10):
        await persist_turn(store, conversation_id, f"msg{i}", f"reply{i}")
    _, history = await load_history(store, "919999999999", window=4)
    assert len(history) == 4
