# Phase 4 — THE CONVERSATION Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a live, LLM-backed reply to every fresh inbound WhatsApp text message: resolve who the customer is and which Shopify orders they own, assemble a knowledge-grounded prompt, call the configured LLM provider, parse its reply defensively, and send it back — never inventing policy, never mutating anything, never crashing the webhook.

**Architecture:** Design is `docs/inbound-conversation-design.md` (owner-approved, 10-step flow). Phase 3.5 already delivered two of the five modules that design originally called for — `app/providers/` (LiteLLM + registry, incl. Vertex) and `app/knowledge/loader.py` (`KnowledgeLoader.assemble_all()` covers the "assembler" role) — so this plan builds only what's left: `app/core/order_resolver.py`, `app/core/sanitize.py`, `app/core/memory.py` (+ a new `ConversationStore` in the store layer), `app/core/engine.py`, and wiring `app/channels/whatsapp.py`'s `InboundText` path to call all of it. `InboundButton`/`InboundInteractive` (template/reply-button taps) stay untouched — deterministic button dispatch and mutation execution are Phase 5, per the design's explicit re-sequencing.

**Tech Stack:** Same as Phases 1–3.5 — FastAPI, Pydantic v2, httpx, asyncpg (Postgres) / in-memory, LiteLLM (already wired by Phase 3.5), pytest + pytest-asyncio, ruff, mypy strict.

## Global Constraints

