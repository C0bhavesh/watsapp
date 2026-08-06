# Phase 4 — Subagent Conversation Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the router + five-specialist conversation engine described in
`docs/superpowers/specs/2026-08-06-phase4-subagent-architecture-design.md` — a customer
WhatsApps the store and gets a grounded, in-character reply for order status, product search,
store policy, recommendations, or general help/handoff, without ever hallucinating a product,
mutating an order, or revealing data to the wrong phone number.

**Architecture:** Every fresh `InboundText` event runs exactly two LLM calls: a router that
classifies intent into one of five buckets, then the matching specialist agent under
`app/agents/` that produces the reply directly. Supersedes and replaces
`docs/superpowers/plans/2026-08-04-phase4-conversation-engine.md` (a single inline-prompt
design) — do not resume that plan; several of its foundational pieces (order resolution,
markdown sanitizing, conversation memory) are reused here in full, mined from that document
rather than reinvented.

**Tech Stack:** Same as Phases 1–3.5 — FastAPI, Pydantic v2, httpx, asyncpg (Postgres) /
in-memory, LiteLLM (already wired), pytest + pytest-asyncio, ruff, mypy strict.

## Global Constraints

- Python 3.12+ syntax, full type hints, `mypy --strict` clean, `ruff check .` clean — run
  against the WHOLE project (`backend/`), every task.
- Secrets grep after every new/modified file:
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+"`
  must return EMPTY.
- No `print()` — use `logging`, matching `app.admin`'s and `app.channels.whatsapp`'s existing
  `logging.getLogger(...)` pattern.
- No bare `except:` — catch specific exception types (`ShopifyError`, `ProviderError`, etc.).
- **The LLM never mutates anything.** No agent ever calls `tagsAdd` or `orderCancel`. A
  cancel-intent reply only tells the customer a Confirm/Cancel button is coming — the actual
  mutation stays behind a deterministic button tap, Phase 5's job.
- **Ownership check before revealing anything.** `order_tracking` only ever sees orders that
  already passed through `AuthorizedOrder`'s runtime-enforced invariant (Phase 1). No agent
  constructs one itself — `order_resolver` is the only place that does.
- **Never leak a raw completion, raw JSON, or a raw exception to the customer.** Every agent
  degrades to the existing `copy_for("error_fallback", "en")` string (from
  `app/channels/copy.py`) on any provider failure or unparseable output.
- **Never hallucinate a product.** `product_search` and `recommendations` may only describe
  products present in an actual `search_products` result — this is enforced by what data is
  placed in the prompt, not by an after-the-fact check.
- **Cross-selling is `recommendations`-only.** No other agent appends its own product
  suggestion — explicit brainstorming decision, not an oversight.
- **VIP status uses `order_count` only** (from `order_mappings`, always available).
  `total_spend` is never pre-computed — corrected during planning after the original spec
  draft assumed it could be. Total/order-count are never *stated* to the customer unprompted;
  this rule lives in the shared personality prompt, not per-agent.
- **Cancellation is dispatch-based, not payment-based** — before dispatch only, matches the
  real Shipping Policy text, corrects the superseded plan's `financial_status`-only check.
- No git push — local commits only, `Co-Authored-By: Claude <noreply@anthropic.com>` trailer.

## File Structure

```
backend/
  app/store/base.py                  # + find_mappings_by_phone, count_orders_by_phone,
                                      #   StoredMessage, ConversationStore (incl. pause/handoff)
                                      #   (modify)
  app/store/memory.py                # + InMemoryConversationStore, in-memory impls (modify)
  app/store/postgres.py              # + PostgresConversationStore, postgres impls (modify)
  app/store/schema.sql               # + ALTER conversations ADD handoff_attempted_at (modify)
  app/core/order_resolver.py         # OrderSource protocol, resolve_by_phone/by_order_name (create)
  app/core/sanitize.py               # strip_markdown (create)
  app/core/memory.py                 # load_history, persist_turn (create)
  app/admin/controls.py              # + vip_order_count_threshold (modify)
  app/admin/static/{index.html,admin.js}  # + VIP threshold panel field (modify)
  app/shopify/client.py              # + search_products (modify)
  app/shopify/models.py              # + Product dataclass (modify)
  app/agents/base.py                 # AgentContext, AgentReply, extract_json_blob,
                                      #   extract_reply_text, PERSONALITY (create)
  app/agents/router.py               # classify_intent (create)
  app/agents/order_tracking.py       # (create)
  app/agents/product_search.py       # ProductSource protocol (create)
  app/agents/policy.py               # (create)
  app/agents/recommendations.py      # (create)
  app/agents/customer_support.py     # handoff logic (create)
  app/knowledge/seeds/brand_voice.md # rewritten personality (modify)
  app/knowledge/seeds/{faq,business}.json  # rewritten with real policy text (modify)
  app/deps.py                        # + Container.conversations, active_llm (modify)
  app/channels/whatsapp.py           # InboundText -> router -> agent -> send (modify)
  tests/test_ingest_store.py         # + find_mappings_by_phone, count_orders_by_phone (modify)
  tests/store/test_conversation_store.py  # (create)
  tests/core/__init__.py             # (create)
  tests/core/test_order_resolver.py  # (create)
  tests/core/test_sanitize.py        # (create)
  tests/core/test_memory.py          # (create)
  tests/agents/__init__.py           # (create)
  tests/agents/test_base.py          # (create)
  tests/agents/test_router.py        # (create)
  tests/agents/test_order_tracking.py     # (create)
  tests/agents/test_product_search.py     # (create)
  tests/agents/test_policy.py             # (create)
  tests/agents/test_recommendations.py    # (create)
  tests/agents/test_customer_support.py   # (create)
  tests/admin/test_controls.py       # + vip_order_count_threshold (modify)
  tests/test_client_reads.py         # + search_products (modify)
  tests/test_deps.py                 # + active_llm (modify)
  tests/test_whatsapp_webhook.py     # + conversation wiring (modify)
  docs/memory/{component_registry,api_registry}.md   # (modify)
  docs/FR/_pipeline_status.md        # (modify)
```

---

### Task 1: `IngestStore.find_mappings_by_phone` + `core/order_resolver.py`

**Files:**
- Modify: `backend/app/store/base.py`, `backend/app/store/memory.py`, `backend/app/store/postgres.py`
- Create: `backend/app/core/order_resolver.py`
- Modify: `backend/tests/test_ingest_store.py`
- Create: `backend/tests/core/__init__.py` (empty), `backend/tests/core/test_order_resolver.py`

**Interfaces:**
- Produces: `IngestStore.find_mappings_by_phone(phone_e164: str, limit: int = 20) -> list[MappingView]`;
  `app.core.order_resolver.OrderSource` (Protocol: `get_order`, `find_order_by_name`,
  `find_customer_orders_by_phone` — `ShopifyClient` already matches this shape structurally);
  `async resolve_by_phone(shopify: OrderSource, ingest: IngestStore, wa_id: str) -> list[AuthorizedOrder]`;
  `async resolve_by_order_name(shopify: OrderSource, wa_id: str, raw_name: str) -> AuthorizedOrder | None`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_ingest_store.py`:

```python
async def test_find_mappings_by_phone_returns_matches_only() -> None:
    from app.store.base import MappingUpsert
    from app.store.memory import InMemoryIngestStore

    store = InMemoryIngestStore()
    await store.ingest_order_created(
        "wh1", "orders/create",
        MappingUpsert(
            order_gid="gid://1", order_name="tavas1", order_number_int=1,
            phone_e164="+919999999999", customer_name=None, email=None,
            language="en", financial_status_at_create=None, is_cod=False,
        ),
        None,
    )
    await store.ingest_order_created(
        "wh2", "orders/create",
        MappingUpsert(
            order_gid="gid://2", order_name="tavas2", order_number_int=2,
            phone_e164="+918888888888", customer_name=None, email=None,
            language="en", financial_status_at_create=None, is_cod=False,
        ),
        None,
    )
    matches = await store.find_mappings_by_phone("+919999999999")
    assert [m.order_gid for m in matches] == ["gid://1"]


async def test_find_mappings_by_phone_no_match_returns_empty() -> None:
    from app.store.memory import InMemoryIngestStore

    store = InMemoryIngestStore()
    assert await store.find_mappings_by_phone("+910000000000") == []
```

`backend/tests/core/test_order_resolver.py`:

```python
from app.core.order_resolver import resolve_by_order_name, resolve_by_phone
from app.shopify.models import Order
from app.store.base import MappingView


