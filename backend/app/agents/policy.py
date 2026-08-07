from app.agents.base import AgentContext, AgentReply, extract_reply_text, personality_for
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

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


async def run(context: AgentContext) -> AgentReply:
    fallback = copy_for("error_fallback", "en")
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        faq=context.knowledge.get("faq", ""),
        business=context.knowledge.get("business", ""),
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
        return AgentReply(text=fallback)
    return AgentReply(text=extract_reply_text(result.text, fallback))
