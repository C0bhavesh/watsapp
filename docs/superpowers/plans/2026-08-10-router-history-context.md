# Router History Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close a known, already-specified gap: `classify_intent` currently classifies only the
customer's current message, with no conversation history, so a short/ambiguous reply (e.g. a
bare number right after the bot asked "please share your order number") often misroutes to
`customer_support` instead of `order_tracking` — silently defeating the order-number lookup
built earlier today, since it never runs when the wrong intent is chosen.

**Architecture:** Thread a small, capped slice of the already-loaded `history` (the same list
`AgentContext` already carries) into the router's classification call. This was the original
Phase 4 design spec's intent ("a fast classification call over the message plus a little recent
history") — never implemented, flagged as an Important finding in the whole-branch review, and
now confirmed by live testing. Kept deliberately small (2 messages) so the router stays a fast,
cheap call, not a full-context one.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio (existing patterns).

## Global Constraints

- No behavior change to the five specialist agents themselves — only the router's classification
  input changes.
- `classify_intent`'s new `history` parameter must be optional (default `None`), so any other
  future caller isn't forced to supply it.
- Full type hints; `mypy` strict clean; `ruff` clean; no bare `except`; no `print()`.

---

### Task 1: Thread history into router classification

**Files:**
- Modify: `backend/app/agents/router.py`
- Modify: `backend/app/core/conversation.py` (one call-site change)
- Modify: `backend/tests/agents/test_router.py`
- Modify: `backend/tests/test_whatsapp_webhook.py`

**Interfaces:**
- Consumes: `app.providers.base.Message` (unchanged), `AgentContext`/`_agent_reply`'s already-loaded `history: list[Message]` (unchanged shape).
- Produces: `classify_intent(provider, model, api_key, user_text, *, history: list[Message] | None = None, timeout: float = 10.0, extra_params: dict[str, object] | None = None) -> Intent` — signature CHANGES (new optional `history` keyword-only parameter, inserted before `timeout`). `_HISTORY_MESSAGES_FOR_ROUTING: int` — new module constant in `app/agents/router.py`.

- [ ] **Step 1: Read current state of both files before editing**

Read `backend/app/agents/router.py` and the `_agent_reply` function in
`backend/app/core/conversation.py` in full. Confirm the current `classify_intent` call site
reads exactly:

```python
    intent = await classify_intent(
        provider, model, api_key, event.text, extra_params=extra_params
    )
```

If it has drifted from this, adapt the edit to the actual current content.

- [ ] **Step 2: Update `_ROUTER_PROMPT` and add the history-window constant**

In `backend/app/agents/router.py`, replace:

```python
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
```

with:

```python
_ROUTER_PROMPT = """Classify the customer's LATEST WhatsApp message into exactly one category.
Recent conversation history, if provided, is for context only -- classify the newest customer
message, using that context to resolve short or ambiguous replies (for example, a bare number
right after the bot asked for an order number is order_tracking, not customer_support; a plain
"yes" right after the bot offered something is about whatever was just offered).

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

# How many of the most recent history messages to include for classification context -- kept
# small on purpose (the design spec calls for "a fast classification call over the message plus
# a LITTLE recent history"): enough to resolve a short/ambiguous reply against what the bot just
# asked, without ballooning the router's prompt size or latency on every single turn.
_HISTORY_MESSAGES_FOR_ROUTING = 2
```

- [ ] **Step 3: Update `classify_intent`'s signature and body**

Replace:

```python
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
```

with:

```python
async def classify_intent(
    provider: LLMProvider,
    model: str,
    api_key: str,
    user_text: str,
    *,
    history: list[Message] | None = None,
    timeout: float = 10.0,
    extra_params: dict[str, object] | None = None,
) -> Intent:
    """Classify one customer message into an Intent, using a little recent history to resolve
    short/ambiguous replies (a bare number right after the bot asked for an order number, a
    plain "yes" after an offer) that carry no signal in isolation. Any failure (provider error
    or an unparseable/unrecognized completion) degrades to customer_support -- the safe
    catch-all, never leaving a message unrouted."""
    recent_history = (history or [])[-_HISTORY_MESSAGES_FOR_ROUTING:]
    messages = [
        Message(role="system", content=_ROUTER_PROMPT),
        *recent_history,
        Message(role="user", content=user_text),
    ]
```

The rest of the function body (the `try`/`except ProviderError`/JSON-parsing block) is
unchanged — leave it exactly as-is.

- [ ] **Step 4: Update the call site in `backend/app/core/conversation.py`**

Inside `_agent_reply`, replace:

```python
    intent = await classify_intent(
        provider, model, api_key, event.text, extra_params=extra_params
    )
```

with:

```python
    intent = await classify_intent(
        provider, model, api_key, event.text, history=history, extra_params=extra_params
    )
```

(`history` is already the function's own `history: list[Message]` parameter — no new
computation needed, just threading the existing value through.)