def _order(gid: str, name: str, phone: str | None) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeShopify:
    def __init__(
        self,
        orders_by_gid: dict[str, Order] | None = None,
        orders_by_name: dict[str, Order] | None = None,
        orders_by_phone: dict[str, list[Order]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.orders_by_gid = orders_by_gid or {}
        self.orders_by_name = orders_by_name or {}
        self.orders_by_phone = orders_by_phone or {}
        self.raises = raises

    async def get_order(self, gid: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.orders_by_gid.get(gid)

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.orders_by_name.get(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        if self.raises:
            raise self.raises
        return self.orders_by_phone.get(phone_e164, [])


class _FakeIngest:
    def __init__(self, mappings: list[MappingView] | None = None) -> None:
        self.mappings = mappings or []

    async def ingest_order_created(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    async def recent_mappings(self, limit: int) -> list[MappingView]:
        raise NotImplementedError

    async def recent_outbound(self, limit: int) -> list[object]:
        raise NotImplementedError

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        return [m for m in self.mappings if m.phone_e164 == phone_e164][:limit]

    async def count_orders_by_phone(self, phone_e164: str) -> int:
        return len([m for m in self.mappings if m.phone_e164 == phone_e164])

    async def delete_by_phone(self, phone_e164: str) -> object:
        raise NotImplementedError

    async def purge_older_than(self, cutoff: object) -> object:
        raise NotImplementedError


def _mapping(gid: str, name: str, phone: str) -> MappingView:
    return MappingView(
        order_gid=gid, order_name=name, phone_e164=phone,
        status="pending", is_cod=False, created_at=None,
    )


async def test_resolve_by_phone_finds_via_mapping() -> None:
    order = _order("gid://1", "tavas1", "+919999999999")
    shopify = _FakeShopify(orders_by_gid={"gid://1": order})
    ingest = _FakeIngest(mappings=[_mapping("gid://1", "tavas1", "+919999999999")])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert len(result) == 1
    assert result[0].order.gid == "gid://1"
    assert result[0].verified_phone == "+919999999999"


async def test_resolve_by_phone_falls_back_to_customer_lookup() -> None:
    order = _order("gid://2", "tavas2", "+918888888888")
    shopify = _FakeShopify(orders_by_phone={"+918888888888": [order]})
    ingest = _FakeIngest(mappings=[])

    result = await resolve_by_phone(shopify, ingest, "918888888888")

    assert len(result) == 1
    assert result[0].order.gid == "gid://2"


async def test_resolve_by_phone_drops_stale_mapping_with_wrong_live_phone() -> None:
    order = _order("gid://3", "tavas3", "+917777777777")
    shopify = _FakeShopify(orders_by_gid={"gid://3": order})
    ingest = _FakeIngest(mappings=[_mapping("gid://3", "tavas3", "+919999999999")])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert result == []


async def test_resolve_by_phone_invalid_number_returns_empty() -> None:
    result = await resolve_by_phone(_FakeShopify(), _FakeIngest(), "abc")
    assert result == []


async def test_resolve_by_phone_degrades_to_empty_on_shopify_outage() -> None:
    from app.shopify.errors import ShopifyUnavailable

    shopify = _FakeShopify(raises=ShopifyUnavailable("down"))
    ingest = _FakeIngest(mappings=[_mapping("gid://4", "tavas4", "+919999999999")])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert result == []


async def test_resolve_by_order_name_ownership_match() -> None:
    order = _order("gid://5", "tavas5", "+919999999999")
    shopify = _FakeShopify(orders_by_name={"tavas5": order})

    result = await resolve_by_order_name(shopify, "919999999999", "tavas5")

    assert result is not None
    assert result.order.gid == "gid://5"


async def test_resolve_by_order_name_ownership_mismatch_returns_none() -> None:
    order = _order("gid://6", "tavas6", "+911111111111")
    shopify = _FakeShopify(orders_by_name={"tavas6": order})

    result = await resolve_by_order_name(shopify, "919999999999", "tavas6")

    assert result is None


async def test_resolve_by_order_name_not_found_returns_none() -> None:
    result = await resolve_by_order_name(_FakeShopify(), "919999999999", "tavas999")
    assert result is None
```

- [ ] **Step 2: Run to verify FAIL**

Run (from `backend/`): `python -m pytest tests/test_ingest_store.py tests/core/test_order_resolver.py -v`
Expected: `ModuleNotFoundError: No module named 'app.core.order_resolver'` and `AttributeError: 'InMemoryIngestStore' object has no attribute 'find_mappings_by_phone'`.

- [ ] **Step 3: Implement**

Append to `backend/app/store/base.py`, inside the `IngestStore` Protocol:

```python
    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]: ...
```

Append to `backend/app/store/memory.py`, inside `InMemoryIngestStore`:

```python
    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        matches = [m for m in self.mappings.values() if m.phone_e164 == phone_e164]
        views = [
            MappingView(
                order_gid=m.order_gid,
                order_name=m.order_name,
                phone_e164=m.phone_e164,
                status="pending",
                is_cod=m.is_cod,
                created_at=None,
            )
            for m in matches
        ]
        return list(reversed(views))[:limit]
```

Append to `backend/app/store/postgres.py`, inside `PostgresIngestStore`:

```python
    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid, order_name, phone_e164, status, is_cod, created_at"
                " FROM order_mappings WHERE phone_e164 = $1 ORDER BY created_at DESC LIMIT $2",
                phone_e164,
                limit,
            )
        return [
            MappingView(
                order_gid=str(r["order_gid"]),
                order_name=str(r["order_name"]),
                phone_e164=None if r["phone_e164"] is None else str(r["phone_e164"]),
                status=str(r["status"]),
                is_cod=bool(r["is_cod"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]
```

Create `backend/app/core/order_resolver.py`:

```python
from typing import Protocol

from app.core.phone import normalize_phone
from app.shopify.errors import ShopifyError
from app.shopify.models import AuthorizedOrder, Order
from app.store.base import IngestStore


class OrderSource(Protocol):
    """What order_resolver needs from Shopify -- an interface, not the concrete client
    (core depends on interfaces, not adapters). ``ShopifyClient`` already matches this
    shape structurally; no inheritance is required."""

    async def get_order(self, gid: str) -> Order | None: ...

    async def find_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]: ...


async def resolve_by_phone(
    shopify: OrderSource, ingest: IngestStore, wa_id: str
) -> list[AuthorizedOrder]:
    """Resolve every order this WhatsApp sender can be shown, re-fetched live from Shopify.

    Chain: our own order_mappings (fast path, built by the Phase 2 webhook) -> Shopify
    customer-by-phone fallback. Every candidate is re-fetched live (never trust the mapping
    snapshot) and re-verified through AuthorizedOrder's own ownership invariant, which raises
    if the live phone no longer matches -- a stale mapping is silently dropped, never
    surfaced. A Shopify outage degrades to whatever was already resolved (often empty) rather
    than raising, so a temporary Shopify blip does not stop the conversation.
    """
    phone = normalize_phone(wa_id)
    if phone is None:
        return []
    orders: list[AuthorizedOrder] = []
    try:
        for mapping in await ingest.find_mappings_by_phone(phone):
            order = await shopify.get_order(mapping.order_gid)
            if order is None:
                continue
            try:
                orders.append(AuthorizedOrder(order=order, verified_phone=phone))
            except ValueError:
                continue
        if orders:
            return orders
        for order in await shopify.find_customer_orders_by_phone(phone):
            try:
                orders.append(AuthorizedOrder(order=order, verified_phone=phone))
            except ValueError:
                continue
    except ShopifyError:
        return orders
    return orders


async def resolve_by_order_name(
    shopify: OrderSource, wa_id: str, raw_name: str
) -> AuthorizedOrder | None:
    """Look up an order by the number the customer typed, ownership-checked against wa_id.

    Returns None both when the order does not exist AND when it exists but belongs to a
    different phone number, so a reply can never be used to enumerate whether an order
    number is valid (Critical Rule 3).
    """
    phone = normalize_phone(wa_id)
    if phone is None:
        return None
    try:
        order = await shopify.find_order_by_name(raw_name)
    except ShopifyError:
        return None
    if order is None:
        return None
    try:
        return AuthorizedOrder(order=order, verified_phone=phone)
    except ValueError:
        return None
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_ingest_store.py tests/core/test_order_resolver.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/store/base.py app/store/memory.py app/store/postgres.py app/core/order_resolver.py tests/test_ingest_store.py tests/core/__init__.py tests/core/test_order_resolver.py
git commit -m "feat: order_resolver -- phone/order-name resolution with live re-fetch + ownership check"
```

---

### Task 2: `core/sanitize.py`

**Files:**
- Create: `backend/app/core/sanitize.py`
- Create: `backend/tests/core/test_sanitize.py`

**Interfaces:**
- Produces: `strip_markdown(text: str) -> str`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/core/test_sanitize.py`:

```python
from app.core.sanitize import strip_markdown


def test_double_asterisk_becomes_whatsapp_single_asterisk() -> None:
    assert strip_markdown("This is **bold** text") == "This is *bold* text"


def test_heading_marker_removed() -> None:
    assert strip_markdown("# Order status\nShipped") == "Order status\nShipped"


def test_multiple_blank_lines_collapsed() -> None:
    assert strip_markdown("line one\n\n\n\nline two") == "line one\n\nline two"


def test_plain_text_unchanged() -> None:
    text = "Hello, your order is on the way."
    assert strip_markdown(text) == text


def test_leading_trailing_whitespace_stripped() -> None:
    assert strip_markdown("  hello  ") == "hello"


def test_multiple_bold_spans_all_converted() -> None:
    assert strip_markdown("**one** and **two**") == "*one* and *two*"
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/core/test_sanitize.py -v`
Expected: `ModuleNotFoundError: No module named 'app.core.sanitize'`.

- [ ] **Step 3: Implement**

`backend/app/core/sanitize.py`:

```python
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """Convert standard Markdown to WhatsApp-safe plain text.

    WhatsApp renders ``**bold**`` literally (its own bold marker is a single ``*``), so an
    LLM reply written in standard Markdown must be converted, not just stripped. Heading
    markers are removed (WhatsApp has no heading concept); runs of 3+ blank lines are
    collapsed to keep replies compact on a phone screen.
    """
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _HEADING_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/core/test_sanitize.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/core/sanitize.py tests/core/test_sanitize.py
git commit -m "feat: strip_markdown -- convert LLM Markdown to WhatsApp-safe plain text"
```

---

### Task 3: `ConversationStore` (incl. pause/handoff) + `core/memory.py`

**Files:**
- Modify: `backend/app/store/base.py`, `backend/app/store/memory.py`, `backend/app/store/postgres.py`, `backend/app/store/schema.sql`
- Create: `backend/app/core/memory.py`
- Create: `backend/tests/store/test_conversation_store.py`
- Create: `backend/tests/core/test_memory.py`

**Interfaces:**
- Produces: `app.store.base.StoredMessage` (frozen dataclass: `role: str`, `content: str`,
  `created_at: str | None`); `app.store.base.ConversationStore` Protocol —
  `get_or_create(user_id: str) -> int`,
  `recent_messages(conversation_id: int, limit: int) -> list[StoredMessage]`,
  `append_message(conversation_id: int, role: str, content: str) -> None`,
  `pause_until(conversation_id: int, until: datetime) -> None`,
  `get_paused_until(conversation_id: int) -> datetime | None`,
  `mark_handoff_attempted(conversation_id: int, at: datetime) -> None`,
  `get_handoff_attempted_at(conversation_id: int) -> datetime | None`;
  `InMemoryConversationStore`, `PostgresConversationStore`;
  `app.core.memory.DEFAULT_WINDOW`,
  `async load_history(store, wa_id, window=DEFAULT_WINDOW) -> tuple[int, list[Message]]`,
  `async persist_turn(store, conversation_id, user_text, assistant_reply) -> None`.

**Why `handoff_attempted_at` is a new column, not a workaround:** the "one AI attempt, then
immediate handoff" rule (design spec) needs to distinguish "this is the customer's first ask
for a human" from "they already asked once and the AI already tried." Detecting this by
scanning message content for a marker string would be fragile; `paused_until` already tracks
"a human has taken over," which is a different state (post-handoff) from "one attempt has been
used but the AI is still allowed to keep talking." Both need to persist across messages within
the same conversation window, so both are real columns on `conversations`, not derived state.

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_conversation_store.py`:

```python
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.store.memory import InMemoryConversationStore


async def test_get_or_create_is_stable_per_user() -> None:
    store = InMemoryConversationStore()
    id1 = await store.get_or_create("919999999999")
    id2 = await store.get_or_create("919999999999")
    assert id1 == id2


async def test_different_users_get_different_conversations() -> None:
    store = InMemoryConversationStore()
    id1 = await store.get_or_create("919999999999")
    id2 = await store.get_or_create("918888888888")
    assert id1 != id2


async def test_append_and_recent_messages_roundtrip() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    await store.append_message(conversation_id, "user", "hello")
    await store.append_message(conversation_id, "assistant", "hi there")
    messages = await store.recent_messages(conversation_id, 10)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]


async def test_recent_messages_respects_limit() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    for i in range(5):
        await store.append_message(conversation_id, "user", f"msg{i}")
    messages = await store.recent_messages(conversation_id, 2)
    assert [m.content for m in messages] == ["msg3", "msg4"]


async def test_paused_until_roundtrip() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    assert await store.get_paused_until(conversation_id) is None
    until = datetime(2026, 1, 1, tzinfo=UTC)
    await store.pause_until(conversation_id, until)
    assert await store.get_paused_until(conversation_id) == until


async def test_handoff_attempted_roundtrip() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    assert await store.get_handoff_attempted_at(conversation_id) is None
    at = datetime(2026, 1, 1, tzinfo=UTC)
    await store.mark_handoff_attempted(conversation_id, at)
    assert await store.get_handoff_attempted_at(conversation_id) == at


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_conversation_roundtrip_postgres() -> None:
    import uuid

    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConversationStore

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    store = PostgresConversationStore(pool)
    user_id = f"test-wa-id-{uuid.uuid4()}"
    conversation_id = await store.get_or_create(user_id)
    await store.append_message(conversation_id, "user", "hello")
    await store.append_message(conversation_id, "assistant", "hi there")
    messages = await store.recent_messages(conversation_id, 10)
    assert [m.content for m in messages] == ["hello", "hi there"]
    same_id = await store.get_or_create(user_id)
    assert same_id == conversation_id

    until = datetime.now(UTC) + timedelta(hours=1)
    await store.pause_until(conversation_id, until)
    fetched = await store.get_paused_until(conversation_id)
    assert fetched is not None and abs((fetched - until).total_seconds()) < 1

    attempted_at = datetime.now(UTC)
    await store.mark_handoff_attempted(conversation_id, attempted_at)
    fetched_attempt = await store.get_handoff_attempted_at(conversation_id)
    assert fetched_attempt is not None and abs((fetched_attempt - attempted_at).total_seconds()) < 1
    await pool.close()
```

`backend/tests/core/test_memory.py`:

```python
from app.core.memory import load_history, persist_turn
from app.store.memory import InMemoryConversationStore


async def test_first_load_creates_conversation_with_empty_history() -> None:
    store = InMemoryConversationStore()
    conversation_id, history = await load_history(store, "919999999999")
    assert conversation_id is not None
    assert history == []


async def test_persist_then_load_returns_turns_in_order() -> None:
    store = InMemoryConversationStore()
    conversation_id, _ = await load_history(store, "919999999999")
    await persist_turn(store, conversation_id, "where is my order", "let me check")
    _, history = await load_history(store, "919999999999")
    assert [m.content for m in history] == ["where is my order", "let me check"]
    assert [m.role for m in history] == ["user", "assistant"]


async def test_same_wa_id_reuses_conversation() -> None:
    store = InMemoryConversationStore()
    id1, _ = await load_history(store, "919999999999")
    id2, _ = await load_history(store, "919999999999")
    assert id1 == id2


async def test_window_limits_history_length() -> None:
    store = InMemoryConversationStore()
    conversation_id, _ = await load_history(store, "919999999999")
    for i in range(10):
        await persist_turn(store, conversation_id, f"msg{i}", f"reply{i}")
    _, history = await load_history(store, "919999999999", window=4)
    assert len(history) == 4
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/store/test_conversation_store.py tests/core/test_memory.py -v`
Expected: `ImportError: cannot import name 'InMemoryConversationStore'` and
`ModuleNotFoundError: No module named 'app.core.memory'`.

- [ ] **Step 3: Implement**

Append to `backend/app/store/schema.sql`, immediately after the `conversations` table's
`CREATE INDEX` line:

```sql
-- Tracks whether ONE AI handoff attempt has already been used in the current conversation
-- window (client decision, round 3 2026-08-06: one AI attempt, then immediate handoff on a
-- second request). Distinct from paused_until, which marks a human has already taken over.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS handoff_attempted_at timestamptz;
```

Append to `backend/app/store/base.py` (add `from datetime import datetime` to the top-of-file
imports, then add at the end of the file):

```python
@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None


class ConversationStore(Protocol):
    """Windowed chat history + handoff state per WhatsApp sender."""

    async def get_or_create(self, user_id: str) -> int: ...

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]: ...

    async def append_message(self, conversation_id: int, role: str, content: str) -> None: ...

    async def pause_until(self, conversation_id: int, until: datetime) -> None: ...

    async def get_paused_until(self, conversation_id: int) -> datetime | None: ...

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None: ...

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None: ...
```

Append to `backend/app/store/memory.py` (add `StoredMessage` to the existing
`from app.store.base import (...)` block, add `from datetime import datetime` at the top):

```python
class InMemoryConversationStore:
    def __init__(self) -> None:
        self._conversations: dict[str, int] = {}
        self._messages: dict[int, list[StoredMessage]] = {}
        self._paused_until: dict[int, datetime] = {}
        self._handoff_attempted_at: dict[int, datetime] = {}
        self._next_id = 1

    async def get_or_create(self, user_id: str) -> int:
        if user_id not in self._conversations:
            self._conversations[user_id] = self._next_id
            self._messages[self._next_id] = []
            self._next_id += 1
        return self._conversations[user_id]

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        return self._messages.get(conversation_id, [])[-limit:]

    async def append_message(self, conversation_id: int, role: str, content: str) -> None:
        self._messages.setdefault(conversation_id, []).append(
            StoredMessage(role=role, content=content, created_at=None)
        )

    async def pause_until(self, conversation_id: int, until: datetime) -> None:
        self._paused_until[conversation_id] = until

    async def get_paused_until(self, conversation_id: int) -> datetime | None:
        return self._paused_until.get(conversation_id)

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None:
        self._handoff_attempted_at[conversation_id] = at

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None:
        return self._handoff_attempted_at.get(conversation_id)
```

Append to `backend/app/store/postgres.py` (add `StoredMessage` to the existing
`from app.store.base import (...)` block, add `from datetime import datetime` at the top):

```python
class PostgresConversationStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def get_or_create(self, user_id: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM conversations WHERE user_id = $1"
                " ORDER BY last_active_at DESC LIMIT 1",
                user_id,
            )
            if row is not None:
                await conn.execute(
                    "UPDATE conversations SET last_active_at = now() WHERE id = $1", row["id"]
                )
                return int(row["id"])
            new_row = await conn.fetchrow(
                "INSERT INTO conversations (user_id) VALUES ($1) RETURNING id", user_id
            )
        return int(new_row["id"])

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = $1"
                " ORDER BY created_at DESC LIMIT $2",
                conversation_id,
                limit,
            )
        ordered = list(reversed(rows))
        return [
            StoredMessage(
                role=str(r["role"]),
                content=str(r["content"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in ordered
        ]

    async def append_message(self, conversation_id: int, role: str, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)",
                conversation_id,
                role,
                content,
            )

    async def pause_until(self, conversation_id: int, until: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET paused_until = $1 WHERE id = $2", until, conversation_id
            )

    async def get_paused_until(self, conversation_id: int) -> datetime | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT paused_until FROM conversations WHERE id = $1", conversation_id
            )
        return None if row is None else row["paused_until"]

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET handoff_attempted_at = $1 WHERE id = $2",
                at,
                conversation_id,
            )

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT handoff_attempted_at FROM conversations WHERE id = $1", conversation_id
            )
        return None if row is None else row["handoff_attempted_at"]
```

Create `backend/app/core/memory.py`:

```python
from app.providers.base import Message
from app.store.base import ConversationStore

DEFAULT_WINDOW = 8


async def load_history(
    store: ConversationStore, wa_id: str, window: int = DEFAULT_WINDOW
) -> tuple[int, list[Message]]:
    """Return (conversation_id, recent turns as provider Message objects), creating the
    conversation on first contact. Only user/assistant turns are replayed into the prompt."""
    conversation_id = await store.get_or_create(wa_id)
    stored = await store.recent_messages(conversation_id, window)
    history: list[Message] = []
    for m in stored:
        if m.role == "user":
            history.append(Message(role="user", content=m.content))
        elif m.role == "assistant":
            history.append(Message(role="assistant", content=m.content))
    return conversation_id, history


async def persist_turn(
    store: ConversationStore, conversation_id: int, user_text: str, assistant_reply: str
) -> None:
    await store.append_message(conversation_id, "user", user_text)
    await store.append_message(conversation_id, "assistant", assistant_reply)
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/store/test_conversation_store.py tests/core/test_memory.py -v`
Expected: all PASS (Postgres test SKIPPED without `TEST_DATABASE_URL`).
Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/store/base.py app/store/memory.py app/store/postgres.py app/store/schema.sql app/core/memory.py tests/store/test_conversation_store.py tests/core/test_memory.py
git commit -m "feat: ConversationStore with pause/handoff state + windowed memory"
```

---

### Task 4: VIP threshold config + `IngestStore.count_orders_by_phone`

**Files:**
- Modify: `backend/app/admin/controls.py`, `backend/app/admin/static/{index.html,admin.js}`
- Modify: `backend/app/store/base.py`, `backend/app/store/memory.py`, `backend/app/store/postgres.py`
- Modify: `backend/tests/admin/test_controls.py`, `backend/tests/test_ingest_store.py`

**Interfaces:**
- Produces: `AdminControls.vip_order_count_threshold: int` (default 3, `ge=1, le=100`);
  `IngestStore.count_orders_by_phone(phone_e164: str) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/admin/test_controls.py`:

```python
def test_defaults_include_vip_threshold() -> None:
    from app.admin.controls import AdminControls

    assert AdminControls().vip_order_count_threshold == 3


async def test_vip_threshold_roundtrip(master_key: str) -> None:
    from app.admin.controls import AdminControls, load_controls, save_controls
    from app.config.crypto import SecretVault
    from app.config.service import ConfigService
    from app.store.memory import InMemoryConfigRepo

    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await save_controls(config, AdminControls(vip_order_count_threshold=5))
    loaded = await load_controls(config)
    assert loaded.vip_order_count_threshold == 5
```

Append to `backend/tests/test_ingest_store.py`:

```python
async def test_count_orders_by_phone_counts_matches_only() -> None:
    from app.store.base import MappingUpsert
    from app.store.memory import InMemoryIngestStore

    store = InMemoryIngestStore()
    for i in range(3):
        await store.ingest_order_created(
            f"wh{i}", "orders/create",
            MappingUpsert(
                order_gid=f"gid://{i}", order_name=f"tavas{i}", order_number_int=i,
                phone_e164="+919999999999", customer_name=None, email=None,
                language="en", financial_status_at_create=None, is_cod=False,
            ),
            None,
        )
    assert await store.count_orders_by_phone("+919999999999") == 3
    assert await store.count_orders_by_phone("+910000000000") == 0
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/admin/test_controls.py tests/test_ingest_store.py -v`
Expected: `AttributeError` on both the missing `vip_order_count_threshold` field and the
missing `count_orders_by_phone` method.

- [ ] **Step 3: Implement**

Modify `backend/app/admin/controls.py` — add the field to `AdminControls` (after
`owner_alert_number`):

```python
    # VIP/repeat-customer threshold (order count, not spend -- order_mappings has no amount
    # column; total_spend is a rare on-demand live Shopify lookup, never pre-computed).
    vip_order_count_threshold: int = Field(default=3, ge=1, le=100)
```

Add `"vip_order_count_threshold"` to the `_INT_KEYS` tuple:

```python
_INT_KEYS: tuple[str, ...] = ("push_staleness_hours", "retention_days", "vip_order_count_threshold")
```

Modify `backend/app/admin/static/index.html` — add a numeric input in the Operational
Controls section, next to the existing retention field (find the `c-retention` input and add
this alongside it, matching its label/input structure exactly):

```html
<label>VIP threshold (orders)</label>
<input type="number" id="c-vip-threshold" min="1" max="100" />
```

Modify `backend/app/admin/static/admin.js` — in the same function that loads `c-retention`
(around line 260), add:

```javascript
  el("c-vip-threshold").value = c.vip_order_count_threshold;
```

In the same function that builds the save payload (around line 288), add:

```javascript
    vip_order_count_threshold: parseInt(el("c-vip-threshold").value, 10),
```

Append to `backend/app/store/base.py`, inside the `IngestStore` Protocol:

```python
    async def count_orders_by_phone(self, phone_e164: str) -> int: ...
```

Append to `backend/app/store/memory.py`, inside `InMemoryIngestStore`:

```python
    async def count_orders_by_phone(self, phone_e164: str) -> int:
        return len([m for m in self.mappings.values() if m.phone_e164 == phone_e164])
```

Append to `backend/app/store/postgres.py`, inside `PostgresIngestStore`:

```python
    async def count_orders_by_phone(self, phone_e164: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS n FROM order_mappings WHERE phone_e164 = $1", phone_e164
            )
        return int(row["n"]) if row else 0
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/admin/test_controls.py tests/test_ingest_store.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/admin/controls.py app/admin/static/index.html app/admin/static/admin.js app/store/base.py app/store/memory.py app/store/postgres.py tests/admin/test_controls.py tests/test_ingest_store.py
git commit -m "feat: VIP order-count threshold config + count_orders_by_phone"
```

---

### Task 5: `ShopifyClient.search_products`

**Files:**
- Modify: `backend/app/shopify/client.py`, `backend/app/shopify/models.py`
- Modify: `backend/tests/test_client_reads.py` (the existing file for `ShopifyClient` read
  operations — `get_order`/`find_order_by_name`/`find_customer_orders_by_phone` already live
  here; `search_products` is the same kind of operation, append rather than creating a new file)

**Interfaces:**
- Consumes: `app.shopify.client.ShopifyClient._graphql` (existing private helper); the test
  helpers `make_client`/`grant_or`/`seed` from `tests/test_client_graphql.py` (already imported
  by `test_client_reads.py` — reuse the same import line, do not redefine them).
- Produces: `app.shopify.models.Product` (frozen dataclass: `gid: str`, `title: str`,
  `handle: str`, `price: Money | None`, `available: bool`, `product_type: str | None`,
  `tags: tuple[str, ...]`); `ShopifyClient.search_products(query: str, limit: int = 5) -> list[Product]`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_client_reads.py`:

```python
PRODUCT_NODE = {
    "id": "gid://shopify/Product/1",
    "title": "Blue Chikankari Kurti",
    "handle": "blue-chikankari-kurti",
    "productType": "Kurti",
    "tags": ["chikankari", "blue"],
    "totalInventory": 12,
    "priceRangeV2": {"minVariantPrice": {"amount": "1299.0", "currencyCode": "INR"}},
}


async def test_search_products_parses_full_node(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"products": {"edges": [{"node": PRODUCT_NODE}]}}}
        )

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("blue kurti")
    assert len(results) == 1
    product = results[0]
    assert product.gid == "gid://shopify/Product/1"
    assert product.title == "Blue Chikankari Kurti"
    assert product.available is True
    assert product.price is not None and product.price.currency == "INR"


