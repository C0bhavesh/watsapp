from app.agents.base import (
    AgentContext,
    AgentReply,
    extract_json_blob,
    extract_reply_text,
    personality_for,
)
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError

HANDOFF_MESSAGE = (
    "I'm connecting you with our team -- they'll continue helping you right here in this chat."
)

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
recommendations -- help with greetings, small talk, or general questions as best you can.

You get ONE attempt. If you cannot genuinely resolve what the customer needs -- or if they
ask to speak to a person, in any language -- do not try to persuade them to stay with you:
set "handoff" to true and a teammate will take over this same chat.

Respond with STRICT JSON only, no other text:
{{"reply": "<your reply to the customer>", "handoff": <true or false>}}
"""


def _wants_human(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _HUMAN_REQUEST_PHRASES)


def _model_asked_for_handoff(data: dict[str, object] | None) -> bool:
    """Read the model's own ``handoff`` judgment, strictly.

    ``bool("false")`` is True, so a model that emits the flag as a string must be compared by
    value -- anything that is not a real true never escalates by accident.
    """
    if data is None:
        return False
    value = data.get("handoff")
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


async def run(context: AgentContext) -> AgentReply:
    """One AI attempt, then handoff -- no second attempt, no persuasion (design spec).

    An explicit English request for a person is matched deterministically and hands off
    immediately, skipping the LLM call entirely. Everything else gets exactly one attempt: if
    the model judges it cannot resolve the issue it returns ``handoff: true`` itself, which is
    how a Hindi/Hinglish/Gujarati request the phrase list cannot match still escalates. A
    provider failure, or the model's own safe-fallback degradation, is "could not help" too.

    Setting the conversation pause is NOT this agent's job -- it returns ``handoff`` and
    ``core.conversation`` applies the same pause for whichever agent asked for it.
    """
    fallback = copy_for("error_fallback", context.language)
    if _wants_human(context.user_text):
        return AgentReply(text=HANDOFF_MESSAGE, handoff=True)

    system_prompt = _SYSTEM_TEMPLATE.format(personality=personality_for(context))
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
        return AgentReply(text=f"{fallback} {HANDOFF_MESSAGE}", handoff=True)

    reply = extract_reply_text(result.text, fallback)
    if reply == fallback or _model_asked_for_handoff(extract_json_blob(result.text)):
        return AgentReply(text=f"{reply} {HANDOFF_MESSAGE}", handoff=True)
    return AgentReply(text=reply)