- Python 3.12+ syntax, full type hints, `mypy` strict clean, `ruff check .` clean — run against the WHOLE project (`backend/`), not just touched files, every task (error_learnings: plan-verbatim code can exceed this repo's line-length; test files are lint targets too).
- Secrets grep after every new/modified file: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+"` must return EMPTY.
- No `print()` — use `logging` (matches `app.admin`'s existing `logger = logging.getLogger("app.admin")` pattern; use `logging.getLogger("app.core")` / `"app.channels.whatsapp"` as appropriate).
- No bare `except:` — catch specific exception types (`ShopifyError`, `ProviderError`, etc.), never a bare `except Exception:` unless the call site is explicitly documented as a last-resort catch-all (only `_handle_text_event` in Task 6 qualifies).
- **The LLM never mutates anything** (Critical Rule 2). This phase only produces `EngineReply.intent` as a classification — no button, no `tagsAdd`, no `orderCancel` is ever called from this code path. That is Phase 5's job.
- **Ownership check before revealing anything** (Critical Rule 3): every order shown to the LLM or the customer must have passed through `AuthorizedOrder`'s own runtime invariant (raises `ValueError` unless `verified_phone` matches one of the order's phones) — never construct one without it.
- **Always re-fetch live order state** (rule 5.4): `order_resolver` re-fetches every order from Shopify by gid even when a DB mapping already named it — the mapping is a lookup index, never a data source.
- **Never leak a raw completion or a raw exception to the customer.** Any provider failure or unparseable JSON degrades to the existing `copy_for("error_fallback", "en")` fixed string from `app/channels/copy.py` — never the raw text.
- **No git push** — local commits only, `Co-Authored-By: Claude <noreply@anthropic.com>` trailer, conventional commit messages (`feat:`).
- Grep `docs/memory/error_learnings.md` and `docs/memory/component_registry.md` before starting — several entries apply directly here: non-ASCII `hmac.compare_digest` (not relevant to new code but don't reintroduce it), "a secret a library may reformat can't be redacted by substring replace — discard the raw error" (already handled by the existing `ProviderError`/Vertex path this plan calls into, don't undo it), and "treat every payload field as attacker-typed" (the LLM's JSON output is exactly this — untrusted input, not just the webhook body).
- **Deliberate scope decision (read before objecting to what's missing):** v1 memory is windowed history only — no LLM-driven rolling summary. The `conversations.running_summary` column exists in the schema for a future phase; this plan does not write to it. Building a summarization sub-feature (its own prompt, its own LLM call, its own tests) is out of scope until a real need for longer context is observed (YAGNI).
- **Deliberate design extension (flag to the owner in your final report, don't silently skip it):** `AdminControls.send_mode` (the ADR-002 kill switch, previously scoped only to the outbound order-push in Phase 5) is reused here to gate the conversation reply too — `off` = do not even run the pipeline, `shadow` = run everything and persist the turn but do not call `send_text`, `allowlist` = only send to a number in `allowlist_phones`, `live` = always send. This is the same existing admin-panel control, not a new one; it now also means "process it but do not disturb the customer" for the reactive reply path, not only the proactive push.

---

## File Structure

```
backend/
  app/store/base.py                 # + IngestStore.find_mappings_by_phone, StoredMessage, ConversationStore  (modify)
  app/store/memory.py                # + InMemoryIngestStore.find_mappings_by_phone, InMemoryConversationStore  (modify)
  app/store/postgres.py              # + PostgresIngestStore.find_mappings_by_phone, PostgresConversationStore  (modify)
  app/core/order_resolver.py         # OrderSource protocol, resolve_by_phone, resolve_by_order_name  (create)
  app/core/sanitize.py                # strip_markdown  (create)
  app/core/memory.py                  # load_history, persist_turn  (create)
  app/core/engine.py                  # build_system_prompt, run_turn, EngineReply  (create)
  app/deps.py                         # + Container.conversations, active_llm()  (modify)
  app/channels/whatsapp.py            # InboundText -> full conversation pipeline  (modify)
  tests/test_ingest_store.py          # + find_mappings_by_phone tests  (modify)
  tests/store/test_conversation_store.py           # + Postgres-gated test  (create)
  tests/core/__init__.py                            # (create)
  tests/core/test_order_resolver.py                 # (create)
  tests/core/test_sanitize.py                       # (create)
  tests/core/test_memory.py                         # (create)
  tests/core/test_engine.py                         # (create)
  tests/test_deps.py                  # + active_llm tests  (modify)
  tests/test_whatsapp_webhook.py      # + conversation wiring tests  (modify)
  docs/memory/component_registry.md   # (modify)
  docs/memory/api_registry.md         # (modify)
  docs/FR/_pipeline_status.md         # (modify)
```

---

### Task 1: `IngestStore.find_mappings_by_phone` + `core/order_resolver.py`

**Files:**
- Modify: `backend/app/store/base.py`
- Modify: `backend/app/store/memory.py`
- Modify: `backend/app/store/postgres.py`
- Create: `backend/app/core/order_resolver.py`
- Modify: `backend/tests/test_ingest_store.py`
- Create: `backend/tests/core/__init__.py` (empty)
- Create: `backend/tests/core/test_order_resolver.py`

**Interfaces:**
- Consumes: `app.shopify.errors.ShopifyError`, `app.shopify.models.{AuthorizedOrder, Order}`, `app.core.phone.normalize_phone`, `app.store.base.{IngestStore, MappingView}`.
- Produces: `IngestStore.find_mappings_by_phone(phone_e164: str, limit: int = 20) -> list[MappingView]`; `app.core.order_resolver.OrderSource` (Protocol: `get_order`, `find_order_by_name`, `find_customer_orders_by_phone` — matches `ShopifyClient`'s existing shape structurally, no import of the concrete class, per the ports-&-adapters layering rule); `async resolve_by_phone(shopify: OrderSource, ingest: IngestStore, wa_id: str) -> list[AuthorizedOrder]`; `async resolve_by_order_name(shopify: OrderSource, wa_id: str, raw_name: str) -> AuthorizedOrder | None`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_ingest_store.py` — append at the end of the file:

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
    # DB mapping says +919999999999, but the LIVE order (re-fetched, rule 5.4) now shows a
    # different phone -- AuthorizedOrder's own invariant rejects it; must not crash or leak it.
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
    order = _order("gid://6", "tavas6", "+911111111111")  # someone else's order
    shopify = _FakeShopify(orders_by_name={"tavas6": order})

    result = await resolve_by_order_name(shopify, "919999999999", "tavas6")

    assert result is None


async def test_resolve_by_order_name_not_found_returns_none() -> None:
    result = await resolve_by_order_name(_FakeShopify(), "919999999999", "tavas999")
    assert result is None


async def test_resolve_by_order_name_shopify_outage_returns_none() -> None:
    from app.shopify.errors import ShopifyUnavailable

    shopify = _FakeShopify(raises=ShopifyUnavailable("down"))
    result = await resolve_by_order_name(shopify, "919999999999", "tavas7")
    assert result is None
```

- [ ] **Step 2: Run to verify FAIL**

Run (from `backend/`): `python -m pytest tests/test_ingest_store.py tests/core/test_order_resolver.py -v`
Expected: `ModuleNotFoundError: No module named 'app.core.order_resolver'` and `AttributeError: 'InMemoryIngestStore' object has no attribute 'find_mappings_by_phone'`.

- [ ] **Step 3: Implement**

Append to `backend/app/store/base.py`, inside the `IngestStore` Protocol (after `recent_outbound`):

```python
    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]: ...
```

Append to `backend/app/store/memory.py`, inside `InMemoryIngestStore` (after `recent_outbound`):

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

Append to `backend/app/store/postgres.py`, inside `PostgresIngestStore` (after `recent_outbound`):

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
    snapshot, rule 5.4) and re-verified through AuthorizedOrder's own ownership invariant,
    which raises if the live phone no longer matches -- a stale mapping is silently dropped,
    never surfaced. A Shopify outage degrades to whatever was already resolved (often empty)
    rather than raising, so a temporary Shopify blip does not stop the conversation.
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
Expected: all PASS.

Then: `ruff check .` and `mypy app` from `backend/` — both clean.

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
Expected: all PASS.

Then: `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/core/sanitize.py tests/core/test_sanitize.py
git commit -m "feat: strip_markdown -- convert LLM Markdown to WhatsApp-safe plain text"
```

---

### Task 3: `ConversationStore` (store layer) + `core/memory.py`

**Files:**
- Modify: `backend/app/store/base.py`
- Modify: `backend/app/store/memory.py`
- Modify: `backend/app/store/postgres.py`
- Create: `backend/app/core/memory.py`
- Create: `backend/tests/store/test_conversation_store.py`
- Create: `backend/tests/core/test_memory.py`

**Interfaces:**
- Consumes: `app.providers.base.Message`.
- Produces: `app.store.base.StoredMessage` (frozen dataclass: `role: str`, `content: str`, `created_at: str | None`); `app.store.base.ConversationStore` Protocol (`get_or_create(user_id: str) -> int`, `recent_messages(conversation_id: int, limit: int) -> list[StoredMessage]`, `append_message(conversation_id: int, role: str, content: str) -> None`); `InMemoryConversationStore`, `PostgresConversationStore`; `app.core.memory.DEFAULT_WINDOW`, `async load_history(store: ConversationStore, wa_id: str, window: int = DEFAULT_WINDOW) -> tuple[int, list[Message]]`, `async persist_turn(store: ConversationStore, conversation_id: int, user_text: str, assistant_reply: str) -> None`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_conversation_store.py`:

```python
import os

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
Expected: `ImportError: cannot import name 'InMemoryConversationStore'` and `ModuleNotFoundError: No module named 'app.core.memory'`.

- [ ] **Step 3: Implement**

Append to `backend/app/store/base.py` (after the `MessageStore` Protocol at the end of the file):

```python
@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None


class ConversationStore(Protocol):
    """Windowed chat history per WhatsApp sender (sibling of MessageStore's dedupe role)."""

    async def get_or_create(self, user_id: str) -> int: ...

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]: ...

    async def append_message(self, conversation_id: int, role: str, content: str) -> None: ...
```

Append to `backend/app/store/memory.py` (add `StoredMessage` to the existing `from app.store.base import (...)` block at the top, then add the class at the end of the file):

```python
class InMemoryConversationStore:
    def __init__(self) -> None:
        self._conversations: dict[str, int] = {}
        self._messages: dict[int, list[StoredMessage]] = {}
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
```

Append to `backend/app/store/postgres.py` (add `StoredMessage` to the existing `from app.store.base import (...)` block at the top, then add the class at the end of the file):

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
    conversation on first contact. Only user/assistant turns are replayed into the prompt
    (a stored role outside that pair, should one ever appear, is silently skipped rather
    than raised)."""
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

Then: `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/store/base.py app/store/memory.py app/store/postgres.py app/core/memory.py tests/store/test_conversation_store.py tests/core/test_memory.py
git commit -m "feat: ConversationStore + windowed conversation memory"
```

---

### Task 4: `core/engine.py`

**Files:**
- Create: `backend/app/core/engine.py`
- Create: `backend/tests/core/test_engine.py`

**Interfaces:**
- Consumes: `app.providers.base.{LLMProvider, Message, ProviderError}`, `app.shopify.models.AuthorizedOrder`, `app.channels.copy.copy_for`.
- Produces: `app.core.engine.EngineReply` (frozen dataclass: `intent: Intent`, `order_name: str | None`, `reply: str`); `build_system_prompt(knowledge: dict[str, str], orders: list[AuthorizedOrder], reveal_fields: list[str], store_name: str = "Thetavas") -> str`; `async run_turn(provider: LLMProvider, model: str, api_key: str, knowledge: dict[str, str], orders: list[AuthorizedOrder], reveal_fields: list[str], history: list[Message], user_text: str, *, timeout: float = 20.0, extra_params: dict[str, object] | None = None) -> EngineReply`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/core/test_engine.py`:

```python
from app.core.engine import build_system_prompt, run_turn
from app.providers.base import CompletionResult, ProviderError, ProviderErrorKind
from app.shopify.models import AuthorizedOrder, Order


def _order(name: str, phone: str) -> Order:
    return Order(
        gid=f"gid://{name}", name=name, email="customer@example.com", phone=phone,
        shipping_phone=None, billing_phone=None, financial_status="paid",
        fulfillment_status=None, cancelled_at=None, tags=(), payment_gateway_names=(),
        total=None, customer_locale=None,
    )


class _FixedProvider:
    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def complete(
        self, model: str, messages: list, api_key: str, timeout: float, *, extra_params=None
    ) -> CompletionResult:
        if self._raises is not None:
            raise self._raises
        return CompletionResult(text=self._text or "", model=model)


_KNOWLEDGE = {"brand_voice": "warm", "faq": "[]", "business": "{}", "patterns": "[]"}


async def test_run_turn_returns_parsed_reply() -> None:
    provider = _FixedProvider(
        text='{"analysis": "x", "intent": "order_status", "order_name": "tavas1", '
        '"reply": "Your order tavas1 is confirmed."}'
    )
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999"), verified_phone="+919999999999"
    )
    result = await run_turn(
        provider, "gemini/gemini-flash-latest", "key", _KNOWLEDGE, [order],
        ["order_number", "status"], [], "where is my order",
    )
    assert result.intent == "order_status"
    assert result.order_name == "tavas1"
    assert result.reply == "Your order tavas1 is confirmed."


async def test_run_turn_strips_think_and_fence_wrapping() -> None:
    provider = _FixedProvider(
        text="<think>reasoning here</think>```json\n"
        '{"analysis": "x", "intent": "chat", "order_name": null, "reply": "Hi there"}\n```'
    )
    result = await run_turn(provider, "m", "k", _KNOWLEDGE, [], [], [], "hi")
    assert result.intent == "chat"
    assert result.reply == "Hi there"


async def test_run_turn_rejects_order_name_not_belonging_to_customer() -> None:
    provider = _FixedProvider(
        text='{"analysis": "x", "intent": "order_status", '
        '"order_name": "someone-elses-order", "reply": "checking"}'
    )
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999"), verified_phone="+919999999999"
    )
    result = await run_turn(provider, "m", "k", _KNOWLEDGE, [order], [], [], "where is my order")
    assert result.order_name is None


async def test_run_turn_unknown_intent_falls_back_to_chat() -> None:
    provider = _FixedProvider(
        text='{"analysis": "x", "intent": "delete_everything", "order_name": null, "reply": "ok"}'
    )
    result = await run_turn(provider, "m", "k", _KNOWLEDGE, [], [], [], "hi")
    assert result.intent == "chat"


async def test_run_turn_on_provider_error_returns_safe_fallback() -> None:
    provider = _FixedProvider(raises=ProviderError("boom", ProviderErrorKind.TIMEOUT))
    result = await run_turn(provider, "m", "k", _KNOWLEDGE, [], [], [], "hi")
    assert result.intent == "chat"
    assert result.order_name is None
    assert "team" in result.reply


async def test_run_turn_on_unparseable_completion_returns_safe_fallback() -> None:
    provider = _FixedProvider(text="not json at all")
    result = await run_turn(provider, "m", "k", _KNOWLEDGE, [], [], [], "hi")
    assert result.intent == "chat"
    assert result.reply != "not json at all"  # never leak the raw completion


async def test_run_turn_missing_reply_field_returns_safe_fallback() -> None:
    provider = _FixedProvider(text='{"analysis": "x", "intent": "chat", "order_name": null}')
    result = await run_turn(provider, "m", "k", _KNOWLEDGE, [], [], [], "hi")
    assert result.intent == "chat"
    assert "team" in result.reply


def test_build_system_prompt_includes_order_context_when_orders_present() -> None:
    order = AuthorizedOrder(
        order=_order("tavas1", "+919999999999"), verified_phone="+919999999999"
    )
    prompt = build_system_prompt(_KNOWLEDGE, [order], ["order_number", "status"])
    assert "tavas1" in prompt


def test_build_system_prompt_asks_for_order_number_when_no_orders() -> None:
    prompt = build_system_prompt(_KNOWLEDGE, [], [])
    assert "order number" in prompt.lower()
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/core/test_engine.py -v`
Expected: `ModuleNotFoundError: No module named 'app.core.engine'`.

- [ ] **Step 3: Implement**

`backend/app/core/engine.py`:

```python
import json
import re
from dataclasses import dataclass
from typing import Literal

from app.channels.copy import copy_for
from app.providers.base import LLMProvider, Message, ProviderError
from app.shopify.models import AuthorizedOrder

Intent = Literal["order_status", "cancel_request", "chat", "handoff"]

_ALLOWED_INTENTS: tuple[Intent, ...] = ("order_status", "cancel_request", "chat", "handoff")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_BRACES_RE = re.compile(r"\{.*\}", re.DOTALL)

_SAFE_FALLBACK = copy_for("error_fallback", "en")


@dataclass(frozen=True)
class EngineReply:
    intent: Intent
    order_name: str | None
    reply: str


_SYSTEM_TEMPLATE = """You are the WhatsApp order assistant for {store_name}, an online store.
Reply in the same language and style the customer used -- English, Hindi, Hinglish, or
Gujarati are all fine. Be warm, transactional, and brief. Never use emojis.

Brand voice:
{brand_voice}

Frequently asked questions (only answer from this list; do not invent policy):
{faq}

Store information:
{business}

Example replies for common phrasings (for tone reference only):
{patterns}

{order_context}

Respond with STRICT JSON only, no other text, in exactly this shape:
{{"analysis": "<your reasoning, not shown to the customer>",
  "intent": "order_status" | "cancel_request" | "chat" | "handoff",
  "order_name": "<the order name/number this reply concerns, or null>",
  "reply": "<your warm reply to send the customer, in their language>"}}

Rules:
- If the customer asks about an order and you were given order context above, answer from
  that context only -- never guess an order's status.
- If no order context was given and the customer has not stated an order number, ask for it;
  set intent to "chat" and order_name to null.
- If the customer asks to cancel, set intent to "cancel_request" -- do NOT claim you cancelled
  it; a human-reviewed button will confirm before anything is cancelled.
- If you cannot help (a request outside order status/cancellation/store FAQs), set intent to
  "handoff" and tell the customer you are connecting them to the team.
"""


def _reveal_line(order: AuthorizedOrder, reveal_fields: list[str]) -> str:
    parts = [f"order name: {order.order.name}"]
    if "email" in reveal_fields:
        parts.append(f"email on file: {order.order.email or 'not on file'}")
    if "status" in reveal_fields:
        status = (
            "cancelled"
            if order.order.is_cancelled()
            else (order.order.financial_status or "unknown")
        )
        parts.append(f"status: {status}")
    return "; ".join(parts)


def build_system_prompt(
    knowledge: dict[str, str],
    orders: list[AuthorizedOrder],
    reveal_fields: list[str],
    store_name: str = "Thetavas",
) -> str:
    if orders:
        lines = "\n".join(f"- {_reveal_line(o, reveal_fields)}" for o in orders)
        order_context = f"Order context for this customer (verified, live from Shopify):\n{lines}"
    else:
        order_context = (
            "No order is linked to this WhatsApp number yet. If the customer asks about an "
            "order, politely ask for their order number."
        )
    return _SYSTEM_TEMPLATE.format(
        store_name=store_name,
        brand_voice=knowledge.get("brand_voice", ""),
        faq=knowledge.get("faq", ""),
        business=knowledge.get("business", ""),
        patterns=knowledge.get("patterns", ""),
        order_context=order_context,
    )


def _extract_json_blob(text: str) -> str | None:
    text = _THINK_RE.sub("", text)
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1)
    text = text.strip()
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    brace_match = _BRACES_RE.search(text)
    if brace_match:
        return brace_match.group(0)
    return None