async def test_search_products_includes_status_active_in_query(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.search_products("kurti")
    assert "status:active" in captured["variables"]["q"]


async def test_search_products_zero_inventory_is_unavailable(settings, master_key) -> None:
    node = {**PRODUCT_NODE, "totalInventory": 0}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"products": {"edges": [{"node": node}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("kurti")
    assert results[0].available is False


async def test_search_products_untracked_inventory_defaults_available(settings, master_key) -> None:
    node = {**PRODUCT_NODE, "totalInventory": None}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"products": {"edges": [{"node": node}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("kurti")
    assert results[0].available is True


async def test_search_products_respects_limit(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.search_products("kurti", limit=2)
    assert captured["variables"]["first"] == 2


async def test_search_products_empty_query_returns_empty_without_calling_shopify(
    settings, master_key
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, handler)
    await seed(config)
    assert await client.search_products("") == []
    assert calls == []
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_client_reads.py -v`
Expected: `AttributeError: 'ShopifyClient' object has no attribute 'search_products'`.

- [ ] **Step 3: Implement**

Append to `backend/app/shopify/models.py`:

```python
@dataclass(frozen=True)
class Product:
    gid: str
    title: str
    handle: str
    price: Money | None
    available: bool
    product_type: str | None
    tags: tuple[str, ...]
```

Append to `backend/app/shopify/client.py`, near `ORDER_FIELDS` at the top of the file:

```python
PRODUCT_FIELDS = (
    "id title handle productType tags totalInventory "
    "priceRangeV2 { minVariantPrice { amount currencyCode } }"
)


def _product_from_node(node: dict[str, Any]) -> Product:
    price_node = (node.get("priceRangeV2") or {}).get("minVariantPrice")
    total_inventory = node.get("totalInventory")
    # None (untracked inventory) defaults to available; a real 0 means out of stock.
    available = total_inventory is None or total_inventory > 0
    return Product(
        gid=str(node["id"]),
        title=str(node["title"]),
        handle=str(node.get("handle") or ""),
        price=Money(str(price_node["amount"]), str(price_node["currencyCode"]))
        if price_node
        else None,
        available=available,
        product_type=node.get("productType"),
        tags=tuple(node.get("tags") or ()),
    )
```

Add `Product` to the existing `from app.shopify.models import (...)` block at the top of
`client.py`. Then append this method to `ShopifyClient` (after `find_customer_orders_by_phone`,
mirroring its structure and error handling exactly):

```python
    async def search_products(self, query: str, limit: int = 5) -> list[Product]:
        stripped = query.strip()
        if not stripped:
            return []
        # status:active only -- never surface a draft/archived product to a customer.
        gql_query = f"({stripped}) AND status:active"
        data = await self._graphql(
            f"query($q: String!, $first: Int!) {{ products(first: $first, query: $q) "
            f"{{ edges {{ node {{ {PRODUCT_FIELDS} }} }} }} }}",
            {"q": gql_query, "first": limit},
        )
        edges = (data.get("products") or {}).get("edges") or []
        return [_product_from_node(e["node"]) for e in edges]
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_client_reads.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/shopify/client.py app/shopify/models.py tests/test_client_reads.py
git commit -m "feat: ShopifyClient.search_products -- active, in-stock product search"
```

---

### Task 6: `app/agents/base.py` + personality rewrite

**Files:**
- Create: `backend/app/agents/__init__.py` (empty), `backend/app/agents/base.py`
- Modify: `backend/app/knowledge/seeds/brand_voice.md`
- Create: `backend/tests/agents/__init__.py` (empty), `backend/tests/agents/test_base.py`

**Interfaces:**
- Consumes: `app.providers.base.{LLMProvider, Message}`, `app.shopify.models.AuthorizedOrder`.
- Produces: `app.agents.base.AgentContext` (frozen dataclass — see fields below);
  `app.agents.base.AgentReply` (frozen dataclass: `text: str`, `handoff: bool = False`);
  `extract_json_blob(raw_text: str) -> dict[str, object] | None`;
  `extract_reply_text(raw_text: str, fallback: str) -> str`; `PERSONALITY: str` (module-level
  constant).

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_base.py`:

```python
from app.agents.base import extract_json_blob, extract_reply_text


def test_extract_json_blob_direct() -> None:
    assert extract_json_blob('{"reply": "hi"}') == {"reply": "hi"}


def test_extract_json_blob_strips_think_and_fence() -> None:
    text = '<think>reasoning</think>```json\n{"reply": "hi there"}\n```'
    assert extract_json_blob(text) == {"reply": "hi there"}


def test_extract_json_blob_extracts_outer_braces_from_prose() -> None:
    text = 'Sure, here is my answer: {"reply": "ok"} -- hope that helps.'
    assert extract_json_blob(text) == {"reply": "ok"}


def test_extract_json_blob_non_json_returns_none() -> None:
    assert extract_json_blob("just plain text, no JSON here") is None


def test_extract_json_blob_json_array_returns_none() -> None:
    assert extract_json_blob("[1, 2, 3]") is None


def test_extract_reply_text_from_valid_json() -> None:
    assert extract_reply_text('{"reply": "Your order is confirmed."}', "fallback") == (
        "Your order is confirmed."
    )


def test_extract_reply_text_falls_back_on_missing_reply_key() -> None:
    assert extract_reply_text('{"other": "x"}', "fallback") == "fallback"


def test_extract_reply_text_tolerates_plain_text_completion() -> None:
    assert extract_reply_text("Sure, happy to help!", "fallback") == "Sure, happy to help!"


def test_extract_reply_text_empty_completion_falls_back() -> None:
    assert extract_reply_text("   ", "fallback") == "fallback"
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_base.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents'`.

- [ ] **Step 3: Implement**

Create `backend/app/agents/__init__.py` (empty).

Create `backend/app/agents/base.py`:

```python
import json
import re
from dataclasses import dataclass
from typing import Any

from app.providers.base import LLMProvider, Message
from app.shopify.models import AuthorizedOrder

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_BRACES_RE = re.compile(r"\{.*\}", re.DOTALL)

# Shared "Friendly Fashion Advisor" personality, injected into every agent's system prompt so
# tone stays consistent regardless of which specialist answers (client decision, round 3
# 2026-08-06). This is a cross-cutting constraint, not one agent's job.
PERSONALITY = """You are the Thetavas WhatsApp shopping assistant -- a Friendly Fashion \
Advisor, not just a support bot. Be warm, professional, and fashion-knowledgeable. Speak \
naturally in English, Hindi, or Hinglish, matching the customer's own language and style. \
Use emojis sparingly, only when they fit naturally. Be honest and trustworthy -- never invent \
product details, policy terms, or order information; if you don't know something, say so and \
offer to connect the customer with the team. Never state a customer's total spending, order \
count, or detailed purchase history unless they explicitly ask for it -- you may use that \
knowledge to inform your tone (for example, a warmer welcome-back for a returning customer) \
but never announce the numbers unprompted."""


@dataclass(frozen=True)
class AgentContext:
    wa_id: str
    phone_e164: str
    user_text: str
    history: list[Message]
    orders: list[AuthorizedOrder]
    is_vip: bool
    knowledge: dict[str, str]
    provider: LLMProvider
    model: str
    api_key: str
    extra_params: dict[str, object] | None
    timeout: float = 20.0


@dataclass(frozen=True)
class AgentReply:
    text: str
    handoff: bool = False


def extract_json_blob(raw_text: str) -> dict[str, object] | None:
    """Hardened JSON-object extraction from a raw LLM completion.

    Strips <think> reasoning blocks and ``` code-fence wrapping, then tries direct
    ``json.loads``, falling back to extracting the outermost ``{...}`` span. Returns None
    (never raises) if no JSON object can be recovered -- callers own their own fallback.
    """
    text = _THINK_RE.sub("", raw_text)
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else text.strip()
    try:
        data: Any = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    brace_match = _BRACES_RE.search(candidate)
    if brace_match:
        try:
            data = json.loads(brace_match.group(0))
            return data if isinstance(data, dict) else None
        except ValueError:
            pass
    return None


def extract_reply_text(raw_text: str, fallback: str) -> str:
    """Extract the customer-facing reply text from a raw completion.

    Prefers the requested ``{"reply": "..."}`` JSON shape; if the completion parses as JSON
    but has no usable ``reply`` string, degrades to ``fallback`` (never leaks raw JSON syntax
    to the customer). If the completion isn't JSON at all, the plain text is trusted as-is --
    some models drift from the requested format but still produce a safe natural-language
    reply, and treating "wasn't JSON" as a hard failure would reject good replies.
    """
    data = extract_json_blob(raw_text)
    if data is not None:
        reply = data.get("reply")
        if isinstance(reply, str) and reply.strip():
            return reply.strip()
        return fallback
    plain = raw_text.strip()
    return plain if plain else fallback
```

Overwrite `backend/app/knowledge/seeds/brand_voice.md` with:

```markdown
# Thetavas Brand Voice — Friendly Fashion Advisor

Thetavas' WhatsApp assistant is a **Friendly Fashion Advisor**, not just a support bot.

- Warm, professional, and genuinely fashion-knowledgeable.
- Honest and trustworthy — never invents product details, policy terms, or order status.
- Speaks naturally in English, Hindi, or Hinglish, matching the customer.
- Uses emojis sparingly, only when they fit naturally — never forced.
- Answers the customer's original question first, before ever suggesting anything else.
- Never pushy. Cross-sell and upsell only happen when the customer is asking for
  recommendations directly.
- Recognizes returning customers with a warm welcome-back tone, but never states their order
  count or total spending unless they explicitly ask.
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_base.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/__init__.py app/agents/base.py app/knowledge/seeds/brand_voice.md tests/agents/__init__.py tests/agents/test_base.py
git commit -m "feat: agents/base.py -- AgentContext, hardened JSON parsing, shared personality"
```

---

### Task 7: `app/agents/router.py`

**Files:**
- Create: `backend/app/agents/router.py`
- Create: `backend/tests/agents/test_router.py`

**Interfaces:**
- Consumes: `app.agents.base.extract_json_blob`, `app.providers.base.{LLMProvider, Message, ProviderError}`.
- Produces: `app.agents.router.Intent` (`Literal["order_tracking", "product_search", "policy", "recommendations", "customer_support"]`);
  `async classify_intent(provider: LLMProvider, model: str, api_key: str, user_text: str, *, timeout: float = 10.0, extra_params: dict[str, object] | None = None) -> Intent`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_router.py`:

```python
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
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_router.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents.router'`.

- [ ] **Step 3: Implement**

`backend/app/agents/router.py`:

```python
from typing import Literal

from app.agents.base import extract_json_blob
from app.providers.base import LLMProvider, Message, ProviderError

Intent = Literal["order_tracking", "product_search", "policy", "recommendations", "customer_support"]

_INTENTS: tuple[Intent, ...] = (
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
)

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
    try:
        result = await provider.complete(model, messages, api_key, timeout, extra_params=extra_params)
    except ProviderError:
        return "customer_support"
    data = extract_json_blob(result.text)
    if data is None:
        return "customer_support"
    intent = data.get("intent")
    return intent if intent in _INTENTS else "customer_support"
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_router.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/router.py tests/agents/test_router.py
git commit -m "feat: router agent -- 5-way intent classification"
```

---

### Task 8: `app/agents/order_tracking.py`

**Files:**
- Create: `backend/app/agents/order_tracking.py`
- Create: `backend/tests/agents/test_order_tracking.py`

**Interfaces:**
- Consumes: `app.agents.base.{AgentContext, AgentReply, PERSONALITY, extract_reply_text}`,
  `app.channels.copy.copy_for`, `app.providers.base.{Message, ProviderError}`.
- Produces: `async run(context: AgentContext) -> AgentReply`.

**Cancellation-eligibility note:** the real Shipping Policy is dispatch-based ("cancelled only
before dispatch"), not payment-based. `Order.fulfillment_status` (Shopify's
`displayFulfillmentStatus`, values like `UNFULFILLED`/`PARTIALLY_FULFILLED`/`FULFILLED`) is the
closest available signal to "has this been dispatched" without a live courier integration
(Q10: none is built) — `UNFULFILLED` is treated as not-yet-dispatched (cancel-eligible),
anything else as dispatched (not cancel-eligible). This is stated as an approximation in the
code comment, not asserted as a perfect match to a courier's actual dispatch event.

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_order_tracking.py`:

```python
from app.agents.base import AgentContext
from app.agents.order_tracking import run
from app.providers.base import CompletionResult, ProviderError, ProviderErrorKind
from app.shopify.models import AuthorizedOrder, Order


def _order(
    name: str, phone: str, fulfillment_status: str | None = None, cancelled_at: str | None = None
) -> Order:
    return Order(
        gid=f"gid://{name}", name=name, email="c@example.com", phone=phone,
        shipping_phone=None, billing_phone=None, financial_status="paid",
        fulfillment_status=fulfillment_status, cancelled_at=cancelled_at, tags=(),
        payment_gateway_names=(), total=None, customer_locale=None,
    )


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


def _context(provider, user_text: str, orders: list) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=orders, is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None,
    )


async def test_run_returns_parsed_reply() -> None:
    provider = _FixedProvider(text='{"reply": "Your order tavas1 is confirmed and on its way."}')
    order = AuthorizedOrder(order=_order("tavas1", "+919999999999"), verified_phone="+919999999999")
    result = await run(_context(provider, "where is my order", [order]))
    assert result.text == "Your order tavas1 is confirmed and on its way."


async def test_run_with_no_orders_asks_for_order_number() -> None:
    provider = _FixedProvider(text='{"reply": "Could you share your order number?"}')
    result = await run(_context(provider, "where is my order", []))
    assert "order number" in result.text.lower()


async def test_run_on_provider_error_returns_safe_fallback() -> None:
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await run(_context(provider, "where is my order", []))
    assert "team" in result.text
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_order_tracking.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents.order_tracking'`.

- [ ] **Step 3: Implement**

`backend/app/agents/order_tracking.py`:

```python
from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError
from app.shopify.models import AuthorizedOrder

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

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


def _is_cancel_eligible(order: AuthorizedOrder) -> bool:
    if order.order.is_cancelled():
        return False
    # displayFulfillmentStatus is the closest available signal to "has this shipped" without
    # a live courier integration (Q10: none is built) -- UNFULFILLED/unset = not yet
    # dispatched, anything else is treated as dispatched.
    return order.order.fulfillment_status in (None, "UNFULFILLED")


def _order_context(orders: list[AuthorizedOrder]) -> str:
    if not orders:
        return "No order is linked to this WhatsApp number yet. Ask for their order number."
    lines = []
    for o in orders:
        lines.append(
            f"- order {o.order.name}: payment status {o.order.financial_status or 'unknown'}, "
            f"fulfillment {o.order.fulfillment_status or 'not dispatched'}, "
            f"cancelled: {o.order.is_cancelled()}, "
            f"cancel eligible: {_is_cancel_eligible(o)}"
        )
    return "\n".join(lines)


async def run(context: AgentContext) -> AgentReply:
    fallback = copy_for("error_fallback", "en")
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=PERSONALITY, order_context=_order_context(context.orders)
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
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_order_tracking.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/order_tracking.py tests/agents/test_order_tracking.py
git commit -m "feat: order_tracking agent -- dispatch-based cancel eligibility"
```

---

### Task 9: `app/agents/product_search.py`

**Files:**
- Create: `backend/app/agents/product_search.py`
- Create: `backend/tests/agents/test_product_search.py`

**Interfaces:**
- Produces: `app.agents.product_search.ProductSource` (Protocol:
  `async search_products(query: str, limit: int = 5) -> list[Product]` — `ShopifyClient`
  already matches this shape); `async run(context: AgentContext, shopify: ProductSource) -> AgentReply`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_product_search.py`:

```python
from app.agents.base import AgentContext
from app.agents.product_search import run
from app.providers.base import CompletionResult
from app.shopify.models import Money, Product


class _FixedProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        return CompletionResult(text=self._text, model=model)


class _FakeShopify:
    def __init__(self, products: list[Product] | None = None, raises: Exception | None = None) -> None:
        self.products = products or []
        self.raises = raises
        self.last_query: str | None = None

    async def search_products(self, query: str, limit: int = 5) -> list[Product]:
        self.last_query = query
        if self.raises:
            raise self.raises
        return self.products


def _context(provider, user_text: str) -> AgentContext:
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
    provider = _FixedProvider('{"reply": "I could not find that, let me connect you with our team."}')
    result = await run(_context(provider, "do you have a green saree"), shopify)
    assert result.text


async def test_run_on_shopify_outage_still_replies_without_crashing() -> None:
    from app.shopify.errors import ShopifyUnavailable

    shopify = _FakeShopify(raises=ShopifyUnavailable("down"))
    provider = _FixedProvider('{"reply": "Let me connect you with our team for that."}')
    result = await run(_context(provider, "do you have a red kurti"), shopify)
    assert result.text
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_product_search.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents.product_search'`.

- [ ] **Step 3: Implement**

`backend/app/agents/product_search.py`:

```python
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
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_product_search.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/product_search.py tests/agents/test_product_search.py
git commit -m "feat: product_search agent -- grounded-only, never-hallucinate catalog search"
```

---

### Task 10: `app/agents/policy.py` + real policy content

**Files:**
- Create: `backend/app/agents/policy.py`
- Modify: `backend/app/knowledge/seeds/{faq.json,business.json}`
- Create: `backend/tests/agents/test_policy.py`

**Interfaces:**
- Produces: `async run(context: AgentContext) -> AgentReply`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_policy.py`:

```python
from app.agents.base import AgentContext
from app.agents.policy import run
from app.providers.base import CompletionResult


class _FixedProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        return CompletionResult(text=self._text, model=model)


def _context(provider, user_text: str, knowledge: dict[str, str]) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=[], is_vip=False, knowledge=knowledge, provider=provider, model="m", api_key="k",
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
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_policy.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents.policy'`.

- [ ] **Step 3: Implement**

`backend/app/agents/policy.py`:

```python
from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError

_SYSTEM_TEMPLATE = """{personality}

Answer the customer's question using ONLY the store policy information below. Published
policy always takes precedence -- do not soften, contradict, or make exceptions to it even if
the customer pushes back. If the answer isn't covered by this information, say you're not
certain and offer to connect them with the team -- never guess or invent a policy detail.

Frequently asked questions:
{faq}

Store information:
{business}

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


async def run(context: AgentContext) -> AgentReply:
    fallback = copy_for("error_fallback", "en")
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=PERSONALITY,
        faq=context.knowledge.get("faq", ""),
        business=context.knowledge.get("business", ""),
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
```

Overwrite `backend/app/knowledge/seeds/faq.json` with the real, verbatim policy text
extracted from `D:\TAVAS Website\policy\*.docx` (already recorded in
`docs/FR/client-decisions-all.md`, round 3):

```json
[
  {"q": "How long does delivery take?", "a": "Orders are dispatched within 1-3 business days. Estimated delivery is 4-7 business days depending on your PIN code and courier availability."},
  {"q": "Do you offer Cash on Delivery?", "a": "Yes, COD is available only in eligible PIN codes."},
  {"q": "Can I cancel my order?", "a": "Orders can be cancelled only before dispatch. Once an order has been dispatched, it cannot be cancelled."},
  {"q": "Can I return a product?", "a": "We do not accept returns once a product has been delivered. The only exceptions are damaged, defective, or incorrect products -- notify us within 24 hours of delivery with a continuous unedited unboxing video and clear photographs showing the issue. Requests submitted after 24 hours or without this proof may be declined."},
  {"q": "Can I exchange a product for a different size?", "a": "Size exchange is allowed within 48 hours of delivery. The product must be unwashed and free from perfume, deodorant, makeup stains, dirt, or any signs of use. Exchange is subject to stock availability, and exchange shipping charges are paid by the customer."},
  {"q": "What if my product is damaged, defective, or incorrect?", "a": "After verification, we provide a replacement subject to stock availability. If a replacement is unavailable, a full refund is issued."},
  {"q": "How do refunds work?", "a": "Refunds are issued only when an approved damaged, defective, or incorrect product cannot be replaced. Refunds are processed to your original payment method within 3-5 business days after approval; bank processing times may vary."},
  {"q": "Do you sell products in this chat?", "a": "I can help you find products and check availability, but all orders are placed on our website."}
]
```

Overwrite `backend/app/knowledge/seeds/business.json` with:

```json
{
  "store_name": "Thetavas",
  "website": "https://thetavas.myshopify.com",
  "instagram": "",
  "support_phone": "",
  "support_email": "",
  "support_hours": "",
  "note": "Support contact details are filled in by the store team from the admin panel.",
  "policy_notes": "Slight colour variation due to lighting, photography, or screen settings is not a defect. Minor variation in print placement, handwork, embroidery, stitching, or fabric texture is normal, not a manufacturing defect. All exchange/refund requests are subject to quality inspection. We do not sell your personal information; it is used only to process orders, provide support, and send order updates.",
  "extra": {}
}
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_policy.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/policy.py app/knowledge/seeds/faq.json app/knowledge/seeds/business.json tests/agents/test_policy.py
git commit -m "feat: policy agent + real extracted policy text (shipping/return/exchange/refund)"
```

---

### Task 11: `app/agents/recommendations.py`

**Files:**
- Create: `backend/app/agents/recommendations.py`
- Create: `backend/tests/agents/test_recommendations.py`

**Interfaces:**
- Consumes: `app.agents.product_search.ProductSource` (reused, not redefined).
- Produces: `async run(context: AgentContext, shopify: ProductSource) -> AgentReply`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_recommendations.py`:

```python
from app.agents.base import AgentContext
from app.agents.recommendations import run
from app.providers.base import CompletionResult
from app.shopify.models import Money, Product


class _FixedProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        return CompletionResult(text=self._text, model=model)


class _FakeShopify:
    def __init__(self, products: list[Product] | None = None) -> None:
        self.products = products or []

    async def search_products(self, query: str, limit: int = 5) -> list[Product]:
        return self.products


def _context(provider, user_text: str) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=[], is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None,
    )


async def test_run_recommends_from_real_search_results() -> None:
    products = [
        Product(gid="1", title="Gold Jhumka Earrings", handle="jhumka", price=Money("499", "INR"),
                available=True, product_type="Accessory", tags=())
    ]
    shopify = _FakeShopify(products=products)
    provider = _FixedProvider('{"reply": "These Gold Jhumka Earrings would pair beautifully!"}')
    result = await run(_context(provider, "what goes with a red kurti"), shopify)
    assert "Jhumka" in result.text


async def test_run_answers_original_question_first_when_no_matches() -> None:
    shopify = _FakeShopify(products=[])
    provider = _FixedProvider('{"reply": "I don\'t have a specific match, but I can connect you with our team."}')
    result = await run(_context(provider, "what goes with a green saree"), shopify)
    assert result.text
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_recommendations.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents.recommendations'`.

- [ ] **Step 3: Implement**

`backend/app/agents/recommendations.py`:

```python
from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.agents.product_search import ProductSource
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError
from app.shopify.errors import ShopifyError

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


def _results_context(products: list) -> str:
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
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_recommendations.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/recommendations.py tests/agents/test_recommendations.py
git commit -m "feat: recommendations agent -- grounded cross-sell/upsell, narrow scope"
```

---

### Task 12: `app/agents/customer_support.py` (handoff logic)

**Files:**
- Create: `backend/app/agents/customer_support.py`
- Create: `backend/tests/agents/test_customer_support.py`

**Interfaces:**
- Consumes: `app.store.base.ConversationStore`.
- Produces: `async run(context: AgentContext, store: ConversationStore, conversation_id: int, now: datetime) -> AgentReply`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_customer_support.py`:

```python
from datetime import UTC, datetime, timedelta

from app.agents.base import AgentContext
from app.agents.customer_support import run
from app.providers.base import CompletionResult, ProviderError, ProviderErrorKind
from app.store.memory import InMemoryConversationStore


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


def _context(provider, user_text: str) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=[], is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None,
    )


async def test_greeting_replies_normally_no_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(text='{"reply": "Hello! How can I help you today?"}')
    result = await run(_context(provider, "hi"), store, conversation_id, datetime.now(UTC))
    assert result.handoff is False
    assert await store.get_paused_until(conversation_id) is None


async def test_first_human_request_gets_one_attempt_not_immediate_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(text='{"reply": "I will do my best to help -- what do you need?"}')
    result = await run(
        _context(provider, "I want to talk to a human"), store, conversation_id, datetime.now(UTC)
    )
    assert result.handoff is False
    assert await store.get_handoff_attempted_at(conversation_id) is not None
    assert await store.get_paused_until(conversation_id) is None


async def test_second_human_request_triggers_immediate_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    now = datetime.now(UTC)
    await store.mark_handoff_attempted(conversation_id, now - timedelta(minutes=5))
    provider = _FixedProvider(text="should not be called for a decisive second ask")
    result = await run(_context(provider, "I need a real person"), store, conversation_id, now)
    assert result.handoff is True
    paused = await store.get_paused_until(conversation_id)
    assert paused is not None and paused > now


async def test_provider_failure_triggers_handoff() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    provider = _FixedProvider(raises=ProviderError("down", ProviderErrorKind.TIMEOUT))
    result = await run(_context(provider, "hi"), store, conversation_id, datetime.now(UTC))
    assert result.handoff is True
    assert await store.get_paused_until(conversation_id) is not None


async def test_handoff_attempt_outside_24h_window_resets() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    now = datetime.now(UTC)
    await store.mark_handoff_attempted(conversation_id, now - timedelta(hours=25))
    provider = _FixedProvider(text='{"reply": "Sure, let me help."}')
    result = await run(_context(provider, "talk to a human"), store, conversation_id, now)
    assert result.handoff is False
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/agents/test_customer_support.py -v`
Expected: `ModuleNotFoundError: No module named 'app.agents.customer_support'`.

- [ ] **Step 3: Implement**

`backend/app/agents/customer_support.py`:

```python
from datetime import datetime, timedelta

from app.agents.base import PERSONALITY, AgentContext, AgentReply, extract_reply_text
from app.channels.copy import copy_for
from app.providers.base import Message, ProviderError
from app.store.base import ConversationStore

HANDOFF_MESSAGE = (
    "I'm connecting you with our team -- they'll continue helping you right here in this chat."
)

_HANDOFF_WINDOW = timedelta(hours=24)

_HUMAN_REQUEST_PHRASES = (
    "talk to a human",
    "speak to a human",
    "real person",
    "human agent",
    "talk to someone",
    "speak to someone",
    "human please",
    "connect me to",
    "escalate",
)

_SYSTEM_TEMPLATE = """{personality}

The customer's message didn't clearly match order tracking, product search, policy, or
recommendations -- help with greetings, small talk, or general questions as best you can. If
you cannot help, say so honestly.

Respond with STRICT JSON only, no other text: {{"reply": "<your reply to the customer>"}}
"""


def _wants_human(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _HUMAN_REQUEST_PHRASES)


async def _already_attempted_recently(
    store: ConversationStore, conversation_id: int, now: datetime
) -> bool:
    attempted_at = await store.get_handoff_attempted_at(conversation_id)
    if attempted_at is None:
        return False
    return (now - attempted_at) < _HANDOFF_WINDOW


async def _handoff(store: ConversationStore, conversation_id: int, now: datetime, text: str) -> AgentReply:
    await store.pause_until(conversation_id, now + _HANDOFF_WINDOW)
    return AgentReply(text=text, handoff=True)


async def run(
    context: AgentContext, store: ConversationStore, conversation_id: int, now: datetime
) -> AgentReply:
    """One AI attempt per conversation window, then immediate handoff (client decision,
    round 3 2026-08-06). A second explicit human request within the window skips the LLM
    call entirely -- deterministic, no persuasion attempted. Any provider failure, or the
    LLM's own safe-fallback degradation, is also treated as "could not help" -> handoff.
    """
    fallback = copy_for("error_fallback", "en")
    wants_human = _wants_human(context.user_text)
    already_attempted = await _already_attempted_recently(store, conversation_id, now)

    if wants_human and already_attempted:
        return await _handoff(store, conversation_id, now, HANDOFF_MESSAGE)

    if wants_human and not already_attempted:
        await store.mark_handoff_attempted(conversation_id, now)

    system_prompt = _SYSTEM_TEMPLATE.format(personality=PERSONALITY)
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
        return await _handoff(store, conversation_id, now, f"{fallback} {HANDOFF_MESSAGE}")

    reply = extract_reply_text(result.text, fallback)
    if reply == fallback:
        return await _handoff(store, conversation_id, now, f"{reply} {HANDOFF_MESSAGE}")
    return AgentReply(text=reply)
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/agents/test_customer_support.py -v`
Expected: all PASS. Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/agents/customer_support.py tests/agents/test_customer_support.py
git commit -m "feat: customer_support agent -- one-attempt-then-handoff, deterministic detection"
```

---

### Task 13: `deps.py` wiring — `Container.conversations` + `active_llm`

**Files:**
- Modify: `backend/app/deps.py`
- Modify: `backend/tests/test_deps.py`

**Interfaces:**
- Produces: `Container.conversations: ConversationStore` (new field);
  `async active_llm(settings: Settings, config: ConfigService) -> tuple[LLMProvider, str, str, dict[str, object] | None] | None`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_deps.py`:

```python
from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.store.memory import InMemoryConfigRepo


def _config(master_key: str) -> ConfigService:
    return ConfigService(InMemoryConfigRepo(), SecretVault(master_key))


async def test_active_llm_returns_none_when_unconfigured(master_key: str) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    result = await active_llm(settings, _config(master_key))
    assert result is None


async def test_active_llm_returns_provider_for_api_key_provider(master_key: str) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    config = _config(master_key)
    await config.set_secret("llm:api_key:gemini", "test-key")
    await config.set_plain("llm:active_provider", "gemini")

    result = await active_llm(settings, config)

    assert result is not None
    provider, model, api_key, extra_params = result
    assert api_key == "test-key"
    assert model == "gemini/gemini-flash-latest"


async def test_active_llm_env_provider_needs_no_stored_key(master_key: str) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    config = _config(master_key)
    await config.set_plain("llm:active_provider", "vertex")

    result = await active_llm(settings, config)

    assert result is not None
    _, model, api_key, _ = result
    assert api_key == ""
    assert model == "vertex_ai/gemini-3.5-flash"


async def test_active_llm_returns_none_if_api_key_provider_has_no_stored_key(
    master_key: str,
) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    config = _config(master_key)
    await config.set_plain("llm:active_provider", "gemini")

    result = await active_llm(settings, config)

    assert result is None


def test_container_has_conversations_store(master_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.deps import get_container, reset_container

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    assert c.conversations is not None
    reset_container()
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_deps.py -v`
Expected: `ImportError: cannot import name 'active_llm'` and
`AttributeError: 'Container' object has no attribute 'conversations'`.

- [ ] **Step 3: Implement**

Modify `backend/app/deps.py` — update imports, `Container`, `get_container`, add `active_llm`:

```python
from dataclasses import dataclass

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.providers.base import LLMProvider
from app.providers.litellm_provider import LiteLLMProvider, VertexConfig
from app.providers.registry import get_provider
from app.shopify.client import ShopifyClient
from app.shopify.token_manager import TokenManager
from app.store.base import ConfigRepo, ConversationStore, IngestStore, MessageStore
from app.store.memory import (
    InMemoryConfigRepo,
    InMemoryConversationStore,
    InMemoryIngestStore,
    InMemoryMessageStore,
)
from app.store.pg_factory import LazyPool
from app.store.postgres import (
    PostgresConfigRepo,
    PostgresConversationStore,
    PostgresIngestStore,
    PostgresMessageStore,
)


@dataclass
class Container:
    settings: Settings
    vault: SecretVault
    config_repo: ConfigRepo
    config: ConfigService
    http: httpx.AsyncClient
    tokens: TokenManager
    shopify: ShopifyClient
    ingest: IngestStore
    messages: MessageStore
    conversations: ConversationStore


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        settings = Settings()  # type: ignore[call-arg]  # app_master_key comes from env/.env
        vault = SecretVault(settings.app_master_key)
        if settings.database_url:
            pool = LazyPool(settings.database_url)
            config_repo: ConfigRepo = PostgresConfigRepo(pool)
            ingest: IngestStore = PostgresIngestStore(pool)
            messages: MessageStore = PostgresMessageStore(pool)
            conversations: ConversationStore = PostgresConversationStore(pool)
        else:
            config_repo = InMemoryConfigRepo()
            ingest = InMemoryIngestStore()
            messages = InMemoryMessageStore()
            conversations = InMemoryConversationStore()
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient(follow_redirects=False)  # never replay the token to a redirect
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(
            settings, vault, config_repo, config, http, tokens, shopify, ingest, messages,
            conversations,
        )
    return _container


def reset_container() -> None:
    global _container
    _container = None


def build_provider(settings: Settings) -> LiteLLMProvider:
    """Construct the LLM verifier with Vertex env-credentials wired in.

    Built at call time (never at import), so the webhook cold path never pays for it and the
    Vertex service-account JSON is read from env-sourced settings only when a verify is requested.
    ``LiteLLMProvider`` still imports litellm lazily inside ``complete`` — nothing here triggers it.
    """
    vertex = VertexConfig(
        credentials_json=settings.vertex_credentials_json or None,
        project=settings.vertex_project or None,
        location=settings.vertex_location,
    )
    return LiteLLMProvider(vertex=vertex)


async def active_llm(
    settings: Settings, config: ConfigService
) -> tuple[LLMProvider, str, str, dict[str, object] | None] | None:
    """Resolve the LLM provider the owner activated in the admin panel, ready to call.

    Mirrors the admin panel's own resolution (`llm:active_provider` / `llm:api_key:{provider}`)
    so the conversation engine always uses whatever is currently configured there. Returns
    None if nothing is active yet, or an api_key provider is active but has no stored key.
    """
    active = await config.get_plain("llm:active_provider")
    if active is None:
        return None
    info = get_provider(active)
    if info is None:
        return None
    provider = build_provider(settings)
    if info.auth_kind == "env":
        return provider, info.default_model, "", info.request_params
    api_key = await config.get_secret(f"llm:api_key:{active}")
    if api_key is None:
        return None
    return provider, info.default_model, api_key, info.request_params
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_deps.py -v`
Expected: all PASS. Also re-run `python -m pytest -q` (full suite) to catch any other call
site constructing `Container` directly — there should be none outside `deps.py` itself.

Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/deps.py tests/test_deps.py
git commit -m "feat: wire ConversationStore into Container + active_llm provider resolution"
```

---

### Task 14: Wire `InboundText` into the router + agent pipeline

**Files:**
- Modify: `backend/app/channels/whatsapp.py`
- Modify: `backend/tests/test_whatsapp_webhook.py`

**Interfaces:**
- Produces: `POST /webhook/whatsapp` now runs the full router → specialist pipeline for every
  fresh `InboundText` event, gated by `AdminControls.send_mode` (`off` = pipeline does not run
  at all; `shadow` = runs and persists but does not call `send_text`; `allowlist` = sends only
  to numbers in `allowlist_phones`; `live` = always sends), and by
  `conversations.paused_until` (paused = message recorded, AI stays silent). Existing response
  shape (`{"ok": True, "processed": N, "duplicate": N, "results": [...]}`) is unchanged.
  `InboundButton`/`InboundInteractive` events are untouched.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_whatsapp_webhook.py` (uses the file's existing `envelope`,
`sign`, `post`, and `_fresh` fixture — do not redefine them):

```python
async def test_post_text_event_without_llm_configured_sends_safe_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["to"] = to
        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="wamid.reply", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "where is my order"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert resp.json()["processed"] == 1
    assert sent["to"] == "919999999999"
    assert "team" in sent["body"]


async def test_post_text_event_send_mode_off_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    # send_mode defaults to "off" -- no controls saved in this test.

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text2",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False


async def test_post_text_event_paused_conversation_stays_silent_but_records_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    from datetime import UTC, datetime, timedelta

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    conversation_id = await c.conversations.get_or_create("919999999999")
    await c.conversations.pause_until(conversation_id, datetime.now(UTC) + timedelta(hours=1))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text3",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "still there?"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert any(m.content == "still there?" for m in messages)


async def test_post_text_event_shadow_mode_processes_but_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="shadow"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text4",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False
    conversation_id = await c.conversations.get_or_create("919999999999")
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert len(messages) == 2  # user turn + assistant turn, persisted even though not sent


async def test_post_button_tap_still_unaffected_by_conversation_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 4 only wires InboundText -- button/interactive taps still just echo event_type."""
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        called["sent"] = True
        raise AssertionError("send_text must not be called for a button tap")

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.btn1",
                "timestamp": "1",
                "type": "button",
                "button": {"text": "Confirm Order", "payload": "order:confirm:gid://1"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.json() == {
        "ok": True,
        "processed": 1,
        "duplicate": 0,
        "results": [
            {"message_id": "wamid.btn1", "duplicate": False, "event_type": "InboundButton"}
        ],
    }
    assert called["sent"] is False
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_whatsapp_webhook.py -v`
Expected: new tests FAIL (nothing wires `InboundText` to the pipeline yet).

- [ ] **Step 3: Implement**

Modify `backend/app/channels/whatsapp.py`. Update the import block:

```python
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.admin.controls import load_controls
from app.agents import customer_support, order_tracking, policy, product_search, recommendations
from app.agents.router import classify_intent
from app.channels.copy import copy_for
from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import InboundText, extract_events
from app.channels.whatsapp_sender import send_text
from app.channels.whatsapp_signature import verify_meta_hmac
from app.config.crypto import VaultError
from app.core.memory import load_history, persist_turn
from app.core.order_resolver import resolve_by_phone
from app.core.phone import normalize_phone
from app.core.sanitize import strip_markdown
from app.deps import Container, active_llm, get_container
from app.knowledge.loader import SEEDS_DIR, KnowledgeLoader

router = APIRouter()
logger = logging.getLogger("app.channels.whatsapp")

MAX_WEBHOOK_BODY_BYTES = 1_048_576
```

Add this function above `receive_webhook` (after `_ascii_compare`, before `verify_webhook`):

```python
async def _run_agent(context, intent, c: Container, conversation_id: int, now: datetime):
    if intent == "order_tracking":
        return await order_tracking.run(context)
    if intent == "product_search":
        return await product_search.run(context, c.shopify)
    if intent == "policy":
        return await policy.run(context)
    if intent == "recommendations":
        return await recommendations.run(context, c.shopify)
    return await customer_support.run(context, c.conversations, conversation_id, now)


async def _handle_text_event(c: Container, event: InboundText) -> None:
    """Run one conversation turn for a fresh inbound text message and send the reply.

    Failures anywhere in this pipeline (Shopify, the LLM provider, sending the reply) are
    swallowed here -- the webhook must still ack 200 for a message it already deduped, and a
    failed reply is a degraded conversation, not a failed webhook delivery.
    """
    from app.agents.base import AgentContext

    try:
        controls = await load_controls(c.config)
        if controls.send_mode == "off":
            return
        wa_cfg = await load_whatsapp_config(c.config)
        if wa_cfg is None:
            return

        conversation_id, history = await load_history(c.conversations, event.wa_id)
        now = datetime.now(UTC)
        paused_until = await c.conversations.get_paused_until(conversation_id)
        if paused_until is not None and now < paused_until:
            await c.conversations.append_message(conversation_id, "user", event.text)
            return

        phone = normalize_phone(event.wa_id)
        orders = await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
        order_count = await c.ingest.count_orders_by_phone(phone) if phone else 0
        is_vip = order_count >= controls.vip_order_count_threshold

        llm = await active_llm(c.settings, c.config)
        if llm is None:
            reply_text = copy_for("error_fallback", "en")
        else:
            provider, model, api_key, extra_params = llm
            loader = KnowledgeLoader(c.config_repo, SEEDS_DIR)
            knowledge = await loader.assemble_all()
            context = AgentContext(
                wa_id=event.wa_id,
                phone_e164=phone or event.wa_id,
                user_text=event.text,
                history=history,
                orders=orders,
                is_vip=is_vip,
                knowledge=knowledge,
                provider=provider,
                model=model,
                api_key=api_key,
                extra_params=extra_params,
            )
            intent = await classify_intent(
                provider, model, api_key, event.text, extra_params=extra_params
            )
            agent_reply = await _run_agent(context, intent, c, conversation_id, now)
            reply_text = strip_markdown(agent_reply.text)

        await persist_turn(c.conversations, conversation_id, event.text, reply_text)

        if controls.send_mode == "shadow":
            return
        if controls.send_mode == "allowlist":
            if phone is None or phone not in controls.allowlist_phones:
                return

        await send_text(c.http, wa_cfg, event.wa_id, reply_text)
    except Exception:
        logger.exception("conversation turn failed for a fresh inbound text message")
```

Modify the event loop inside `receive_webhook` — replace:

```python
    results: list[dict[str, Any]] = []
    processed = 0
    duplicate = 0
    for event in events:
        is_new = await c.messages.record_if_new(event.message_id)
        if is_new:
            processed += 1
        else:
            duplicate += 1
        results.append(
            {
                "message_id": event.message_id,
                "duplicate": not is_new,
                "event_type": type(event).__name__,
            }
        )

    # Routing each fresh event to the deterministic button dispatcher (Phase 5) and
    # the conversation engine / order_resolver (Phase 4) attaches here. Phase 3 is
    # the pipe only.
    return JSONResponse(
        {"ok": True, "processed": processed, "duplicate": duplicate, "results": results}
    )
```

with:

```python
    results: list[dict[str, Any]] = []
    processed = 0
    duplicate = 0
    for event in events:
        is_new = await c.messages.record_if_new(event.message_id)
        if is_new:
            processed += 1
            if isinstance(event, InboundText):
                await _handle_text_event(c, event)
        else:
            duplicate += 1
        results.append(
            {
                "message_id": event.message_id,
                "duplicate": not is_new,
                "event_type": type(event).__name__,
            }
        )

    # Deterministic button-tap dispatch (order:confirm/cancel -> tagsAdd/orderCancel) is
    # Phase 5. InboundText already runs the full router + agent pipeline above.
    return JSONResponse(
        {"ok": True, "processed": processed, "duplicate": duplicate, "results": results}
    )
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_whatsapp_webhook.py -v`
Expected: all PASS.

Then run the full suite: `python -m pytest -q` — all green, Postgres-gated tests SKIP without
`TEST_DATABASE_URL`.

Then `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/channels/whatsapp.py tests/test_whatsapp_webhook.py
git commit -m "feat: wire InboundText to router + 5-agent pipeline, gated on send_mode + pause"
```

---

### Task 15: Registries + pipeline status + final sweep

**Files:**
- Modify: `docs/memory/component_registry.md`, `docs/memory/api_registry.md`, `docs/FR/_pipeline_status.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update `docs/memory/component_registry.md`**

Add an entry for each new/extended module: `app/core/order_resolver.py`, `app/core/sanitize.py`,
`app/core/memory.py`, `ConversationStore` (+ pause/handoff methods, in-memory + Postgres),
`IngestStore.find_mappings_by_phone`/`count_orders_by_phone`, `AdminControls.vip_order_count_threshold`,
`ShopifyClient.search_products` + `Product`, `app/agents/{base,router,order_tracking,
product_search,policy,recommendations,customer_support}.py`, `deps.active_llm`,
`Container.conversations`. Follow the existing entry format (check the most recent entries as
the template).

- [ ] **Step 2: Update `docs/memory/api_registry.md`**

Note that `POST /webhook/whatsapp` now runs the full router → agent pipeline for `InboundText`
events (previously pipe-only, echo-only), gated by `send_mode` and `paused_until`. No new HTTP
routes were added.

- [ ] **Step 3: Update `docs/FR/_pipeline_status.md`**

Add a row marking Phase 4 as **BUILT — READY FOR REVIEW** (not "complete/closed" —
`code-reviewer` and `security-reviewer` have not run yet). Include: final test count from
Step 4, commit range, and note that this design (subagent architecture, real policy content,
VIP-by-order-count, dispatch-based cancellation, handoff via `paused_until`/
`handoff_attempted_at`) supersedes and replaces the 2026-08-04 single-inline-prompt plan, which
was never executed.

- [ ] **Step 4: Final full-project sweep**

Run (from `backend/`):
```
python -m pytest -q
ruff check .
mypy app
```
All green; Postgres-gated tests SKIP without `TEST_DATABASE_URL`. Then run the secrets grep
across every file touched in this plan (Tasks 1–14) — must return EMPTY.

- [ ] **Step 5: Commit**

```bash
git add docs/memory/component_registry.md docs/memory/api_registry.md docs/FR/_pipeline_status.md
git commit -m "docs: Phase 4 subagent architecture registry updates + pipeline status"
```

---

## Self-Review (done at plan time)

- **Spec coverage:** every section of `docs/superpowers/specs/2026-08-06-phase4-subagent-architecture-design.md`
  maps to a task — architecture/module structure (Tasks 6–14), data model additions (Task 3
  pause/handoff, Task 4 VIP), product search (Task 5 Shopify capability, Task 9 agent), policy
  grounding (Task 10, incl. real extracted text), personality (Task 6), human handoff protocol
  (Task 12), order tracking dispatch-based cancellation (Task 8), error handling (baked into
  every agent task + Task 14's outer try/except), testing (every task has its own test file).
- **Deliberately absent, and why:** a second routing pass / re-classification when an agent
  gets a poor-fit message (spec's explicit "out of scope" list) — not built. Cross-cutting
  recommendations inside other agents' replies — not built, `recommendations` stays the only
  entry point (explicit brainstorming decision). Live courier/a2ship tracking — not built (Q10).
  Vector/semantic product search — not built (Approach A only, spec's explicit choice).
- **Placeholders:** none — every step has complete, runnable code.
- **Type consistency:** `AgentContext`/`AgentReply` (Task 6) used identically across Tasks
  8–12 and Task 14's wiring. `ProductSource` (Task 9) reused verbatim by `recommendations`
  (Task 11), not redefined. `ConversationStore`'s pause/handoff method names (Task 3) match
  exactly what `customer_support.py` (Task 12) and `whatsapp.py` (Task 14) call.
  `extract_json_blob`/`extract_reply_text`/`PERSONALITY` (Task 6) are the single source every
  agent imports — no agent redefines its own parsing logic.
- **Corrected during planning, not silently:** the spec's original claim that `total_spend`
  could be computed from `order_mappings` was found to be wrong (no amount column) — resolved
  with the owner before this plan was written (VIP uses `order_count` only), and the spec file
  itself was corrected to match, not just this plan.
- **Open item for the owner, not blocking this plan:** Q6 (no-match fallback: support-contact-
  only vs. also-alert-staff) and Q13 (tag-name compatibility) remain open client questions,
  unrelated to this build. The Postgres-backed rate limiter (separate client decision) and the
  three tracked LOW/INFO security items from the DPDP review are explicitly out of scope here.
