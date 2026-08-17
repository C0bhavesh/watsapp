from app.providers.base import Message
from app.store.base import ConversationStore

DEFAULT_WINDOW = 8


async def load_history(
    store: ConversationStore, wa_id: str, window: int = DEFAULT_WINDOW
) -> tuple[int, list[Message]]:
    """Return (conversation_id, recent turns as provider Message objects), creating the
    conversation on first contact. Only user/assistant turns are replayed into the prompt."""
    conversation_id = await store.get_or_create(wa_id)
    # A real inbound message IS genuine activity -- bump recency explicitly. get_or_create no longer
    # bumps on an existing row (so display-only lookups don't corrupt recency), so the real-message
    # path restores "recent activity floats to the top" via touch. Fires exactly once per inbound
    # message: load_history is called once per turn in core/conversation.handle_message.
    await store.touch(wa_id)
    stored = await store.recent_messages(conversation_id, window)
    history: list[Message] = []
    for m in stored:
        if m.role == "user":
            history.append(Message(role="user", content=m.content))
        elif m.role == "assistant":
            history.append(Message(role="assistant", content=m.content))
    return conversation_id, history


async def persist_turn(
    store: ConversationStore, conversation_id: int, user_text: str, assistant_reply: str
) -> int:
    """Persist the user + assistant turn, returning the assistant message's new id so the caller
    can attach the WhatsApp wamid to it once the reply is actually sent (delivery tracking)."""
    await store.append_message(conversation_id, "user", user_text)
    return await store.append_message(conversation_id, "assistant", assistant_reply)
