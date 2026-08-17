# Unified Chat Thread View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only, WhatsApp-style chat thread per customer in the admin panel, merging the AI conversation, template sends, and Confirm/Cancel button taps — the only way to see what's been sent/received on this number, since it's Cloud API-only and can't be viewed via the WhatsApp app or Meta Business Suite.

**Architecture:** Three previously-unconnected tables (`messages`/`conversations`, `outbound_messages`, `order_actions`) get new read-only query methods; a new admin endpoint merges and sorts them into one timeline per phone; a new panel section renders it as chat bubbles. No new tables, no writes anywhere in this feature.

**Tech Stack:** Python 3.12, FastAPI, asyncpg (Postgres) + in-memory store (dual implementation, this repo's standing convention), pytest, plain HTML/JS admin panel (no framework, no build step).

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean (64 files today).
- `ruff check .` clean. No bare `except:`. No `print()` — use `logging.getLogger("app.<module>")`.
- Every new `/admin/*` route requires `Depends(require_admin)`, matching every existing admin route exactly — no new auth mechanism.
- The new AI-chat lookup must NEVER create a conversation row as a side effect of a read (unlike `ConversationStore.get_or_create`, which does) — viewing a thread must be side-effect-free.
- `backend/app/core/order_actions.py` must remain byte-identical throughout — this feature only READS `order_actions` (a new query method), never writes to it or touches the mutation-safety core. Verify via `git diff` at the end of every task.
- Both store implementations (Postgres, in-memory) must be extended together and stay behaviorally equivalent — this repo's standing dual-implementation convention.
- No schema/migration changes — all three source tables already exist and are already populated.
- Secrets/print/bare-except compliance grep (from `no-secrets.md`) must return empty on every touched file:
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>`
- Do not push to git — commit locally only, per this repo's standing rule (owner approves pushes separately).

---

## File Structure

- **Modify** `backend/app/store/base.py` — three new dataclasses (`ConversationSummary`, `OutboundEntry`, `OrderActionEntry`) and four new Protocol methods across `ConversationStore`/`IngestStore`.
- **Modify** `backend/app/store/postgres.py` — implementations of the four new methods, querying the existing tables.
- **Modify** `backend/app/store/memory.py` — implementations of the four new methods, PLUS two grounding-discovered gaps: `InMemoryConversationStore` has no `last_active_at` tracking at all today (needed for `recent_conversations`'s ordering), and `InMemoryIngestStore.record_order_action` doesn't stamp a timestamp (needed for the merge's chronological sort).
- **Modify** `backend/app/admin/router.py` — two new routes: `GET /admin/conversations` (thread list) and `GET /admin/conversations/{wa_id}` (merged timeline), plus the merge/normalize/summarize logic.
- **Modify** `backend/app/admin/static/index.html`, `backend/app/admin/static/admin.js` — a new "Chats" panel section: thread-list sidebar + bubble-rendered thread view.
- **Test files** (extend existing, no new files): `backend/tests/store/test_reminders_store.py`-adjacent new test file `backend/tests/store/test_chat_reads.py` (new file — this is genuinely new read-path coverage, not an extension of an unrelated existing suite), `backend/tests/admin/test_views.py`, `backend/tests/admin/test_static_mount.py`.

---

### Task 1: New store read methods (Postgres + in-memory)

**Files:**
- Modify: `backend/app/store/base.py` (new dataclasses + Protocol methods)
- Modify: `backend/app/store/postgres.py` (`PostgresConversationStore`, `PostgresIngestStore`)
- Modify: `backend/app/store/memory.py` (`InMemoryConversationStore`, `InMemoryIngestStore`)
- Test: `backend/tests/store/test_chat_reads.py` (new file)

**Interfaces:**
- Produces: `ConversationStore.find_messages_by_user_id(user_id: str, limit: int) -> list[StoredMessage]` — read-only, returns `[]` on no conversation, NEVER creates one.
- Produces: `ConversationStore.recent_conversations(limit: int) -> list[ConversationSummary]` where `ConversationSummary(user_id: str, last_active_at: str | None)`.
- Produces: `IngestStore.find_outbound_by_phone(phone_e164: str, limit: int) -> list[OutboundEntry]` where `OutboundEntry(dedupe_key: str, kind: str, state: str, payload_json: str, created_at: str | None)`.
- Produces: `IngestStore.find_order_actions_by_wa_id(wa_id: str, limit: int) -> list[OrderActionEntry]` where `OrderActionEntry(order_gid: str, action: str, result: str, created_at: str | None)`.
- Consumed by: Task 2 (the new admin merge endpoints call all four).

- [ ] **Step 1: Write the failing tests for all four new methods (in-memory)**

Create `backend/tests/store/test_chat_reads.py`:

```python
"""New read-only chat-aggregation methods: ConversationStore + IngestStore additions."""

from datetime import UTC, datetime

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.memory import InMemoryConversationStore, InMemoryIngestStore


# --- ConversationStore.find_messages_by_user_id ---

async def test_find_messages_by_user_id_returns_history_without_creating() -> None:
    store = InMemoryConversationStore()
    conv_id = await store.get_or_create("919664290413")
    await store.append_message(conv_id, "user", "where is my order")
    await store.append_message(conv_id, "assistant", "let me check")

    messages = await store.find_messages_by_user_id("919664290413", limit=10)

    assert [m.content for m in messages] == ["where is my order", "let me check"]


async def test_find_messages_by_user_id_unknown_user_returns_empty_no_create() -> None:
    store = InMemoryConversationStore()

    messages = await store.find_messages_by_user_id("911111111111", limit=10)

    assert messages == []
    # The read must not have created a conversation row as a side effect.
    assert "911111111111" not in store._conversations  # type: ignore[attr-defined]


# --- ConversationStore.recent_conversations ---

async def test_recent_conversations_ordered_by_last_active_desc() -> None:
    store = InMemoryConversationStore()
    await store.get_or_create("919664290413")
    await store.get_or_create("917000000000")
    # Touch the FIRST one again so it becomes the most recently active.
    await store.get_or_create("919664290413")

    summaries = await store.recent_conversations(limit=10)

    assert [s.user_id for s in summaries] == ["919664290413", "917000000000"]


# --- IngestStore.find_outbound_by_phone ---

async def _seed_outbound(store: InMemoryIngestStore, gid: str, phone: str) -> None:
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164=phone,
        customer_name="Suman", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json='{"template": "cod_confirmation"}',
    )
    await store.ingest_order_created(f"wh-{gid}", "orders/create", mapping, draft)


