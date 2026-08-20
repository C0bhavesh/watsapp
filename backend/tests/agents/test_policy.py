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


async def test_run_fallback_uses_the_context_language() -> None:
    from dataclasses import replace

    from app.channels.copy import copy_for

    provider = _FixedProvider(raises=ProviderError("service unavailable", "TIMEOUT"))
    context = replace(
        _context(provider, "what is your return policy", {"faq": "[]", "business": "{}"}),
        language="gu",
    )
    result = await run(context)
    assert result.text == copy_for("error_fallback", "gu")


async def test_model_handoff_flag_is_honored() -> None:
    """The prompt tells the model to offer to connect the customer with the team when the
    answer isn't covered -- without the handoff flag in the contract that offer was a promise
    nothing could keep (the AI just answered again on the next turn)."""
    provider = _FixedProvider(
        '{"reply": "I am not certain -- let me connect you with our team.", "handoff": true}'
    )
    result = await run(_context(provider, "do you price match", {"faq": "[]", "business": "{}"}))
    assert result.handoff is True


async def test_reply_without_handoff_flag_does_not_hand_off() -> None:
    provider = _FixedProvider('{"reply": "Returns are accepted within 7 days."}')
    result = await run(_context(provider, "return window", {"faq": "[]", "business": "{}"}))
    assert result.handoff is False


async def test_system_prompt_requests_the_handoff_field() -> None:
    provider = _CapturingProvider('{"reply": "Sure.", "handoff": false}')
    await run(_context(provider, "return window", {"faq": "[]", "business": "{}"}))
    assert provider.captured_messages is not None
    assert '"handoff"' in provider.captured_messages[0].content


async def test_system_prompt_directs_uncovered_questions_to_contact_email() -> None:
    """When the policy knowledge does not cover a question, the model is told to give the real
    contact email info@thetavas.com rather than a vague "we'll connect you with the team"
    promise (owner-requested). The email is stated in the English system instruction; the model
    localises the surrounding reply into the customer's language per the shared personality."""
    provider = _CapturingProvider('{"reply": "Sure.", "handoff": false}')
    await run(_context(provider, "do you gift wrap", {"faq": "[]", "business": "{}"}))
    assert provider.captured_messages is not None
    system_prompt = provider.captured_messages[0].content
    assert "info@thetavas.com" in system_prompt
    # The vague "connect you with the team" promise for the uncovered-answer case is gone.
    assert "connect them with the team" not in system_prompt
