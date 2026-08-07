from app.agents.base import AgentContext
from app.agents.recommendations import run
from app.providers.base import CompletionResult, Message, ProviderError
from app.shopify.models import Money, Product


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: ProviderError | None = None) -> None:
        self._text = text
        self.raises = raises
        self.captured_messages: list[Message] = []

    async def complete(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: object = None,
    ) -> CompletionResult:
        if self.raises:
            raise self.raises
        self.captured_messages = messages
        return CompletionResult(text=self._text or "", model=model)


class _FakeShopify:
    def __init__(self, products: list[Product] | None = None) -> None:
        self.products = products or []

    async def search_products(self, query: str, limit: int = 5) -> list[Product]:
        return self.products


def _context(provider: _FixedProvider, user_text: str) -> AgentContext:
    return AgentContext(
        wa_id="919999999999",
        phone_e164="+919999999999",
        user_text=user_text,
        history=[],
        orders=[],
        is_vip=False,
        knowledge={},
        provider=provider,
        model="m",
        api_key="k",
        extra_params=None,
    )


async def test_run_recommends_from_real_search_results() -> None:
    products = [
        Product(
            gid="1",
            title="Gold Jhumka Earrings",
            handle="jhumka",
            price=Money("499", "INR"),
            available=True,
            product_type="Accessory",
            tags=(),
        )
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider(
        '{"reply": "These Gold Jhumka Earrings would pair beautifully!"}'
    )
    result = await run(_context(provider, "what goes with a red kurti"), shopify)
    assert "Jhumka" in result.text


async def test_run_answers_original_question_first_when_no_matches() -> None:
    shopify = _FakeShopify(products=[])
    provider = _FixedProvider(
        '{"reply": "I don\'t have a specific match, but I can connect you with our team."}'
    )
    result = await run(_context(provider, "what goes with a green saree"), shopify)
    assert result.text


async def test_product_data_rendered_in_system_prompt() -> None:
    """Verify core computation: product data is grounded in system prompt."""
    products = [
        Product(
            gid="1",
            title="Gold Earrings",
            handle="gold-earrings",
            price=Money("999", "INR"),
            available=True,
            product_type="Accessory",
            tags=("gold",),
        ),
        Product(
            gid="2",
            title="Silver Bracelet",
            handle="silver-bracelet",
            price=Money("599", "INR"),
            available=False,
            product_type="Accessory",
            tags=("silver",),
        ),
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider('{"reply": "We have jewelry options."}')
    await run(_context(provider, "what accessories go with a kurti"), shopify)

    # Verify products are rendered into the system prompt (first message, role="system")
    assert len(provider.captured_messages) >= 2
    system_message = provider.captured_messages[0]
    assert system_message.role == "system"
    assert "Gold Earrings" in system_message.content
    assert "999 INR" in system_message.content
    # Recommendations are always filtered to currently-available-for-sale (design spec):
    # an out-of-stock item must never reach the prompt, labelled or not.
    assert "Silver Bracelet" not in system_message.content
    assert "599 INR" not in system_message.content


async def test_all_results_out_of_stock_reads_as_no_recommendation() -> None:
    products = [
        Product(
            gid="1",
            title="Sold Out Saree",
            handle="sold-out-saree",
            price=Money("1999", "INR"),
            available=False,
            product_type="Saree",
            tags=(),
        )
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider('{"reply": "Let me connect you with our team."}')
    await run(_context(provider, "what goes with a red kurti"), shopify)
    system_message = provider.captured_messages[0]
    assert "Sold Out Saree" not in system_message.content
    assert "No matching products" in system_message.content


async def test_fallback_on_provider_error() -> None:
    """Verify ProviderError fallback branch returns safe text."""
    products = [
        Product(
            gid="1",
            title="Gold Earrings",
            handle="gold-earrings",
            price=Money("999", "INR"),
            available=True,
            product_type="Accessory",
            tags=(),
        )
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider(raises=ProviderError("API key invalid", "AUTH"))
    result = await run(_context(provider, "what goes with a red kurti"), shopify)
    # Fallback should be a non-empty string (from copy_for("error_fallback", "en"))
    assert result.text
    assert len(result.text) > 0