async def test_find_outbound_by_phone_returns_matching_rows_with_payload() -> None:
    store = InMemoryIngestStore()
    await _seed_outbound(store, "gid://shopify/Order/1", "+919664290413")
    await _seed_outbound(store, "gid://shopify/Order/2", "+911111111111")

    rows = await store.find_outbound_by_phone("+919664290413", limit=10)

    assert len(rows) == 1
    assert rows[0].dedupe_key == "order_created:gid://shopify/Order/1"
    assert rows[0].payload_json == '{"template": "cod_confirmation"}'
    assert rows[0].state == "queued"


async def test_find_outbound_by_phone_no_match_returns_empty() -> None:
    store = InMemoryIngestStore()
    await _seed_outbound(store, "gid://shopify/Order/1", "+919664290413")

    rows = await store.find_outbound_by_phone("+910000000000", limit=10)

    assert rows == []


# --- IngestStore.find_order_actions_by_wa_id ---

async def test_find_order_actions_by_wa_id_returns_matching_rows() -> None:
    store = InMemoryIngestStore()
    await store.record_order_action(
        "gid://shopify/Order/1", "confirm", "919664290413", "m1", "ok", None
    )
    await store.record_order_action(
        "gid://shopify/Order/2", "confirm", "911111111111", "m2", "ok", None
    )

    rows = await store.find_order_actions_by_wa_id("919664290413", limit=10)

    assert len(rows) == 1
    assert rows[0].order_gid == "gid://shopify/Order/1"
    assert rows[0].action == "confirm"
    assert rows[0].result == "ok"
    assert rows[0].created_at is not None


async def test_find_order_actions_by_wa_id_no_actor_never_matches() -> None:
    # actor_wa_id can be None (system-recorded actions, e.g. reconcile's "system" actor uses a
    # literal string, but other paths may pass None) -- must not raise or false-match.
    store = InMemoryIngestStore()
    await store.record_order_action(
        "gid://shopify/Order/1", "cancelled", None, None, "ok", None
    )

    rows = await store.find_order_actions_by_wa_id("919664290413", limit=10)

    assert rows == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/test_chat_reads.py -v`
Expected: FAIL — `AttributeError: 'InMemoryConversationStore' object has no attribute 'find_messages_by_user_id'` (and similarly for the other three new methods).

- [ ] **Step 3: Add the new dataclasses to `base.py`**

In `backend/app/store/base.py`, add right after `OutboundView` (currently ends around line 84):

```python
@dataclass(frozen=True)
class ConversationSummary:
    user_id: str
    last_active_at: str | None


