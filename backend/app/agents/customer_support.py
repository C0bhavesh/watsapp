from datetime import datetime, timedelta

from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError
from app.store.base import ConversationStore

HANDOFF_MESSAGE = (
    "I'm connecting you with our team -- they'll continue helping you right here in this chat."
)

_HANDOFF_WINDOW = timedelta(hours=24)

_HUMAN_REQUEST_PHRASES = (
    "talk to a human",
    "speak to a human",
    "real person",
    "human agent",
    "talk to someone",
    "speak to someone",
    "human please",
    "connect me to",
    "escalate",
)

_SYSTEM_TEMPLATE = """{personality}

The customer's message didn't clearly match order tracking, product search, policy, or
recommendations -- help with greetings, small talk, or general questions as best you can. If
you cannot help, say so honestly.

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


def _wants_human(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _HUMAN_REQUEST_PHRASES)


async def _already_attempted_recently(
    store: ConversationStore, conversation_id: int, now: datetime
) -> bool:
    attempted_at = await store.get_handoff_attempted_at(conversation_id)
    if attempted_at is None:
        return False
    return (now - attempted_at) < _HANDOFF_WINDOW


async def _handoff(
    store: ConversationStore, conversation_id: int, now: datetime, text: str
) -> AgentReply:
    await store.pause_until(conversation_id, now + _HANDOFF_WINDOW)
    return AgentReply(text=text, handoff=True)


async def run(
    context: AgentContext, store: ConversationStore, conversation_id: int, now: datetime
) -> AgentReply:
    """One AI attempt per conversation window, then immediate handoff (client decision,
    round 3 2026-08-06). A second explicit human request within the window skips the LLM
    call entirely -- deterministic, no persuasion attempted. Any provider failure, or the
    LLM's own safe-fallback degradation, is also treated as "could not help" -> handoff.
    """
    fallback = copy_for("error_fallback", "en")
    wants_human = _wants_human(context.user_text)
    already_attempted = await _already_attempted_recently(store, conversation_id, now)

    if wants_human and already_attempted:
        return await _handoff(store, conversation_id, now, HANDOFF_MESSAGE)

    if wants_human and not already_attempted:
        await store.mark_handoff_attempted(conversation_id, now)

    system_prompt = _SYSTEM_TEMPLATE.format(personality=PERSONALITY)
    messages = [
        Message(role="system", content=system_prompt),
        *context.history,
        Message(role="user", content=context.user_text),
    ]
    try:
        result = await context.provider.complete(
            context.model,
            messages,
            context.api_key,
            context.timeout,
            extra_params=context.extra_params,
        )
    except ProviderError:
        return await _handoff(store, conversation_id, now, f"{fallback} {HANDOFF_MESSAGE}")

    reply = extract_reply_text(result.text, fallback)
    if reply == fallback:
        return await _handoff(store, conversation_id, now, f"{reply} {HANDOFF_MESSAGE}")
    return AgentReply(text=reply)
