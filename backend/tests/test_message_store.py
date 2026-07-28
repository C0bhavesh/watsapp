from app.store.memory import InMemoryMessageStore


async def test_first_time_message_is_new() -> None:
    store = InMemoryMessageStore()
    assert await store.record_if_new("wamid.1") is True
    assert "wamid.1" in store.seen


async def test_replayed_message_is_not_new() -> None:
    store = InMemoryMessageStore()
    await store.record_if_new("wamid.1")
    assert await store.record_if_new("wamid.1") is False
