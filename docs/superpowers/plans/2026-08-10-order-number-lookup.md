# Order-Number Lookup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing (Phase 5) order-name recovery mechanism in `app/core/conversation.py`
so a customer can be helped by an order number they type, even when they already have other
orders linked, with bare-digit and `#`-prefixed forms recognized and a 4-digit format check that
avoids a wasted Shopify call on an obviously-malformed number.

**Architecture:** Broaden `_extract_order_number_candidate`'s matching (three patterns tried in
priority order: `tavas<digits>`, `#<digits>`, bare 3+ digit run), add a digit-length check backed
by a named constant in `app/shopify/models.py`, change `_recover_order_by_name` to always run for
`order_tracking` intent (not only when the phone lookup found nothing) and to return a format
hint instead of querying Shopify when the digit count is wrong, thread that hint through
`AgentContext` into `order_tracking`'s prompt.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio (existing patterns in this codebase).

## Global Constraints

- Critical Rule 2 (LLM never mutates) — untouched; no new Shopify write paths.
- Critical Rule 3 (ownership check before revealing anything, always re-fetch live) — enforced
  entirely by the existing `resolve_by_order_name`/`AuthorizedOrder` chain; do not bypass or
  duplicate it.
- No new secrets, no new admin-panel config, no schema/migration changes.
- Full type hints on every function signature; `mypy` strict clean; `ruff` clean; no bare
  `except`; no `print()`.
- Design spec: `docs/superpowers/specs/2026-08-10-order-number-lookup-design.md`.

---

### Task 1: Extend order-number extraction, format validation, and the conversation wiring

**Files:**
- Modify: `backend/app/shopify/models.py` (add `ORDER_NUMBER_DIGIT_LENGTH` near `normalize_order_name`)
- Modify: `backend/app/core/conversation.py` (extraction, `_recover_order_by_name`, `_agent_reply`)
- Modify: `backend/app/agents/base.py` (`AgentContext` gains `order_number_format_hint`)
- Modify: `backend/app/agents/order_tracking.py` (prompt template + `run()`)
- Modify: `backend/tests/core/test_conversation.py` (rewrite for new signature + new cases)
- Modify: `backend/tests/test_whatsapp_webhook.py` (rewrite the "skips" test, add 3 new tests)
- Modify: `backend/tests/agents/test_order_tracking.py` (format-hint rendering)

**Interfaces:**
- Consumes: existing `resolve_by_order_name(shopify: OrderSource, wa_id: str, raw_name: str) -> AuthorizedOrder | None` (`app/core/order_resolver.py`, unchanged), existing `normalize_order_name(raw: str, prefix: str = "tavas") -> str` (`app/shopify/models.py`, unchanged).
- Produces: `ORDER_NUMBER_DIGIT_LENGTH: int` (`app/shopify/models.py`) — importable by any module that needs the store's order-number digit count. `_recover_order_by_name(shopify: OrderSource, wa_id: str, text: str) -> tuple[list[AuthorizedOrder], str | None]` (`app/core/conversation.py`) — signature CHANGES from the current `-> list[AuthorizedOrder]`. `AgentContext.order_number_format_hint: str | None = None` (`app/agents/base.py`) — new field, must be added AFTER `timeout: float = 20.0` (the last existing defaulted field) to keep valid dataclass field ordering.

- [ ] **Step 1: Read the current state of every file before editing**

Read `backend/app/shopify/models.py`, `backend/app/core/conversation.py`,
`backend/app/agents/base.py`, `backend/app/agents/order_tracking.py`,
`backend/tests/core/test_conversation.py`, and the two existing order-name-recovery tests in
`backend/tests/test_whatsapp_webhook.py` (search for `resolve_by_order_name` — there are two
test functions, one named `test_post_text_event_recovers_owned_order_from_order_name_when_phone_has_none`
which needs NO changes, and one named `test_post_text_event_with_phone_orders_skips_order_name_recovery`
which must be REPLACED per Step 6 below, since its core assertion — that the order-name scan
must not run when the phone path already found orders — describes the OLD behavior this task
changes). Confirm line numbers/exact current content match what this plan assumes before editing;
if anything has drifted, adapt the edits to the actual current content rather than blindly
applying a diff.

- [ ] **Step 2: Add the digit-length constant to `app/shopify/models.py`**

Find `normalize_order_name` in this file (it strips a leading `#`, lowercases, and prepends
`prefix` to a bare digit string). Add this constant directly above it:

```python
# Tavas order numbers are exactly this many digits today (confirmed live: tavas3898,
# tavas9652). Shopify order numbers are sequential, so this WILL need bumping once the store's
# order count crosses 9999 -- a "true today" fact, not a permanent assumption.
ORDER_NUMBER_DIGIT_LENGTH = 4


def normalize_order_name(raw: str, prefix: str = "tavas") -> str:
    ...  # unchanged, leave the existing body exactly as-is
```

- [ ] **Step 3: Broaden extraction in `app/core/conversation.py`**

Find the existing block (currently around lines 55-61):

```python
# Thetavas order names look like "tavas3733" -- the store prefix + the order number, with NO
# "#" prefix (see find_order_by_name / phase0-verification-results). Conservative match anchored
# to that prefix, case-insensitive; `resolve_by_order_name` re-guards the extracted value
# (injection guard + live ownership check), so this only has to surface a candidate token. The
# `[0-9]+` (not `\d+`) and single search keep it linear on arbitrarily long / non-ASCII input --
# no catastrophic backtracking and no Unicode-digit surprises.
_ORDER_NAME_RE = re.compile(r"tavas[0-9]+", re.IGNORECASE)
```

Replace it with:

```python
# Thetavas order names look like "tavas3733" -- the store prefix + the order number, with NO
# "#" prefix (see find_order_by_name / phase0-verification-results). Tried in priority order:
# an explicit "tavas<digits>" or "#<digits>" token is an unambiguous order-number attempt at
# any digit count; a bare run of digits only counts as a candidate at 3+ digits, since shorter
# numbers (dates, quantities) are far more likely to be something else. `[0-9]` (not `\d`) and
# a single search each keep this linear on arbitrarily long / non-ASCII input -- no
# catastrophic backtracking and no Unicode-digit surprises. `resolve_by_order_name` re-guards
# whatever candidate is extracted here (injection guard + live ownership check), so this only
# has to surface a plausible token.
_TAVAS_PREFIXED_RE = re.compile(r"tavas[0-9]+", re.IGNORECASE)
_HASH_PREFIXED_RE = re.compile(r"#[0-9]+")
_BARE_DIGITS_RE = re.compile(r"\b[0-9]{3,}\b")


def _extract_order_number_candidate(text: str) -> str | None:
    """Find the first plausible order-number token in free text, tried in priority order."""
    for pattern in (_TAVAS_PREFIXED_RE, _HASH_PREFIXED_RE, _BARE_DIGITS_RE):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None
```

Update the import line near the top of the file — find:

```python
from app.shopify.models import AuthorizedOrder
```

Replace with:

```python
from app.shopify.models import ORDER_NUMBER_DIGIT_LENGTH, AuthorizedOrder
```

- [ ] **Step 4: Rewrite `_recover_order_by_name`**

Find the existing function (currently around lines 222-241):

```python
async def _recover_order_by_name(
    shopify: OrderSource, wa_id: str, text: str
) -> list[AuthorizedOrder]:
    """Recover an order the sender OWNS from an order-name token in their own message.

    Used only as a fallback when the phone lookup found nothing: a customer whose WhatsApp
    number is not mapped to any order can still be helped if they type their order number.
    ``resolve_by_order_name`` re-fetches the order live and enforces ownership, returning None
    both for "no such order" AND for "belongs to a different number" -- so nothing is revealed
    for an order the sender does not own, and a reply can never be used to enumerate order
    numbers. No token found (or an unowned one) returns an empty list, leaving the turn
    unchanged (the agent asks for the number / hands off as before). This makes the multi-turn
    case work for free: "where is my order" (no number -> the agent asks) then "tavas1234" (that
    reply is scanned on its own turn).
    """
    match = _ORDER_NAME_RE.search(text)
    if match is None:
        return []
    order = await resolve_by_order_name(shopify, wa_id, match.group(0))
    return [order] if order is not None else []
```

Replace it with:

