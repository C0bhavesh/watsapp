import json

from app.agents.base import AgentContext
from app.agents.customer_support import HANDOFF_MESSAGE, run
from app.providers.base import CompletionResult, Message, ProviderError, ProviderErrorKind


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises
        self.called = False

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.called = True
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
    knowledge: dict[str, str] | None = None,
) -> AgentContext:
    return AgentContext(
        wa_id="919999999999",
        phone_e164="+919999999999",
        user_text=user_text,
        history=[],
        orders=[],
        is_vip=False,
        knowledge=knowledge or {},
        provider=provider,
        model="m",
        api_key="k",
        extra_params=None,
    )


async def test_greeting_replies_normally_no_handoff() -> None:
    provider = _FixedProvider(
        text='{"reply": "Hello! How can I help you today?", "handoff": false}'
    )
    result = await run(_context(provider, "hi"))
    assert result.handoff is False
    assert result.text == "Hello! How can I help you today?"


async def test_first_human_request_hands_off_immediately_without_calling_the_llm() -> None:
    """No free first attempt: the first explicit ask for a person hands off (design spec)."""
    provider = _FixedProvider(text="should not be called")
    result = await run(_context(provider, "I want to talk to a human"))
    assert result.handoff is True
    assert result.text == HANDOFF_MESSAGE
    assert provider.called is False


async def test_model_judged_handoff_is_honored() -> None:
    """The multilingual path: the phrase list misses "मुझे किसी व्यक्ति से बात करनी है",
    but the model's own handoff judgment still escalates."""
    provider = _FixedProvider(
        text=json.dumps({"reply": "Main aapko team se connect karta hoon.", "handoff": True})
    )
    result = await run(_context(provider, "मुझे किसी व्यक्ति से बात करनी है"))
    assert result.handoff is True
    assert "Main aapko team se connect karta hoon." in result.text
    assert HANDOFF_MESSAGE in result.text


async def test_model_handoff_flag_as_string_true_is_honored() -> None:
    provider = _FixedProvider(text='{"reply": "Let me get someone.", "handoff": "true"}')
    assert (await run(_context(provider, "kisi se baat karwao"))).handoff is True


async def test_model_handoff_flag_as_string_false_does_not_hand_off() -> None:
    provider = _FixedProvider(text='{"reply": "Happy to help!", "handoff": "false"}')
    assert (await run(_context(provider, "hi"))).handoff is False


async def test_missing_handoff_key_does_not_hand_off() -> None:
    provider = _FixedProvider(text='{"reply": "Happy to help!"}')
    assert (await run(_context(provider, "hi"))).handoff is False


async def test_provider_failure_triggers_handoff() -> None:
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await run(_context(provider, "hi"))
    assert result.handoff is True
    assert HANDOFF_MESSAGE in result.text


async def test_safe_fallback_degradation_also_triggers_handoff() -> None:
    """The LLM's own fallback (unparseable/broken JSON reply) is "could not help" too."""
    provider = _FixedProvider(text="```{not valid json")
    result = await run(_context(provider, "hi"))
    assert result.handoff is True
    assert HANDOFF_MESSAGE in result.text


async def test_run_renders_knowledge_in_system_prompt() -> None:
    """Defensive fix: customer_support must carry the same faq/business knowledge slot policy.py
    already has, so a future router miss on a policy-adjacent message doesn't strip policy
    content again (mirrors test_policy.py::test_run_renders_knowledge_in_system_prompt)."""
    faq_content = '[{"q": "What is the return policy?", "a": "Returns accepted within 30 days."}]'
    business_content = '{"store_name": "Thetavas", "return_window": "30 days"}'
    knowledge = {"faq": faq_content, "business": business_content}
    provider = _CapturingProvider('{"reply": "Our return window is 30 days.", "handoff": false}')
    await run(_context(provider, "can I return items", knowledge))

    assert provider.captured_messages is not None
    system_message = provider.captured_messages[0]
    assert system_message.role == "system"
    assert faq_content in system_message.content
    assert business_content in system_message.content
