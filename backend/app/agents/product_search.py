from typing import Protocol

from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError
from app.shopify.errors import ShopifyError
from app.shopify.models import Product

_SYSTEM_TEMPLATE = """{personality}

You help customers find products. Below are REAL search results from the store's current
catalog -- you may ONLY describe products listed here. Never invent a product, price, color,
or availability that is not in this list.

{results_context}

If nothing suitable is listed above, say so honestly and offer to connect the customer with
the team, or suggest they describe what they're looking for a little differently.

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


class ProductSource(Protocol):
    async def search_products(self, query: str, limit: int = 5) -> list[Product]: ...


def _results_context(products: list[Product]) -> str:
    if not products:
        return "No matching products were found in the current search."
    lines = []
    for p in products:
        price = f"{p.price.amount} {p.price.currency}" if p.price else "price unavailable"
        stock = "in stock" if p.available else "currently out of stock"
        lines.append(f"- {p.title} ({price}) -- {stock}")
    return "\n".join(lines)


async def run(context: AgentContext, shopify: ProductSource) -> AgentReply:
    fallback = copy_for("error_fallback", "en")
    try:
        products = await shopify.search_products(context.user_text, limit=5)
    except ShopifyError:
        products = []
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=PERSONALITY, results_context=_results_context(products)
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
        return AgentReply(text=fallback)
    return AgentReply(text=extract_reply_text(result.text, fallback))