```python
async def _recover_order_by_name(
    shopify: OrderSource, wa_id: str, text: str
) -> tuple[list[AuthorizedOrder], str | None]:
    """Recover an order the sender OWNS from an order-name token in their own message, and flag
    a number-shaped token that doesn't match the store's order-ID format.

    Runs on every order_tracking turn, not only when the phone lookup found nothing: a customer
    can own more than one order, or one placed under different contact info, so a message
    mentioning a different order number should still be checked even when the phone path already
    found something. ``resolve_by_order_name`` re-fetches the order live and enforces ownership,
    returning None both for "no such order" AND for "belongs to a different number" -- so
    nothing is revealed for an order the sender does not own, and a reply can never be used to
    enumerate order numbers.

    Returns ``(orders, format_hint)``: ``format_hint`` is set (and ``orders`` is empty) when the
    extracted candidate's digit count doesn't match ``ORDER_NUMBER_DIGIT_LENGTH`` -- Shopify is
    never queried for a token already known to be the wrong shape; the caller threads the hint
    to the agent so it can ask the customer to double-check their order ID instead of silently
    finding nothing, which reads to the customer as "that order doesn't exist."
    """
    candidate = _extract_order_number_candidate(text)
    if candidate is None:
        return [], None
    digits = re.sub(r"[^0-9]", "", candidate)
    if len(digits) != ORDER_NUMBER_DIGIT_LENGTH:
        return [], (
            f"The customer mentioned a number that doesn't match our order ID format (ours "
            f"are exactly {ORDER_NUMBER_DIGIT_LENGTH} digits, e.g. "
            f"tavas{'9' * ORDER_NUMBER_DIGIT_LENGTH}) -- ask them to double-check and resend "
            f"their order ID."
        )
    order = await resolve_by_order_name(shopify, wa_id, candidate)
    return ([order] if order is not None else []), None
```

- [ ] **Step 5: Update `_agent_reply`'s order-resolution block**

Find the existing block inside `_agent_reply` (currently around lines 261-269):

```python
    orders: list[AuthorizedOrder] = []
    if intent == "order_tracking":
        orders = await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
        if not orders:
            # The sender's WhatsApp number maps to no order -- fall back to recovering one they
            # OWN from the order number in THIS message (ownership re-checked, live-refetched,
            # non-enumerable inside resolve_by_order_name). Only reached when the phone path is
            # empty, so a customer with mapped orders never pays the extra lookup.
            orders = await _recover_order_by_name(c.shopify, event.wa_id, event.text)
```

Replace it with:

```python
    orders: list[AuthorizedOrder] = []
    order_number_format_hint: str | None = None
    if intent == "order_tracking":
        orders = await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
        # Always attempted, not only when the phone path found nothing: a customer can own
        # more than one order, or ask about one placed under different contact info.
        # Ownership re-checked, live-refetched, non-enumerable inside resolve_by_order_name.
        extra_orders, order_number_format_hint = await _recover_order_by_name(
            c.shopify, event.wa_id, event.text
        )
        for extra in extra_orders:
            if not any(o.order.name == extra.order.name for o in orders):
                orders.append(extra)
```

Then find the `AgentContext(...)` construction a few lines below (still inside `_agent_reply`)
and add one new keyword argument, right after the existing `language=controls.default_language,`
line:

```python
        language=controls.default_language,
        order_number_format_hint=order_number_format_hint,
    )
```

- [ ] **Step 6: Add the new field to `AgentContext`**

In `backend/app/agents/base.py`, find the `AgentContext` dataclass's last field:

```python
    timeout: float = 20.0
```

Add immediately after it:

```python
    timeout: float = 20.0
    # Set by conversation.py when the customer's message contained a number-shaped token that
    # doesn't match the store's order-ID digit count -- lets order_tracking ask the customer to
    # double-check it instead of silently treating the turn as "no order mentioned."
    order_number_format_hint: str | None = None
```

- [ ] **Step 7: Thread the hint into `order_tracking`'s prompt**

In `backend/app/agents/order_tracking.py`, find `_SYSTEM_TEMPLATE`:

```python
_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}

Store cancellation policy: orders can only be cancelled BEFORE they are dispatched. Once
dispatched, cancellation is not possible -- if the customer asks to cancel a dispatched order,
tell them clearly and do not offer a cancel option for it.

If the customer wants to cancel an order that IS still eligible, tell them you'll bring up a
Confirm/Cancel button for them to tap -- you never cancel anything yourself.

{contract}
"""
```

Replace with (only the blank line after `{order_context}` becomes `{format_hint}`, everything
else identical):

```python
_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}
{format_hint}
Store cancellation policy: orders can only be cancelled BEFORE they are dispatched. Once
dispatched, cancellation is not possible -- if the customer asks to cancel a dispatched order,
tell them clearly and do not offer a cancel option for it.

If the customer wants to cancel an order that IS still eligible, tell them you'll bring up a
Confirm/Cancel button for them to tap -- you never cancel anything yourself.

{contract}
"""
```

Find `run()`'s `system_prompt = _SYSTEM_TEMPLATE.format(...)` call:

```python
    fallback = copy_for("error_fallback", context.language)
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, context.reveal_fields),
        contract=HANDOFF_JSON_CONTRACT,
    )
```