def _parse_reply(raw_text: str, valid_order_names: set[str]) -> EngineReply | None:
    """Hardened parse of the raw completion into an EngineReply, or None on any failure.

    Never trusts the model: an order_name that is not one the customer is actually
    authorized to see is discarded (None), and an unrecognized intent falls back to "chat"
    rather than being rejected outright.
    """
    blob = _extract_json_blob(raw_text)
    if blob is None:
        return None
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    intent_raw = data.get("intent")
    intent: Intent = intent_raw if intent_raw in _ALLOWED_INTENTS else "chat"
    order_name_raw = data.get("order_name")
    order_name = order_name_raw if order_name_raw in valid_order_names else None
    return EngineReply(intent=intent, order_name=order_name, reply=reply.strip())


async def run_turn(
    provider: LLMProvider,
    model: str,
    api_key: str,
    knowledge: dict[str, str],
    orders: list[AuthorizedOrder],
    reveal_fields: list[str],
    history: list[Message],
    user_text: str,
    *,
    timeout: float = 20.0,
    extra_params: dict[str, object] | None = None,
) -> EngineReply:
    """Run one conversation turn: assemble the prompt, call the provider, parse hardened.

    Any provider failure or unparseable completion degrades to the fixed error_fallback copy
    (never the raw completion text, never a raw exception) -- the customer always gets a
    coherent message, and no upstream error detail leaks.
    """
    system_prompt = build_system_prompt(knowledge, orders, reveal_fields)
    messages = [
        Message(role="system", content=system_prompt),
        *history,
        Message(role="user", content=user_text),
    ]
    valid_names = {o.order.name for o in orders}
    try:
        result = await provider.complete(
            model, messages, api_key, timeout, extra_params=extra_params
        )
    except ProviderError:
        return EngineReply(intent="chat", order_name=None, reply=_SAFE_FALLBACK)
    parsed = _parse_reply(result.text, valid_names)
    if parsed is None:
        return EngineReply(intent="chat", order_name=None, reply=_SAFE_FALLBACK)
    return parsed
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/core/test_engine.py -v`
Expected: all PASS.

Then: `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/core/engine.py tests/core/test_engine.py
git commit -m "feat: conversation engine -- prompt assembly, provider call, hardened JSON parse"
```

---

### Task 5: `deps.py` wiring — `Container.conversations` + `active_llm`

**Files:**
- Modify: `backend/app/deps.py`
- Modify: `backend/tests/test_deps.py`

**Interfaces:**
- Consumes: `app.providers.registry.get_provider`, `app.store.base.ConversationStore`, `app.store.memory.InMemoryConversationStore`, `app.store.postgres.PostgresConversationStore`.
- Produces: `Container.conversations: ConversationStore` (new field); `async active_llm(settings: Settings, config: ConfigService) -> tuple[LLMProvider, str, str, dict[str, object] | None] | None` (provider, model, api_key, extra_params — `None` if no provider is active or its credentials are unset).

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
    await config.set_plain("llm:active_provider", "gemini")  # active, but no key ever saved

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
Expected: `ImportError: cannot import name 'active_llm'` and `AttributeError: 'Container' object has no attribute 'conversations'`.

- [ ] **Step 3: Implement**

Modify `backend/app/deps.py` — update imports and the `Container` dataclass, `get_container`, and add `active_llm`:

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
Expected: all PASS. Also re-run `python -m pytest -q` (full suite) — the `Container` constructor signature changed, so this catches any other call site that builds a `Container` directly (there should be none outside `deps.py` itself; `get_container()`/`reset_container()` are the only public entry points used elsewhere).

Then: `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/deps.py tests/test_deps.py
git commit -m "feat: wire ConversationStore into Container + active_llm provider resolution"
```

---

### Task 6: Wire `InboundText` into the conversation pipeline

**Files:**
- Modify: `backend/app/channels/whatsapp.py`
- Modify: `backend/tests/test_whatsapp_webhook.py`

**Interfaces:**
- Consumes: everything built in Tasks 1–5, plus `app.admin.controls.{AdminControls, load_controls, save_controls}`, `app.channels.whatsapp_sender.send_text`, `app.knowledge.loader.{KnowledgeLoader, SEEDS_DIR}`.
- Produces: `POST /webhook/whatsapp` now sends a real reply for every fresh `InboundText` event, gated by `AdminControls.send_mode` (`off` = pipeline does not run at all; `shadow` = runs and persists but does not call `send_text`; `allowlist` = sends only to numbers in `allowlist_phones`; `live` = always sends). The existing per-event response shape (`{"ok": True, "processed": N, "duplicate": N, "results": [...]}`) is unchanged. `InboundButton`/`InboundInteractive` events are untouched — still just recorded and echoed, per the Phase 3/5 scope boundary.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_whatsapp_webhook.py` (uses the file's existing `envelope`, `sign`, `post`, and `_fresh` fixture — do not redefine them):

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
    # send_mode defaults to "off" -- no controls have been saved in this test.

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
                "id": "wamid.text3",
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


async def test_post_text_event_allowlist_mode_blocks_unlisted_number(
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

    await save_controls(
        get_container().config,
        AdminControls(send_mode="allowlist", allowlist_phones=["+911111111111"]),
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",  # not on the allowlist
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
Expected: the new tests FAIL (`send_text` is never called because nothing wires `InboundText` to the pipeline yet, so `test_post_text_event_without_llm_configured_sends_safe_fallback` finds `sent` empty and KeyErrors).

- [ ] **Step 3: Implement**

Modify `backend/app/channels/whatsapp.py`. Update the import block at the top:

```python
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.admin.controls import load_controls
from app.channels.copy import copy_for
from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import InboundText, extract_events
from app.channels.whatsapp_sender import send_text
from app.channels.whatsapp_signature import verify_meta_hmac
from app.config.crypto import VaultError
from app.core.engine import run_turn
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

Add this new function above `receive_webhook` (after the `_ascii_compare` helper, before `verify_webhook`):

```python
async def _handle_text_event(c: Container, event: InboundText) -> None:
    """Run one conversation turn for a fresh inbound text message and send the reply.

    Failures anywhere in this pipeline (Shopify, the LLM provider, sending the reply) are
    swallowed here -- the webhook must still ack 200 for a message it already deduped, and a
    failed reply is a degraded conversation, not a failed webhook delivery.
    """
    try:
        controls = await load_controls(c.config)
        if controls.send_mode == "off":
            return
        wa_cfg = await load_whatsapp_config(c.config)
        if wa_cfg is None:
            return

        orders = await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
        conversation_id, history = await load_history(c.conversations, event.wa_id)

        llm = await active_llm(c.settings, c.config)
        if llm is None:
            reply_text = copy_for("error_fallback", "en")
        else:
            provider, model, api_key, extra_params = llm
            loader = KnowledgeLoader(c.config_repo, SEEDS_DIR)
            knowledge = await loader.assemble_all()
            engine_reply = await run_turn(
                provider,
                model,
                api_key,
                knowledge,
                orders,
                controls.reveal_fields,
                history,
                event.text,
                extra_params=extra_params,
            )
            reply_text = strip_markdown(engine_reply.reply)

        await persist_turn(c.conversations, conversation_id, event.text, reply_text)

        if controls.send_mode == "shadow":
            return
        if controls.send_mode == "allowlist":
            phone = normalize_phone(event.wa_id)
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
    # Phase 5. InboundText already runs the full conversation pipeline above.
    return JSONResponse(
        {"ok": True, "processed": processed, "duplicate": duplicate, "results": results}
    )
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_whatsapp_webhook.py -v`
Expected: all PASS.

Then run the full suite: `python -m pytest -q` — all green, Postgres-gated tests SKIP without `TEST_DATABASE_URL`.

Then: `ruff check .` and `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/channels/whatsapp.py tests/test_whatsapp_webhook.py
git commit -m "feat: wire InboundText to the conversation engine, gated on send_mode"
```

---

### Task 7: Registries + pipeline status + final sweep

**Files:**
- Modify: `docs/memory/component_registry.md`
- Modify: `docs/memory/api_registry.md`
- Modify: `docs/FR/_pipeline_status.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update `docs/memory/component_registry.md`**

Add an entry for each new/extended module built in Tasks 1–6: `app/core/order_resolver.py` (`OrderSource`, `resolve_by_phone`, `resolve_by_order_name`), `app/core/sanitize.py` (`strip_markdown`), `app/core/memory.py` (`load_history`, `persist_turn`), `app/core/engine.py` (`EngineReply`, `build_system_prompt`, `run_turn`), `ConversationStore` (+ `InMemoryConversationStore`/`PostgresConversationStore`), `IngestStore.find_mappings_by_phone`, `deps.active_llm`, `Container.conversations`. Follow the existing entry format already in that file (look at the most recent entries added for Phase 3.5 as the template).

- [ ] **Step 2: Update `docs/memory/api_registry.md`**

Note that `POST /webhook/whatsapp` now sends a live reply for `InboundText` events (previously pipe-only, echo-only) and is gated by `send_mode`. No new HTTP routes were added in this phase.

- [ ] **Step 3: Update `docs/FR/_pipeline_status.md`**

Add a row marking Phase 4 as **BUILT — READY FOR REVIEW** (not "complete/closed" — code-reviewer and security-reviewer have not run yet). Include: final test count from Step 4 below, commit range, and the two deliberate decisions from this plan's Global Constraints (windowed-memory-only scope, `send_mode` reused for the reply path) so a future session does not mistake either for an oversight.

- [ ] **Step 4: Final full-project sweep**

Run (from `backend/`):
```
python -m pytest -q
ruff check .
mypy app
```
All green; Postgres-gated tests SKIP without `TEST_DATABASE_URL`. Then run the secrets grep across every file touched in this plan (Tasks 1–6) — must return EMPTY.

- [ ] **Step 5: Commit**

```bash
git add docs/memory/component_registry.md docs/memory/api_registry.md docs/FR/_pipeline_status.md
git commit -m "docs: Phase 4 registry updates + pipeline status"
```

---

## Self-Review (done at plan time)

- **Spec coverage:** every numbered step of `docs/inbound-conversation-design.md`'s flow that is NOT explicitly deferred to Phase 5 (deterministic button dispatch, mutations, outbox drain) has a task: identity+order resolution (Task 1), knowledge-grounded prompt + hardened JSON parse (Task 4), conversation memory (Task 3), sending the reply (Task 6). `core/sanitize.py` (Task 2) covers the design doc's explicit "strip markdown" module-map entry.
- **Deliberately absent, and why:** `core/order_resolver.py`'s step-4d "ask for order number" free-text follow-up is NOT built as a separate deterministic step — the LLM itself asks for it (the system prompt instructs this when `orders` is empty), because building a second deterministic sub-flow for "customer typed an order number in a later message" would need its own state machine (was an order number expected? which turn?) that the design doc does not specify and that Phase 5's `pending_actions` mechanism is a better fit for once it exists. This is a narrower v1 than the full design's step 4d, flagged here rather than silently built halfway.
- **Placeholders:** none — every step has complete, runnable code.
- **Type consistency:** `AuthorizedOrder` (Task 1, 4, 6) is the exact Phase-1 type from `app.shopify.models`, never redefined. `ConversationStore`/`StoredMessage` (Task 3) used identically in `core/memory.py` and both store impls. `EngineReply`/`Intent` (Task 4) used identically in Task 6's `_handle_text_event`. `active_llm`'s return tuple shape (Task 5) matches exactly how Task 6 unpacks it (`provider, model, api_key, extra_params`).
- **Open item for the owner, not blocking this plan:** the `send_mode` reuse for gating replies (not just the outbound push) is a scope extension beyond what Phase 3.5's admin panel UI copy currently implies ("off — nothing is sent" already reads correctly for this new meaning, no UI change needed) — flagged in Global Constraints, surface it in the completion report rather than treating it as self-evidently approved.
