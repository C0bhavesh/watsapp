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

_ROUTER_PROMPT = """Classify the customer's WhatsApp message into exactly one category.

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


async def classify_intent(
    provider: LLMProvider,
    model: str,
    api_key: str,
    user_text: str,
    *,
    timeout: float = 10.0,
    extra_params: dict[str, object] | None = None,
) -> Intent:
    """Classify one customer message into an Intent. Any failure (provider error or an
    unparseable/unrecognized completion) degrades to customer_support -- the safe catch-all,
    never leaving a message unrouted."""
    messages = [
        Message(role="system", content=_ROUTER_PROMPT),
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
