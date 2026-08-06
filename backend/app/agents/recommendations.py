from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.agents.product_search import ProductSource
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError
from app.shopify.errors import ShopifyError
from app.shopify.models import Product

_SYSTEM_TEMPLATE = """{personality}

The customer is asking for a recommendation, outfit idea, or suggestion. Below are REAL
products from the store's current catalog -- you may ONLY suggest items listed here, never
invent a product. Recommend naturally (cross-sell, upsell, complete-the-look, matching
accessories) but ALWAYS answer the customer's original question first, and never be pushy --
one or two genuine suggestions is enough.

{results_context}

If nothing suitable is available, say so honestly and offer to connect the customer with the
team instead of forcing a recommendation.

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


def _results_context(products: list[Product]) -> str:
    if not products:
        return "No matching products were found for this recommendation."
    lines = []
    for p in products:
        price = f"{p.price.amount} {p.price.currency}" if p.price else "price unavailable"
        stock = "in stock" if p.available else "currently out of stock"
        lines.append(f"- {p.title} ({price}) -- {stock}")
    return "\n".join(lines)


async def run(context: AgentContext, shopify: ProductSource) -> AgentReply:
    fallback = copy_for("error_fallback", "en")
    try:
        # None ("nothing searchable in the message") and [] ("searched, no match") both mean
        # "nothing to recommend" here -- recommendations never broadens or re-searches.
        products = await shopify.search_products(context.user_text, limit=5) or []
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
            context.model,
            messages,
            context.api_key,
            context.timeout,
            extra_params=context.extra_params,
        )
    except ProviderError:
        return AgentReply(text=fallback)
    return AgentReply(text=extract_reply_text(result.text, fallback))