Replace with:

```python
    fallback = copy_for("error_fallback", context.language)
    format_hint = (
        f"\n{context.order_number_format_hint}\n" if context.order_number_format_hint else ""
    )
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, context.reveal_fields),
        format_hint=format_hint,
        contract=HANDOFF_JSON_CONTRACT,
    )
```

- [ ] **Step 8: Rewrite `backend/tests/core/test_conversation.py`**

Replace the entire file content with:

```python
"""Unit tests for the order-name recovery fallback in the conversation pipeline.

Covers ``_recover_order_by_name`` -- the helper that lets a customer be helped by the order
number they type, whether or not their WhatsApp number is already mapped to an order.
Ownership is enforced by ``resolve_by_order_name`` (returning None both for "no such order" and
"not this sender's order"), so these tests pin: an owned token is recovered, an unowned/absent
token reveals nothing, extraction is safe on huge/hostile input, and a number-shaped token of
the wrong digit count never reaches Shopify at all -- it produces a format hint instead.
"""

from app.core.conversation import _recover_order_by_name
from app.shopify.models import ORDER_NUMBER_DIGIT_LENGTH, Order


def _order(gid: str, name: str, phone: str | None) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeShopify:
    """Only ``find_order_by_name`` is exercised by the recovery path; the rest satisfy the
    structural ``OrderSource`` shape without being called."""

    def __init__(self, orders_by_name: dict[str, Order] | None = None) -> None:
        self.orders_by_name = orders_by_name or {}
        self.calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        return None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        self.calls.append(raw_name)
        return self.orders_by_name.get(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


async def test_recover_order_by_name_returns_owned_order() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "hi where is tavas5432")

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://5"
    assert orders[0].verified_phone == "+919999999999"
    assert hint is None


async def test_recover_order_by_name_unowned_returns_empty() -> None:
    # The order exists but belongs to a different phone -> resolve_by_order_name returns None,
    # so nothing is revealed (and the None is indistinguishable from "no such order").
    shopify = _FakeShopify(
        orders_by_name={"tavas6543": _order("gid://6", "tavas6543", "+911111111111")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas6543")

    assert orders == []
    assert hint is None


async def test_recover_order_by_name_no_token_returns_empty() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is my order please")

    assert orders == []
    assert hint is None


async def test_recover_order_by_name_extracts_token_from_long_hostile_text() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )
    hostile = (
        "\U0001f600" * 5000 + "\n\x00 TAVAS no-digits here " + "tavas5432"
        + " ' \" ; -- OR 1=1 " + "力" * 5000
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", hostile)

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://5"
    assert hint is None


async def test_recover_order_by_name_bare_digits_normalize_to_tavas_prefix() -> None:
    # A bare 4-digit number (no "tavas"/"#" prefix) is still a valid candidate -- passed through
    # as-is to resolve_by_order_name, which normalizes it exactly like the real Shopify client
    # does (bare digits -> "tavas" + digits).
    shopify = _FakeShopify(
        orders_by_name={"9652": _order("gid://9652", "tavas9652", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "my order number is 9652")

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://9652"
    assert hint is None


async def test_recover_order_by_name_hash_prefixed() -> None:
    shopify = _FakeShopify(
        orders_by_name={"#9652": _order("gid://9652", "tavas9652", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "order #9652 please")

    assert len(orders) == 1
    assert hint is None


async def test_recover_order_by_name_short_bare_number_is_not_a_candidate() -> None:
    # Below the 3-digit floor for a bare (unprefixed) number -- dates/quantities/etc. should not
    # be mistaken for an order-number attempt.
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "I ordered 2 shirts")

    assert orders == []
    assert hint is None
    assert shopify.calls == []


async def test_recover_order_by_name_wrong_digit_count_never_calls_shopify() -> None:
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "my order id is 965")

    assert orders == []
    assert hint is not None
    assert str(ORDER_NUMBER_DIGIT_LENGTH) in hint
    assert shopify.calls == []


async def test_recover_order_by_name_tavas_prefixed_wrong_digit_count_never_calls_shopify() -> None:
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas96522")

    assert orders == []
    assert hint is not None
    assert shopify.calls == []
```

- [ ] **Step 9: Run the new unit tests**

