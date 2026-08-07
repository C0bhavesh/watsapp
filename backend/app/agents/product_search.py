from typing import Protocol

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
from app.shopify.client import sanitize_product_query  # pure helper, not the client itself
from app.shopify.errors import ShopifyError
from app.shopify.models import Product

_SYSTEM_TEMPLATE = """{personality}

You help customers find products. Below are REAL search results from the store's current
catalog -- you may ONLY describe products listed here. Never invent a product, price, color,
or availability that is not in this list.

{results_context}

If nothing suitable is listed above, say so honestly and offer to connect the customer with
the team, or suggest they describe what they're looking for a little differently.

{contract}
"""


class ProductSource(Protocol):
    async def search_products(self, query: str, limit: int = 5) -> list[Product] | None: ...


def _broaden(query: str) -> str | None:
    """Drop the trailing word of a sanitized query, or None if there is nothing to drop.

    Qualifiers (colour/size/fit words) tend to trail the core product noun in the short
    phrases customers actually send, so dropping the last word is the cheapest broadening
    that keeps the noun. One retry only, per the design spec.
    """
    words = query.split()
    return " ".join(words[:-1]) if len(words) > 1 else None


def _results_context(products: list[Product] | None) -> str:
    if products is None:
        return (
            "The customer's message did not contain a searchable product term, so no catalog "
            "search was run. Ask them which product they are looking for."
        )
    if not products:
        return "No matching products were found in the current search."
    lines = []
    for p in products:
        price = f"{p.price.amount} {p.price.currency}" if p.price else "price unavailable"
        stock = "in stock" if p.available else "currently out of stock"
        lines.append(f"- {p.title} ({price}) -- {stock}")
    return "\n".join(lines)


async def run(context: AgentContext, shopify: ProductSource) -> AgentReply:
    fallback = copy_for("error_fallback", context.language)
    query = sanitize_product_query(context.user_text)
    products: list[Product] | None = None
    if query is not None:
        try:
            products = await shopify.search_products(query, limit=5)
            if products is not None and not products:
                broadened = _broaden(query)
                if broadened is not None:
                    products = await shopify.search_products(broadened, limit=5)
        except ShopifyError:
            # A Shopify outage is reported to the model as "search found nothing" rather than
            # "nothing searchable" -- pre-existing behaviour, unchanged here.
            products = []
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        results_context=_results_context(products),
        contract=HANDOFF_JSON_CONTRACT,
    )
    messages = [
        Message(role="system", content=system_prompt),
        *context.history,
        Message(role="user", content=context.user_text),
    ]
    try:
        result = await context.provider.complete(
            context.model, messages, context.api_key, context.timeout,
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
