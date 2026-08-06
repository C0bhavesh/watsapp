from app.providers.base import Message
from app.store.base import ConversationStore

DEFAULT_WINDOW = 8


async def load_history(
    store: ConversationStore, wa_id: str, window: int = DEFAULT_WINDOW
) -> tuple[int, list[Message]]:
    """Return (conversation_id, recent turns as provider Message objects), creating the
    conversation on first contact. Only user/assistant turns are replayed into the prompt."""
    conversation_id = await store.get_or_create(wa_id)
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
) -> None:
    await store.append_message(conversation_id, "user", user_text)
    await store.append_message(conversation_id, "assistant", assistant_reply)