@dataclass(frozen=True)
class OutboundEntry:
    dedupe_key: str
    kind: str
    state: str
    payload_json: str
    created_at: str | None


@dataclass(frozen=True)
class OrderActionEntry:
    order_gid: str
    action: str
    result: str
    created_at: str | None
```

Add the two new `IngestStore` Protocol methods right after `get_mapping_phone` (currently line 180):

```python
    async def find_outbound_by_phone(self, phone_e164: str, limit: int = 100) -> list[OutboundEntry]: ...

    async def find_order_actions_by_wa_id(self, wa_id: str, limit: int = 100) -> list[OrderActionEntry]: ...
```

Add the two new `ConversationStore` Protocol methods right after `get_or_create` (currently around line 221):

```python
    # Genuinely read-only: unlike get_or_create, MUST NOT create a conversation row on a miss.
    # A miss (no conversation for this user_id) returns an empty list.
    async def find_messages_by_user_id(self, user_id: str, limit: int = 100) -> list[StoredMessage]: ...

    async def recent_conversations(self, limit: int = 50) -> list[ConversationSummary]: ...
```

- [ ] **Step 4: Implement the in-memory versions**

In `backend/app/store/memory.py`, `InMemoryConversationStore` needs `last_active_at` tracking added (it doesn't exist today). Change the `__init__` (currently lines 580-585) from:

```python
    def __init__(self) -> None:
        self._conversations: dict[str, int] = {}
        self._messages: dict[int, list[StoredMessage]] = {}
        self._paused_until: dict[int, datetime] = {}
        self._handoff_attempted_at: dict[int, datetime] = {}
        self._next_id = 1
```

to:

```python
    def __init__(self) -> None:
        self._conversations: dict[str, int] = {}
        self._messages: dict[int, list[StoredMessage]] = {}
        self._paused_until: dict[int, datetime] = {}
        self._handoff_attempted_at: dict[int, datetime] = {}
        # Mirrors conversations.last_active_at (Postgres bumps this on every get_or_create, even
        # an already-exists ON CONFLICT hit -- not just on first creation). Needed so
        # recent_conversations can order threads by recency, same as the Postgres index does.
        self._last_active_at: dict[int, datetime] = {}
        self._next_id = 1
```

Change `get_or_create` (currently lines 587-596) from:

```python
    async def get_or_create(self, user_id: str) -> int:
        # Not racy: this check-then-set has no `await` between the membership test and the
        # dict writes, so under asyncio's single-threaded cooperative scheduling no other
        # coroutine can run in between — there is no yield point for a concurrent
        # get_or_create(same user_id) call to interleave on. Safe without a lock.
        if user_id not in self._conversations:
            self._conversations[user_id] = self._next_id
            self._messages[self._next_id] = []
            self._next_id += 1
        return self._conversations[user_id]
```

to:

```python
    async def get_or_create(self, user_id: str) -> int:
        # Not racy: this check-then-set has no `await` between the membership test and the
        # dict writes, so under asyncio's single-threaded cooperative scheduling no other
        # coroutine can run in between — there is no yield point for a concurrent
        # get_or_create(same user_id) call to interleave on. Safe without a lock.
        if user_id not in self._conversations:
            self._conversations[user_id] = self._next_id
            self._messages[self._next_id] = []
            self._next_id += 1
        conversation_id = self._conversations[user_id]
        self._last_active_at[conversation_id] = datetime.now(UTC)
        return conversation_id
