from app.agents.base import AgentContext
from app.agents.policy import run
from app.providers.base import CompletionResult, Message, ProviderError


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


class _CapturingProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.captured_messages: list[Message] | None = None

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.captured_messages = messages
        return CompletionResult(text=self._text, model=model)


def _context(
    provider: _FixedProvider | _CapturingProvider,
    user_text: str,
    knowledge: dict[str, str],
    is_vip: bool = False,
) -> AgentContext:
    return AgentContext(
        wa_id="919999999999",
        phone_e164="+919999999999",
        user_text=user_text,
        history=[],
        orders=[],
        is_vip=is_vip,
        knowledge=knowledge,
        provider=provider,
        model="m",
        api_key="k",
        extra_params=None,
    )


async def test_run_grounds_reply_in_knowledge() -> None:
    knowledge = {"faq": '[{"q": "Can I cancel?", "a": "Only before dispatch."}]', "business": "{}"}
    provider = _FixedProvider('{"reply": "You can cancel only before your order is dispatched."}')
    result = await run(_context(provider, "can I cancel my order", knowledge))
    assert "dispatch" in result.text.lower()


async def test_run_missing_knowledge_key_does_not_crash() -> None:
    provider = _FixedProvider('{"reply": "Let me check that for you."}')
    result = await run(_context(provider, "what is your return policy", {}))
    assert result.text


async def test_run_renders_knowledge_in_system_prompt() -> None:
    faq_content = '[{"q": "What is the return policy?", "a": "Returns accepted within 30 days."}]'
    business_content = '{"store_name": "Thetavas", "return_window": "30 days"}'
    knowledge = {"faq": faq_content, "business": business_content}
    provider = _CapturingProvider('{"reply": "Our return window is 30 days."}')
    await run(_context(provider, "can I return items", knowledge))

    # Verify the system prompt (first message) contains the knowledge content
    assert provider.captured_messages is not None
    assert len(provider.captured_messages) >= 1
    system_message = provider.captured_messages[0]
    assert system_message.role == "system"
    assert faq_content in system_message.content
    assert business_content in system_message.content


async def test_run_renders_brand_voice_and_vip_hint_in_system_prompt() -> None:
    """The admin-editable brand_voice seed must actually reach the prompt, and a VIP's
    returning-customer hint with it."""
    from app.agents.base import VIP_HINT

    knowledge = {"faq": "[]", "business": "{}", "brand_voice": "Always close with a namaste."}
    provider = _CapturingProvider('{"reply": "Sure."}')
    await run(_context(provider, "do you ship to Pune", knowledge, is_vip=True))

    assert provider.captured_messages is not None
    system_message = provider.captured_messages[0]
    assert "Always close with a namaste." in system_message.content
    assert VIP_HINT in system_message.content


async def test_run_on_provider_error_returns_safe_fallback() -> None:
    provider = _FixedProvider(raises=ProviderError("service unavailable", "TIMEOUT"))
    knowledge: dict[str, str] = {"faq": "[]", "business": "{}"}
    result = await run(_context(provider, "what is your return policy", knowledge))
    assert "team" in result.text
