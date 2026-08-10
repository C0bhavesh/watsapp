from typing import Literal

from app.agents.base import extract_json_blob
from app.providers.base import LLMProvider, Message, ProviderError

Intent = Literal[
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
]

_INTENTS: tuple[Intent, ...] = (
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
)

_ROUTER_PROMPT = """Classify the customer's LATEST WhatsApp message into exactly one category.
Recent conversation history, if provided, is for context only -- classify the newest customer
message, using that context to resolve short or ambiguous replies (for example, a bare number
right after the bot asked for an order number is order_tracking, not customer_support; a plain
"yes" right after the bot offered something is about whatever was just offered).

- order_tracking: asking about an existing order (status, cancellation, tracking).
- product_search: asking whether a specific product/item/size/color is available, or to \
find something specific.
- policy: asking about shipping, returns, exchanges, refunds, COD, or other store policy.
- recommendations: asking what to buy, what goes well with something, or for suggestions or \
outfit ideas.
- customer_support: greetings, small talk, unclear messages, or explicitly asking for a \
human -- use this for anything that doesn't clearly fit the other four.

Respond with STRICT JSON only, no other text: {"intent": "<one of the five categories above>"}
"""

# How many of the most recent history messages to include for classification context -- kept
# small on purpose (the design spec calls for "a fast classification call over the message plus
# a LITTLE recent history"): enough to resolve a short/ambiguous reply against what the bot just
# asked, without ballooning the router's prompt size or latency on every single turn.
_HISTORY_MESSAGES_FOR_ROUTING = 2


async def classify_intent(
    provider: LLMProvider,
    model: str,
    api_key: str,
    user_text: str,
    *,
    history: list[Message] | None = None,
    timeout: float = 10.0,
    extra_params: dict[str, object] | None = None,
) -> Intent:
    """Classify one customer message into an Intent, using a little recent history to resolve
    short/ambiguous replies (a bare number right after the bot asked for an order number, a
    plain "yes" after an offer) that carry no signal in isolation. Any failure (provider error
    or an unparseable/unrecognized completion) degrades to customer_support -- the safe
    catch-all, never leaving a message unrouted."""
    recent_history = (history or [])[-_HISTORY_MESSAGES_FOR_ROUTING:]
    messages = [
        Message(role="system", content=_ROUTER_PROMPT),
        *recent_history,
        Message(role="user", content=user_text),
    ]
    try:
        result = await provider.complete(
            model, messages, api_key, timeout, extra_params=extra_params
        )
    except ProviderError:
        return "customer_support"
    data = extract_json_blob(result.text)
    if data is None:
        return "customer_support"
    intent = data.get("intent")
    return intent if intent in _INTENTS else "customer_support"
