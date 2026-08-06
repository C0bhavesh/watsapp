from app.agents.base import AgentContext
from app.agents.product_search import run
from app.providers.base import CompletionResult, Message
from app.shopify.models import Money, Product


class _FixedProvider:
    def __init__(self, text: str) -> None:
        self._text = text
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
        self.captured_messages = messages
        return CompletionResult(text=self._text, model=model)


class _FakeShopify:
    def __init__(
        self,
        products: list[Product] | None = None,
        raises: Exception | None = None,
        results: list[list[Product] | None] | None = None,
    ) -> None:
        self.products = products or []
        self.raises = raises
        self.results = results  # scripted per-call results, when a test needs a retry
        self.queries: list[str] = []
        self.last_query: str | None = None

    async def search_products(self, query: str, limit: int = 5) -> list[Product] | None:
        self.queries.append(query)
        self.last_query = query
        if self.raises:
            raise self.raises
        if self.results is not None:
            return self.results[min(len(self.queries) - 1, len(self.results) - 1)]
        return self.products


def _context(provider: _FixedProvider, user_text: str) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=[], is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None,
    )


async def test_run_grounds_reply_in_search_results() -> None:
    products = [
        Product(gid="1", title="Blue Chikankari Kurti", handle="blue-kurti",
                price=Money("1299", "INR"), available=True, product_type="Kurti", tags=())
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider('{"reply": "Yes, we have a Blue Chikankari Kurti for 1299 INR."}')
    result = await run(_context(provider, "do you have anything blue"), shopify)
    assert "Blue Chikankari Kurti" in result.text


async def test_run_with_no_results_still_replies() -> None:
    shopify = _FakeShopify(products=[])
    provider = _FixedProvider(
        '{"reply": "I could not find that, let me connect you with our team."}'
    )
    result = await run(_context(provider, "do you have a green saree"), shopify)
    assert result.text


async def test_run_on_shopify_outage_still_replies_without_crashing() -> None:
    from app.shopify.errors import ShopifyUnavailable

    shopify = _FakeShopify(raises=ShopifyUnavailable("down"))
    provider = _FixedProvider('{"reply": "Let me connect you with our team for that."}')
    result = await run(_context(provider, "do you have a red kurti"), shopify)
    assert result.text


def _product(title: str) -> Product:
    return Product(
        gid="1", title=title, handle="h", price=Money("1299", "INR"), available=True,
        product_type="Kurti", tags=(),
    )


async def test_empty_first_search_broadens_once_by_dropping_the_last_word() -> None:
    shopify = _FakeShopify(results=[[], [_product("Red Silk Kurti")]])
    provider = _FixedProvider('{"reply": "Here is what I found."}')
    await run(_context(provider, "red silk kurti xxl"), shopify)
    assert shopify.queries == ["red silk kurti xxl", "red silk kurti"]
    assert "Red Silk Kurti" in provider.captured_messages[0].content


async def test_broadened_search_also_empty_stops_after_one_retry() -> None:
    shopify = _FakeShopify(results=[[], []])
    provider = _FixedProvider('{"reply": "I could not find that."}')
    await run(_context(provider, "green polka dot lehenga"), shopify)
    assert len(shopify.queries) == 2  # one retry only, never a third round
    assert "No matching products" in provider.captured_messages[0].content


async def test_no_broadening_when_the_first_search_has_results() -> None:
    shopify = _FakeShopify(results=[[_product("Blue Kurti")]])
    provider = _FixedProvider('{"reply": "We have a Blue Kurti."}')
    await run(_context(provider, "blue kurti"), shopify)
    assert shopify.queries == ["blue kurti"]


async def test_single_word_query_is_not_broadened() -> None:
    shopify = _FakeShopify(results=[[]])
    provider = _FixedProvider('{"reply": "Nothing found."}')
    await run(_context(provider, "kurti"), shopify)
    assert shopify.queries == ["kurti"]  # nothing left to drop


async def test_unsearchable_message_is_never_searched_and_says_so() -> None:
    shopify = _FakeShopify(results=[[]])
    provider = _FixedProvider('{"reply": "What are you looking for?"}')
    await run(_context(provider, '"" ***'), shopify)
    assert shopify.queries == []  # nothing searchable survived sanitization
    system_prompt = provider.captured_messages[0].content
    assert "No matching products" not in system_prompt  # never a fabricated negative
    assert "did not contain" in system_prompt


async def test_product_data_rendered_in_system_prompt() -> None:
    """Verify core computation: product data is grounded in system prompt."""
    products = [
        Product(
            gid="1", title="Red Silk Kurti", handle="red-silk-kurti",
            price=Money("2500", "INR"), available=True, product_type="Kurti",
            tags=("silk", "red")
        ),
        Product(
            gid="2", title="Red Cotton Kurti", handle="red-cotton-kurti",
            price=Money("1200", "INR"), available=False, product_type="Kurti",
            tags=("cotton", "red")
        ),
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider('{"reply": "We have red kurtis."}')
    await run(_context(provider, "do you have red kurtis"), shopify)

    # Verify products are rendered into the system prompt (second message, role="system")
    assert len(provider.captured_messages) >= 2
    system_message = provider.captured_messages[0]
    assert system_message.role == "system"
    assert "Red Silk Kurti" in system_message.content
    assert "2500 INR" in system_message.content
    assert "in stock" in system_message.content
    assert "Red Cotton Kurti" in system_message.content
    assert "1200 INR" in system_message.content
    assert "currently out of stock" in system_message.content