- [ ] **Step 5: Add new tests to `backend/tests/agents/test_router.py`**

The existing six tests all call `classify_intent(provider, "m", "k", "<text>")` with no
`history` kwarg — confirm they still pass unmodified (the new parameter is optional, defaulting
to `None`). Add these three new tests to the end of the file:

```python
async def test_classify_intent_uses_history_to_resolve_a_bare_number_reply() -> None:
    """A bare number carries no signal on its own -- with the prior assistant turn asking for
    an order number, that context must reach the provider so the model can use it."""
    from app.providers.base import Message

    seen: dict[str, object] = {}

    class _RecordingProvider:
        async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
            seen["messages"] = messages
            return CompletionResult(text='{"intent": "order_tracking"}', model=model)

    history = [
        Message(role="user", content="can u tell me my order detail"),
        Message(
            role="assistant",
            content="It looks like there isn't an order linked yet. Could you share your order number?",
        ),
    ]
    result = await classify_intent(_RecordingProvider(), "m", "k", "9652", history=history)

    assert result == "order_tracking"
    sent_contents = [m.content for m in seen["messages"]]
    assert any("order number" in c for c in sent_contents)


async def test_classify_intent_caps_history_to_the_configured_window() -> None:
    """Only the most recent few history messages are sent -- the router stays a fast, cheap
    classification call, not a full-context one."""
    from app.agents.router import _HISTORY_MESSAGES_FOR_ROUTING
    from app.providers.base import Message

    seen: dict[str, object] = {}

    class _RecordingProvider:
        async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
            seen["messages"] = messages
            return CompletionResult(text='{"intent": "customer_support"}', model=model)

    history = [Message(role="user", content=f"msg{i}") for i in range(10)]
    await classify_intent(_RecordingProvider(), "m", "k", "hi", history=history)

    # system prompt + capped history + the current message
    assert len(seen["messages"]) == 1 + _HISTORY_MESSAGES_FOR_ROUTING + 1


async def test_classify_intent_with_no_history_still_works() -> None:
    provider = _FixedProvider(text='{"intent": "order_tracking"}')
    result = await classify_intent(provider, "m", "k", "where is my order", history=None)
    assert result == "order_tracking"
```

- [ ] **Step 6: Run the router unit tests**

Run: `cd backend && python -m pytest tests/agents/test_router.py -v`
Expected: all 9 tests PASS (6 existing unmodified + 3 new).

- [ ] **Step 7: Add one webhook-level integration test**

Read `backend/tests/test_whatsapp_webhook.py`'s `FakeProvider` class first (search for `class
FakeProvider`) to confirm its exact constructor/behavior before writing this test — it returns
canned responses in sequence per call, and does not itself inspect `messages` content, so this
test proves the WIRING (that `history` reaches the router's provider call during a real turn),
not the real LLM's classification judgment. Add this test near the other order-name-recovery
tests added earlier today (search for `test_post_text_event_wrong_digit_order_number_asks_customer_to_recheck`
and add this one after it):

```python
async def test_post_text_event_threads_history_into_router_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router's classify_intent call must receive the loaded conversation history, not
    just the bare current message -- otherwise a short reply like a bare order number right
    after the bot asked for one has no way to be classified correctly."""
    from app.channels.whatsapp_sender import SendResult

    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    async def fake_resolve_by_phone(*args, **kwargs):
        return []

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    class _RecordingProvider:
        def __init__(self) -> None:
            self.calls: list[list[object]] = []

        async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
            self.calls.append(messages)
            if len(self.calls) == 1:
                # The router call.
                return CompletionResult(text=json.dumps({"intent": "order_tracking"}), model=model)
            return CompletionResult(text=json.dumps({"reply": "ok", "handoff": False}), model=model)

    provider = _RecordingProvider()
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    # Seed prior history: the bot already asked for an order number.
    conversation_id = await c.conversations.get_or_create("919999999999")
    await c.conversations.append_message(conversation_id, "user", "can u tell me my order detail")
    await c.conversations.append_message(
        conversation_id, "assistant", "Could you please share your order number?"
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.routerhistory1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "9652"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    router_messages = provider.calls[0]
    contents = [m.content for m in router_messages]
    assert any("order number" in c for c in contents)
```

(Uses the module's existing `CompletionResult`, `json`, `envelope`, `post`, `sign`,
`_fake_active_llm` helpers already imported/defined elsewhere in this test file — no new
imports needed beyond `SendResult`, which other tests in this file already import inline the
same way.)

- [ ] **Step 8: Run the full backend test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 9: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/agents/router.py app/core/conversation.py
```
Expected: EMPTY output.

- [ ] **Step 10: Commit**

```bash
git add backend/app/agents/router.py backend/app/core/conversation.py backend/tests/agents/test_router.py backend/tests/test_whatsapp_webhook.py
git commit -m "feat(agents): thread recent history into router classification so short replies resolve correctly"
```
