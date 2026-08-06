from app.agents.router import classify_intent
from app.providers.base import CompletionResult, ProviderError, ProviderErrorKind


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


async def test_classify_intent_order_tracking() -> None:
    provider = _FixedProvider(text='{"intent": "order_tracking"}')
    result = await classify_intent(provider, "m", "k", "where is my order")
    assert result == "order_tracking"


async def test_classify_intent_product_search() -> None:
    provider = _FixedProvider(text='{"intent": "product_search"}')
    result = await classify_intent(provider, "m", "k", "do you have this in blue")
    assert result == "product_search"


async def test_classify_intent_policy() -> None:
    provider = _FixedProvider(text='{"intent": "policy"}')
    result = await classify_intent(provider, "m", "k", "what is your return policy")
    assert result == "policy"


async def test_classify_intent_recommendations() -> None:
    provider = _FixedProvider(text='{"intent": "recommendations"}')
    result = await classify_intent(provider, "m", "k", "what goes well with a red kurti")
    assert result == "recommendations"


async def test_classify_intent_unknown_value_falls_back_to_customer_support() -> None:
    provider = _FixedProvider(text='{"intent": "make_me_a_sandwich"}')
    result = await classify_intent(provider, "m", "k", "hi")
    assert result == "customer_support"


async def test_classify_intent_unparseable_falls_back_to_customer_support() -> None:
    provider = _FixedProvider(text="not json")
    result = await classify_intent(provider, "m", "k", "hi")
    assert result == "customer_support"


async def test_classify_intent_provider_error_falls_back_to_customer_support() -> None:
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await classify_intent(provider, "m", "k", "hi")
    assert result == "customer_support"