```

Add the two new methods right after `append_message` (currently ends at line 604):

```python
    async def find_messages_by_user_id(self, user_id: str, limit: int = 100) -> list[StoredMessage]:
        conversation_id = self._conversations.get(user_id)
        if conversation_id is None:
            return []
        return self._messages.get(conversation_id, [])[-limit:]

    async def recent_conversations(self, limit: int = 50) -> list[ConversationSummary]:
        ordered = sorted(
            self._conversations.items(),
            key=lambda item: self._last_active_at.get(item[1], datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )
        return [
            ConversationSummary(
                user_id=user_id,
                last_active_at=self._last_active_at.get(conv_id).isoformat()
                if self._last_active_at.get(conv_id) else None,
            )
            for user_id, conv_id in ordered[:limit]
        ]
```

Add `ConversationSummary` to `memory.py`'s existing `from app.store.base import (...)` import block (extend it with `ConversationSummary`, `OutboundEntry`, `OrderActionEntry`).

Now `InMemoryIngestStore.record_order_action` (currently lines 519-536+) needs a `created_at` stamp added — it currently stores no timestamp at all. Change:

```python
    async def record_order_action(
        self,
        order_gid: str,
        action: str,
        actor_wa_id: str | None,
        source_wamid: str | None,
        result: str,
        user_errors_json: str | None,
    ) -> None:
        self.order_actions.append(
            {
                "order_gid": order_gid,
                "action": action,
                "actor_wa_id": actor_wa_id,
                "source_wamid": source_wamid,
                "result": result,
                "user_errors_json": user_errors_json,
            }
```

to (add one new key to the dict, leave everything else — including the closing paren/lines below — exactly as-is):

```python
    async def record_order_action(
        self,
        order_gid: str,
        action: str,
        actor_wa_id: str | None,
        source_wamid: str | None,
        result: str,
        user_errors_json: str | None,
    ) -> None:
        self.order_actions.append(
            {
                "order_gid": order_gid,
                "action": action,
                "actor_wa_id": actor_wa_id,
                "source_wamid": source_wamid,
                "result": result,
                "user_errors_json": user_errors_json,
                "created_at": datetime.now(UTC).isoformat(),
            }
```

Add `find_outbound_by_phone`/`find_order_actions_by_wa_id` to `InMemoryIngestStore` — place them near `find_mappings_by_phone` (search for that method to find a sensible insertion point):

```python
    async def find_outbound_by_phone(self, phone_e164: str, limit: int = 100) -> list[OutboundEntry]:
        matches = [
            (dedupe_key, draft) for dedupe_key, draft in self.outbound.items()
            if draft.phone_e164 == phone_e164
        ]
        return [
            OutboundEntry(
                dedupe_key=dedupe_key,
                kind=draft.kind,
                state=self._outbound_meta[dedupe_key].state,
                payload_json=draft.payload_json,
                created_at=self._outbound_meta[dedupe_key].updated_at.isoformat(),
            )
            for dedupe_key, draft in matches[-limit:]
        ]

    async def find_order_actions_by_wa_id(self, wa_id: str, limit: int = 100) -> list[OrderActionEntry]:
        matches = [row for row in self.order_actions if row.get("actor_wa_id") == wa_id]
        return [
            OrderActionEntry(
                order_gid=str(row["order_gid"]),
                action=str(row["action"]),
                result=str(row["result"]),
                created_at=row.get("created_at"),
            )
            for row in matches[-limit:]
        ]
```

(Check the exact attribute name on `self._outbound_meta[dedupe_key]` for the row's own timestamp before using `.updated_at` — read `memory.py`'s `_OutboundRow` dataclass definition first; if the field is named differently, e.g. just `updated_at` vs `created_at`, use whichever one that dataclass actually has for "when was this row's state established/last touched.")

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/test_chat_reads.py -v`
Expected: PASS — all 7 tests.

- [ ] **Step 6: Implement the Postgres versions**

In `backend/app/store/postgres.py`, add `find_messages_by_user_id`/`recent_conversations` to `PostgresConversationStore`, right after `recent_messages` (currently ends at line 1137):

```python
    async def find_messages_by_user_id(self, user_id: str, limit: int = 100) -> list[StoredMessage]:
        # Genuinely read-only -- unlike get_or_create, a miss (no conversation row for this
        # user_id yet) returns an empty list via the JOIN yielding zero rows, never creates one.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.role, m.content, m.created_at FROM messages m"
                " JOIN conversations c ON c.id = m.conversation_id"
                " WHERE c.user_id = $1 ORDER BY m.created_at DESC LIMIT $2",
                user_id, limit,
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

    async def recent_conversations(self, limit: int = 50) -> list[ConversationSummary]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, last_active_at FROM conversations"
                " ORDER BY last_active_at DESC LIMIT $1",
                limit,
            )
        return [
            ConversationSummary(
                user_id=str(r["user_id"]),
                last_active_at=r["last_active_at"].isoformat() if r["last_active_at"] else None,
            )
            for r in rows
        ]
```

Add `find_outbound_by_phone`/`find_order_actions_by_wa_id` to `PostgresIngestStore`, right after `find_mappings_by_phone` (currently ends around line 657):

```python
    async def find_outbound_by_phone(self, phone_e164: str, limit: int = 100) -> list[OutboundEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT dedupe_key, kind, state, payload_json, created_at"
                " FROM outbound_messages WHERE phone_e164 = $1"
                " ORDER BY created_at DESC LIMIT $2",
                phone_e164, limit,
            )
        return [
            OutboundEntry(
                dedupe_key=str(r["dedupe_key"]),
                kind=str(r["kind"]),
                state=str(r["state"]),
                payload_json=str(r["payload_json"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    async def find_order_actions_by_wa_id(self, wa_id: str, limit: int = 100) -> list[OrderActionEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid, action, result, created_at FROM order_actions"
                " WHERE actor_wa_id = $1 ORDER BY created_at DESC LIMIT $2",
                wa_id, limit,
            )
        return [
            OrderActionEntry(
                order_gid=str(r["order_gid"]),
                action=str(r["action"]),
                result=str(r["result"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]
```

Add `ConversationSummary`, `OutboundEntry`, `OrderActionEntry` to `postgres.py`'s existing `from app.store.base import (...)` import block.

- [ ] **Step 7: Write the gated Postgres tests**

Add to a new `backend/tests/store/test_chat_reads_pg.py`, mirroring the gated-test pattern used elsewhere in this repo (check `backend/tests/store/test_reminders_pg.py` for the exact fixture/skip-without-`TEST_DATABASE_URL` pattern and mirror it precisely — the `pool`/`LazyPool` fixture, schema setup, cleanup):

```python
"""Postgres-gated tests for the new chat-aggregation read methods (Task 1)."""

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.postgres import LazyPool, PostgresConversationStore, PostgresIngestStore

pytestmark = pytest.mark.anyio


async def test_find_messages_by_user_id_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)
    conv_id = await store.get_or_create("919664290413-pgchat")
    await store.append_message(conv_id, "user", "hi")

    messages = await store.find_messages_by_user_id("919664290413-pgchat", limit=10)

    assert [m.content for m in messages] == ["hi"]


async def test_find_messages_by_user_id_unknown_pg(pool: LazyPool) -> None:
    store = PostgresConversationStore(pool)

    messages = await store.find_messages_by_user_id("no-such-user-pgchat", limit=10)

    assert messages == []


async def test_find_outbound_by_phone_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    mapping = MappingUpsert(
        order_gid="gid://shopify/Order/pgchat1", order_name="tavaspg1", order_number_int=1,
        phone_e164="+919999888877", customer_name="A", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key="order_created:gid://shopify/Order/pgchat1", kind="order_confirmation",
        phone_e164="+919999888877", payload_json='{"template": "cod_confirmation"}',
    )
    await store.ingest_order_created("wh-pgchat1", "orders/create", mapping, draft)

    rows = await store.find_outbound_by_phone("+919999888877", limit=10)

    assert len(rows) == 1
    assert rows[0].payload_json == '{"template": "cod_confirmation"}'
```

(Read `test_reminders_pg.py`'s exact `pool` fixture setup/teardown first and mirror it — every gated test file in this repo uses the same connection/cleanup convention; do not invent a new one.)

- [ ] **Step 8: Run the Postgres-gated tests if `TEST_DATABASE_URL` is available**

Run: `cd backend && TEST_DATABASE_URL=<your-test-db-url> python -m pytest tests/store/test_chat_reads_pg.py -v`
Expected: PASS if available; correctly SKIPS (not fails) if `TEST_DATABASE_URL` is unset.

- [ ] **Step 9: Run the full suite + mypy + ruff + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/store/base.py app/store/postgres.py app/store/memory.py
```
Expected: full suite green, mypy clean, ruff clean, grep empty.

- [ ] **Step 10: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 11: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/tests/store/test_chat_reads.py backend/tests/store/test_chat_reads_pg.py
git commit -m "feat(store): read-only methods for the admin chat-thread merge (messages/outbound/order_actions by phone)"
```

---

### Task 2: Admin endpoints — merge into one timeline

**Files:**
- Modify: `backend/app/admin/router.py`
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: all four Task 1 methods (`c.conversations.find_messages_by_user_id`, `c.conversations.recent_conversations`, `c.ingest.find_outbound_by_phone`, `c.ingest.find_order_actions_by_wa_id`), `app.core.phone.normalize_phone`.
- Produces: `GET /admin/conversations` → `list[{"user_id": str, "last_active_at": str | None, "preview": str}]`. `GET /admin/conversations/{wa_id}` → `list[{"type": str, "timestamp": str | None, "text": str}]`, sorted chronologically ascending.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/admin/test_views.py`, after the existing `login`/`_ingest_one` helpers (read the file first to confirm current line numbers). This file already imports `asyncio`, `get_container`, and `TestClient` at the top (lines 1-6) — only `json` is new, add it to the existing import block:

```python
def _record_button_tap(order_gid: str, wa_id: str) -> None:
    asyncio.run(
        get_container().ingest.record_order_action(
            order_gid, "confirm", wa_id, "wamid.1", "ok", None
        )
    )


def _send_ai_message(wa_id: str, user_text: str, ai_text: str) -> None:
    async def _do() -> None:
        conv_id = await get_container().conversations.get_or_create(wa_id)
        await get_container().conversations.append_message(conv_id, "user", user_text)
        await get_container().conversations.append_message(conv_id, "assistant", ai_text)

    asyncio.run(_do())


def test_conversations_list_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/conversations").status_code == 401


def test_conversations_thread_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/conversations/919664290413").status_code == 401


def test_conversations_list_shows_recent_threads(client: TestClient) -> None:
    login(client)
    _send_ai_message("919664290413", "hi", "hello there")

    resp = client.get("/admin/conversations")

    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["user_id"] == "919664290413" for r in rows)


def test_conversation_thread_merges_all_three_sources(client: TestClient) -> None:
    login(client)
    wa_id = "919664290413"
    order_gid = "gid://shopify/Order/chat1"
    _ingest_one(order_gid)  # existing helper -- seeds an order_created outbound at +919999999999
    # Re-seed at the SAME phone this test's wa_id normalizes to, so the outbound row matches.
    # MappingUpsert/OutboundDraft are already imported at the top of this file.
    mapping = MappingUpsert(
        order_gid="gid://shopify/Order/chat2", order_name="tavaschat", order_number_int=2,
        phone_e164="+919664290413", customer_name="Suman", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key="order_created:gid://shopify/Order/chat2", kind="order_confirmation",
        phone_e164="+919664290413",
        payload_json=json.dumps({
            "template": "cod_confirmation", "language": "en",
            "body_params": {"customer_name": "Suman", "order_id": "tavaschat"},
        }),
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            "wh-chat2", "orders/create", mapping, draft
        )
    )
    _send_ai_message(wa_id, "where is my order", "let me check for you")
    _record_button_tap("gid://shopify/Order/chat2", wa_id)

    resp = client.get(f"/admin/conversations/{wa_id}")

    assert resp.status_code == 200
    entries = resp.json()
    types = [e["type"] for e in entries]
    assert "template_sent" in types
    assert "customer_message" in types
    assert "ai_reply" in types
    assert "button_tap" in types
    template_entry = next(e for e in entries if e["type"] == "template_sent")
    assert "cod_confirmation" in template_entry["text"]
    button_entry = next(e for e in entries if e["type"] == "button_tap")
    assert "confirm" in button_entry["text"]
    assert "ok" in button_entry["text"]


def test_conversation_thread_unknown_wa_id_returns_empty_list(client: TestClient) -> None:
    login(client)

    resp = client.get("/admin/conversations/900000000000")

    assert resp.status_code == 200
    assert resp.json() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -v`
Expected: FAIL with 404 (routes don't exist yet) for every new test.

- [ ] **Step 3: Implement the two new routes**

In `backend/app/admin/router.py`, extend the `app.store.base` import (currently line 29):

```python
from app.store.base import MappingView, OutboundView
```

to:

```python
from app.store.base import ConversationSummary, MappingView, OutboundEntry, OrderActionEntry, OutboundView
```

Add `from app.core.phone import normalize_phone` to the imports (near the other `app.core.*`-style imports, or alongside `app.channels.*` — place it in the existing import block in whatever position keeps imports alphabetically/logically grouped consistent with the rest of the file).

Add the merge/summarize helpers and the two routes, right after `list_outbox` (currently ends at line 581):

```python
def _template_sent_text(payload_json: str) -> str:
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return "(unreadable template payload)"
    template = data.get("template", "?")
    body_params = data.get("body_params")
    if isinstance(body_params, dict):
        values = ", ".join(str(v) for v in body_params.values())
    elif isinstance(body_params, list):
        values = ", ".join(str(v) for v in body_params)
    else:
        values = ""
    return f"{template} → {values}" if values else str(template)


def _button_tap_text(action: str, result: str) -> str:
    return f"Tapped {action} → {result}"


@admin_router.get("/conversations", dependencies=[Depends(require_admin)])
async def list_conversations(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, object]]:
    c = get_container()
    summaries: list[ConversationSummary] = await c.conversations.recent_conversations(limit)
    result: list[dict[str, object]] = []
    for s in summaries:
        recent = await c.conversations.find_messages_by_user_id(s.user_id, limit=1)
        preview = recent[-1].content[:120] if recent else ""
        result.append(
            {"user_id": s.user_id, "last_active_at": s.last_active_at, "preview": preview}
        )
    return result


@admin_router.get("/conversations/{wa_id}", dependencies=[Depends(require_admin)])
async def get_conversation_thread(wa_id: str) -> list[dict[str, object]]:
    c = get_container()
    entries: list[dict[str, object]] = []

    for msg in await c.conversations.find_messages_by_user_id(wa_id, limit=200):
        entries.append({
            "type": "customer_message" if msg.role == "user" else "ai_reply",
            "timestamp": msg.created_at,
            "text": msg.content,
        })

    normalized_phone = normalize_phone(wa_id)
    if normalized_phone is not None:
        for row in await c.ingest.find_outbound_by_phone(normalized_phone, limit=200):
            entries.append({
                "type": "template_sent",
                "timestamp": row.created_at,
                "text": f"[{row.state}] {_template_sent_text(row.payload_json)}",
            })
    else:
        logger.info("conversation thread: unparseable wa_id for outbound lookup")

    for action in await c.ingest.find_order_actions_by_wa_id(wa_id, limit=200):
        entries.append({
            "type": "button_tap",
            "timestamp": action.created_at,
            "text": _button_tap_text(action.action, action.result),
        })

    entries.sort(key=lambda e: e["timestamp"] or "")
    return entries
```

Add `import json` to the top of the file if not already imported (check the existing import block first — this file may already import `json` for another purpose; if so, don't duplicate the import).

- [ ] **Step 4: Run tests to verify they pass, plus the full admin test suite**

Run: `cd backend && python -m pytest tests/admin/ -v`
Expected: PASS — every test, including all new ones. Confirm the placeholder stub test was actually deleted (grep the file for `def test_conversation_thread_merges_all_three_sources_in_order` — it should not exist in the final file).

- [ ] **Step 5: Run the full suite + mypy + ruff + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/admin/router.py
```
Expected: full suite green, mypy clean, ruff clean, grep empty.

- [ ] **Step 6: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): merge AI chat + template sends + button taps into one thread endpoint"
```

---

### Task 3: Frontend — Chats panel

**Files:**
- Modify: `backend/app/admin/static/index.html`
- Modify: `backend/app/admin/static/admin.js`
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `GET /admin/conversations`, `GET /admin/conversations/{wa_id}` (Task 2).
- Produces: no new interface consumed by later work — this is the plan's final leaf.

- [ ] **Step 1: Write the failing tests (markup/JS smoke tests, matching this repo's existing frontend-testing convention)**

This repo has no JS unit-test framework — the existing pattern (`test_static_mount.py`) fetches the served HTML/JS as text and asserts specific element ids/strings are present. Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_panel_present(client: TestClient) -> None:
    html = client.get("/admin/ui/").text
    assert 'id="chats-card"' in html
    assert 'id="chats-list-table"' in html
    assert 'id="chat-thread"' in html


def test_chats_panel_js_calls_the_new_endpoints(client: TestClient) -> None:
    js = client.get("/admin/ui/admin.js").text
    assert "/admin/conversations" in js
    assert "loadChatList" in js
    assert "loadChatThread" in js
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: FAIL — the new element ids/functions don't exist in the served files yet.

- [ ] **Step 3: Add the Chats card to `index.html`**

In `backend/app/admin/static/index.html`, add a new card right after the existing `views-card` (currently ends at line 207, right before `<script src="admin.js"></script>`):

```html
    <div class="card" id="chats-card">
      <h2>Chats</h2>
      <button class="small" id="chats-refresh">Refresh threads</button>
      <div class="scroll"><table id="chats-list-table">
        <thead><tr><th>Phone</th><th>Last active</th><th>Preview</th></tr></thead>
        <tbody></tbody></table></div>
      <div id="chat-thread"></div>
      <div class="status" id="chats-status"></div>
    </div>
