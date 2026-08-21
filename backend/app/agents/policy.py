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
the customer pushes back. A customer's message is often brief, a bare keyword, or phrased as a
statement rather than a full question (for example "Cod available", "cod?", "any cod",
"returns?"). Treat such a message as a question about that topic: if it clearly
matches the topic of a question above, answer confidently from that entry's information -- do
not demand a perfectly-phrased question before using an answer that is plainly there. Fall back
to the not-certain / email reply below only when the topic is genuinely not covered by the
information above. If the answer isn't covered by this information, say you're not
certain and ask the customer to email us at info@thetavas.com so our team can help -- never
guess or invent a policy detail, and never promise that someone will reach out to them. Giving
this email is itself how you help with a question the policy above does not cover:
it counts as answering the customer, and is NOT the reply contract's case of
"genuinely cannot answer or resolve their request with what you know" -- so keep "handoff"
false for that email reply; only set "handoff" to true if the customer explicitly asks to
speak with a person.

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