Run: `cd backend && python -m pytest tests/core/test_conversation.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 10: Update `backend/tests/test_whatsapp_webhook.py`**

First, confirm `test_post_text_event_recovers_owned_order_from_order_name_when_phone_has_none`
(search for that name) needs NO changes — trace it by hand: message `"hey where is tavas4242"`
→ `_extract_order_number_candidate` matches `_TAVAS_PREFIXED_RE` → `"tavas4242"` → digits
`"4242"`, length 4, matches `ORDER_NUMBER_DIGIT_LENGTH` → calls
`resolve_by_order_name(shopify, wa_id, "tavas4242")` exactly as the test's fake expects. Leave
this test exactly as-is.

Then find `test_post_text_event_with_phone_orders_skips_order_name_recovery` (search for that
name) and DELETE it entirely (its assertion describes behavior this task intentionally changes).
Replace it with these two tests, inserted in its place:

```python
async def test_post_text_event_with_phone_orders_also_recovers_a_different_mentioned_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A customer with a phone-mapped order can still ask about a DIFFERENT order they own (e.g.
    placed under different contact info) -- the order-name scan runs regardless of whether the
    phone path already found something, and the recovered order is added alongside it."""
    from app.agents.base import AgentReply

    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    phone_order = _owned_order("gid://mapped", "tavas1000")
    other_order = _owned_order("gid://99", "tavas4242")

    async def fake_resolve_by_phone(*args, **kwargs):
        return [phone_order]

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    async def fake_resolve_by_order_name(shopify, wa_id, raw_name):
        seen["raw_name"] = raw_name
        return other_order

    monkeypatch.setattr(
        "app.core.conversation.resolve_by_order_name", fake_resolve_by_order_name
    )

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["orders"] = list(context.orders)
        return AgentReply(text="ok")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover2",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what about tavas4242 too"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["raw_name"] == "tavas4242"
    assert seen["orders"] == [phone_order, other_order]


async def test_post_text_event_does_not_duplicate_an_order_already_found_by_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the mentioned order number is the SAME one the phone path already found, it must not
    be appended a second time."""
    from app.agents.base import AgentReply

    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    phone_order = _owned_order("gid://mapped", "tavas4242")

    async def fake_resolve_by_phone(*args, **kwargs):
        return [phone_order]

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    async def fake_resolve_by_order_name(shopify, wa_id, raw_name):
        return _owned_order("gid://mapped", "tavas4242")

    monkeypatch.setattr(
        "app.core.conversation.resolve_by_order_name", fake_resolve_by_order_name
    )

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["orders"] = list(context.orders)
        return AgentReply(text="ok")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover3",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "where is tavas4242"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["orders"] == [phone_order]


async def test_post_text_event_wrong_digit_order_number_asks_customer_to_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A number-shaped token of the wrong digit count never reaches Shopify -- the agent gets a
    format hint instead, so it can ask the customer to double-check their order ID."""
    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    async def fake_resolve_by_phone(*args, **kwargs):
        return []

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("Shopify was queried for a token already known to be malformed")

    monkeypatch.setattr("app.core.conversation.resolve_by_order_name", must_not_be_called)

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["hint"] = context.order_number_format_hint
        from app.agents.base import AgentReply

        return AgentReply(text="Could you double check your order ID?")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover4",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "my order id is 965"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["hint"] is not None
```

- [ ] **Step 11: Add a format-hint rendering test to `backend/tests/agents/test_order_tracking.py`**

Read the existing file first to match its exact fixture/fake-provider style (it already has
tests for `reveal_fields` gating that follow the same shape: build a context, call `run`, inspect
what the fake provider was sent). Add one test following that same pattern: build an
`AgentContext` with `order_number_format_hint` set to a non-empty string, call `order_tracking.run`,
and assert the fake provider's captured system-prompt message contains that exact hint string.
Add a second test with `order_number_format_hint=None` (the default) and assert the hint text is
absent from the system prompt.

- [ ] **Step 12: Run the full backend test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS (no failures, no new skips beyond the existing Postgres-gated ones).

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 13: Compliance grep on every touched `app/` file**

Run (from `backend/`), once per touched file under `app/`:
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/shopify/models.py app/core/conversation.py app/agents/base.py app/agents/order_tracking.py
```
Expected: EMPTY output on every file.

- [ ] **Step 14: Commit**

```bash
git add backend/app/shopify/models.py backend/app/core/conversation.py backend/app/agents/base.py backend/app/agents/order_tracking.py backend/tests/core/test_conversation.py backend/tests/test_whatsapp_webhook.py backend/tests/agents/test_order_tracking.py
git commit -m "feat(core): recognize bare/#-prefixed order numbers, validate digit count, and always check a mentioned order number regardless of phone-linked orders"
```