```

- [ ] **Step 4: Add the JS logic to `admin.js`**

In `backend/app/admin/static/admin.js`, add right after `loadViews`/its event listener (currently ends around line 331, right before the `// ---- boot` comment):

```javascript
function renderChatEntry(entry) {
  const div = document.createElement("div");
  const side = entry.type === "customer_message" ? "bubble-in" : "bubble-out";
  div.className = "bubble " + side + " bubble-" + entry.type;
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent = entry.type.replace("_", " ");
  const text = document.createElement("div");
  text.textContent = entry.text;
  const ts = document.createElement("div");
  ts.className = "bubble-ts";
  ts.textContent = entry.timestamp || "";
  div.appendChild(label);
  div.appendChild(text);
  div.appendChild(ts);
  return div;
}

async function loadChatThread(waId) {
  try {
    const entries = await api("GET", "/admin/conversations/" + encodeURIComponent(waId));
    const container = el("chat-thread");
    container.innerHTML = "";
    for (const entry of entries) {
      container.appendChild(renderChatEntry(entry));
    }
    setStatus("chats-status", "");
  } catch (e) { setStatus("chats-status", e.message, "err"); }
}

async function loadChatList() {
  try {
    const threads = await api("GET", "/admin/conversations");
    const tbody = el("chats-list-table").querySelector("tbody");
    tbody.innerHTML = "";
    for (const t of threads) {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => loadChatThread(t.user_id));
      for (const val of [t.user_id, t.last_active_at || "", t.preview || ""]) {
        const td = document.createElement("td");
        td.textContent = val;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    setStatus("chats-status", "");
  } catch (e) { setStatus("chats-status", e.message, "err"); }
}
el("chats-refresh").addEventListener("click", loadChatList);
```

