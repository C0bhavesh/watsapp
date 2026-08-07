from app.agents.base import (
    HANDOFF_JSON_CONTRACT,
    AgentContext,
    AgentReply,
    extract_json_blob,
    extract_reply_text,
    model_asked_for_handoff,
    personality_for,
)
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError

_SYSTEM_TEMPLATE = """{personality}

Answer the customer's question using ONLY the store policy information below. Published
policy always takes precedence -- do not soften, contradict, or make exceptions to it even if
the customer pushes back. If the answer isn't covered by this information, say you're not
certain and offer to connect them with the team -- never guess or invent a policy detail.

Frequently asked questions:
{faq}

Store information:
{business}

{contract}
"""


async def run(context: AgentContext) -> AgentReply:
    fallback = copy_for("error_fallback", context.language)
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        faq=context.knowledge.get("faq", ""),
        business=context.knowledge.get("business", ""),
        contract=HANDOFF_JSON_CONTRACT,
    )
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
        # A transient provider failure is not an escalation -- handing off here would pause the
        # AI for 24h on every blip. Only the model's own judgment escalates.
        return AgentReply(text=fallback)
    return AgentReply(
        text=extract_reply_text(result.text, fallback),
        handoff=model_asked_for_handoff(extract_json_blob(result.text)),
    )