Add `loadChatList()` to the existing `loadAll()` function's `Promise.all([...])` list (currently around line 336-338 — read the exact current contents before editing, since Task 1/2 don't touch this file so it should be unchanged from what was seen during planning):

```javascript
async function loadAll() {
  try {
    await Promise.all([
      loadShopify(), loadWhatsApp(), loadProviders(), loadKnowledge(), loadControls(), loadViews(),
```

add `loadChatList()` to that array (append it, e.g. `loadViews(), loadChatList()`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: PASS — all tests including the two new ones.

- [ ] **Step 6: Manually verify in a browser**

This repo has no automated visual/interaction testing for the admin panel — per this project's own standing instruction ("For UI or frontend changes... test the golden path... before reporting the task as complete"), start the app locally (or use the deployed instance, log in as admin) and confirm: the Chats card renders, clicking "Refresh threads" populates the thread list, clicking a thread row renders its merged timeline as bubbles in the correct chronological order with all four entry types visually distinguishable. Report what you see, including any layout/styling issue found (this repo's existing CSS classes like `.card`/`.scroll`/`.status` are reused here; `.bubble`/`.bubble-in`/`.bubble-out`/`.bubble-label`/`.bubble-ts` are NEW class names this task introduces with no CSS rules yet — add minimal CSS for them in `index.html`'s existing `<style>` block, matching the visual weight of the rest of the panel, e.g. `.bubble { padding: .4rem .6rem; margin: .3rem 0; border-radius: .5rem; max-width: 70%; } .bubble-in { background: #eee; margin-right: auto; } .bubble-out { background: #d4e8ff; margin-left: auto; } .bubble-label { font-size: .7rem; opacity: .6; } .bubble-ts { font-size: .65rem; opacity: .5; }` — adjust to match the existing panel's actual color/spacing conventions found in its current `<style>` block, don't just paste this verbatim without checking it fits).

- [ ] **Step 7: Run the full suite + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/admin/static/index.html app/admin/static/admin.js
```
Expected: full suite green, grep empty. (mypy/ruff don't apply to `.html`/`.js` files — this step intentionally omits them for this task only.)

- [ ] **Step 8: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/admin/static/index.html backend/app/admin/static/admin.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): Chats panel — thread list + bubble-rendered merged timeline"
```

---

## Post-Implementation

After all three tasks are committed:
- Update `docs/FR/_pipeline_status.md` and `docs/memory/{component_registry,api_registry,error_learnings}.md` per this repo's standing protocol (route to `doc-updater`).
- Route to `code-reviewer`, then `security-reviewer` (this exposes conversation content, template send details, and order-action history in an admin-only view — confirm the auth boundary is airtight and no phone/conversation data leaks to an unauthenticated request; sensitive surface per the routing rules).
- Do NOT push — commits stay local until the owner approves, per this repo's standing rule.
- Remind the owner: this is sub-project 1 of 3 (chat view → manual send → tags). Manual send (the next sub-project) will reuse the existing `pause_until`/`get_paused_until` AI-handoff mechanism rather than inventing a new one.
