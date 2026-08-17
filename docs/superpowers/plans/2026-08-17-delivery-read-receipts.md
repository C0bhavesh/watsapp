# Delivery / Read Receipts (sub-project 1c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WhatsApp-style delivery/read tick marks on the admin chat page, for both template sends and AI replies, by parsing Meta's message-status webhook events (currently dropped entirely) and tracking them per message.

**Architecture:** A small ordering-guard pure function shared by two new store methods (one per message-origin table), a new webhook-payload parser mirroring the existing message parser, wiring into the existing webhook handler and the AI-reply send path, and additive fields on the admin API + frontend.

**Tech Stack:** Python 3.12 / FastAPI, asyncpg (Postgres) + in-memory dev store (dual-implementation), vanilla JS (frontend), pytest.

## Global Constraints

- Admin-only surface unaffected — no auth changes.
- `backend/app/core/order_actions.py` is never touched by any task in this plan.
- No new secrets, no new external API calls — all data arrives via the existing, already-HMAC-verified `/webhook/whatsapp` endpoint.
- Every `IngestStore`/`ConversationStore` method is implemented identically in `app/store/postgres.py` and `app/store/memory.py`, per this project's dual-store convention.
- Full type hints, `mypy`/`ruff` clean, no bare `except`, `async def` for I/O.
- Design source of truth: `docs/superpowers/specs/2026-08-17-delivery-read-receipts-design.md`.
- **Schema change required, NOT part of any task below** — the project owner must run this against production Postgres manually (same pattern as this session's earlier data-repair SQL) before Task 2/5's `messages.wamid`/`messages.delivery_status` columns exist in production. Tests use the in-memory store and are unaffected by whether this has been run yet:
  ```sql
  ALTER TABLE messages ADD COLUMN IF NOT EXISTS wamid text;
  ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery_status text;
  ```
  `outbound_messages.delivery_status` already exists (schema.sql:40) — no change needed there.

---

### Task 1: Ordering-guard core logic

**Files:**
- Create: `backend/app/core/delivery_status.py`
- Test: `backend/tests/core/test_delivery_status.py`

**Interfaces:**
- Produces: `should_apply_delivery_status(current: str | None, new: str) -> bool` — pure function, no I/O. Consumed by Tasks 2 and 3's store methods.

- [ ] **Step 1: Write the failing test**

```python
from app.core.delivery_status import should_apply_delivery_status


def test_none_current_accepts_any_recognized_status():
    assert should_apply_delivery_status(None, "sent") is True
    assert should_apply_delivery_status(None, "delivered") is True
    assert should_apply_delivery_status(None, "read") is True
    assert should_apply_delivery_status(None, "failed") is True


def test_forward_progression_applies():
    assert should_apply_delivery_status("sent", "delivered") is True
    assert should_apply_delivery_status("delivered", "read") is True
    assert should_apply_delivery_status("sent", "read") is True


def test_out_of_order_regression_rejected():
    assert should_apply_delivery_status("read", "delivered") is False
    assert should_apply_delivery_status("read", "sent") is False
    assert should_apply_delivery_status("delivered", "sent") is False


def test_equal_rank_is_a_noop():
    assert should_apply_delivery_status("delivered", "delivered") is False
    assert should_apply_delivery_status("read", "read") is False


def test_failed_always_applies_going_forward():
    assert should_apply_delivery_status("sent", "failed") is True
    assert should_apply_delivery_status("delivered", "failed") is True
    assert should_apply_delivery_status(None, "failed") is True


def test_failed_is_terminal_nothing_overwrites_it():
    assert should_apply_delivery_status("failed", "sent") is False
    assert should_apply_delivery_status("failed", "delivered") is False
    assert should_apply_delivery_status("failed", "read") is False
    assert should_apply_delivery_status("failed", "failed") is False


def test_unrecognized_new_status_is_rejected():
    assert should_apply_delivery_status("sent", "some_future_status") is False
    assert should_apply_delivery_status(None, "some_future_status") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/core/test_delivery_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.delivery_status'`

- [ ] **Step 3: Implement**

```python
# app/core/delivery_status.py
_RANK: dict[str, int] = {"sent": 0, "delivered": 1, "read": 2}


def should_apply_delivery_status(current: str | None, new: str) -> bool:
    """True if `new` should overwrite `current` per the delivery/read ordering guard.

    sent < delivered < read strictly increases; a lower-or-equal rank never overwrites a
    higher one (protects against out-of-order webhook delivery -- a late "delivered" arriving
    after "read" is already recorded must not regress the stored state). `failed` is WhatsApp's
    own definitive "this did not go through" signal: it always applies going forward, but once
    recorded is terminal -- nothing overwrites a `failed` row after the fact. An unrecognized
    `new` value (a future Meta status this app doesn't know about yet) is rejected, never applied.
    """
    if current == "failed":
        return False
    if new == "failed":
        return True
    if new not in _RANK:
        return False
    if current is None:
        return True
    return _RANK.get(current, -1) < _RANK[new]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/core/test_delivery_status.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/delivery_status.py backend/tests/core/test_delivery_status.py
git commit -m "feat(core): ordering guard for WhatsApp delivery/read status transitions"
```

---

### Task 2: `messages` store — wamid capture + delivery-status tracking

**Files:**
- Modify: `backend/app/store/base.py` (`StoredMessage`, `ConversationStore` protocol)
- Modify: `backend/app/store/postgres.py` (`PostgresConversationStore`)
- Modify: `backend/app/store/memory.py` (`InMemoryConversationStore`)
- Test: `backend/tests/store/test_chat_reads.py`, `backend/tests/store/test_chat_reads_pg.py`

**Interfaces:**
- Consumes: `should_apply_delivery_status` (Task 1).
- Produces: `ConversationStore.append_message(conversation_id, role, content) -> int` (widened return type, was `None`); `ConversationStore.set_message_wamid(message_id: int, wamid: str) -> None`; `ConversationStore.apply_message_delivery_status(wamid: str, status: str) -> bool` (returns whether a matching row was found and updated); `StoredMessage` gains `delivery_status: str | None = None`. Consumed by Task 5 (webhook wiring, AI-reply wamid capture) and Task 6 (admin API).

- [ ] **Step 1: Write the failing tests**

Read `backend/tests/store/test_chat_reads.py` first to match its exact fixture/store-construction conventions before writing these (the store instance variable name, any shared setup). Add:

```python
async def test_append_message_returns_the_new_message_id(store) -> None:
    conv_id = await store.get_or_create("+919876500050")
    msg_id = await store.append_message(conv_id, "assistant", "hello")
    assert isinstance(msg_id, int)


async def test_set_message_wamid_then_apply_delivery_status(store) -> None:
    conv_id = await store.get_or_create("+919876500051")
    msg_id = await store.append_message(conv_id, "assistant", "your order shipped")
    await store.set_message_wamid(msg_id, "wamid.TEST123")

    applied = await store.apply_message_delivery_status("wamid.TEST123", "delivered")

    assert applied is True
    messages = await store.find_messages_by_user_id("+919876500051")
    assert messages[-1].delivery_status == "delivered"


async def test_apply_message_delivery_status_unknown_wamid_returns_false(store) -> None:
    applied = await store.apply_message_delivery_status("wamid.NEVER_SEEN", "delivered")
    assert applied is False


async def test_apply_message_delivery_status_respects_ordering_guard(store) -> None:
    conv_id = await store.get_or_create("+919876500052")
    msg_id = await store.append_message(conv_id, "assistant", "hi")
    await store.set_message_wamid(msg_id, "wamid.TEST456")
    await store.apply_message_delivery_status("wamid.TEST456", "read")

    regressed = await store.apply_message_delivery_status("wamid.TEST456", "delivered")

    assert regressed is False
    messages = await store.find_messages_by_user_id("+919876500052")
    assert messages[-1].delivery_status == "read"


async def test_user_role_messages_unaffected_by_delivery_status_default(store) -> None:
    conv_id = await store.get_or_create("+919876500053")
    await store.append_message(conv_id, "user", "hi")
    messages = await store.find_messages_by_user_id("+919876500053")
    assert messages[-1].delivery_status is None
```

Mirror these into `test_chat_reads_pg.py` following that file's existing pattern for pairing an in-memory test with a Postgres-gated equivalent (check how the file already pairs tests before duplicating — some existing tests there may share a fixture/parametrization rather than being fully separate functions; match whatever convention is already established).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/test_chat_reads.py -k "wamid or delivery_status or returns_the_new_message_id" -v`
Expected: FAIL — `append_message` still returns `None`, `set_message_wamid`/`apply_message_delivery_status` don't exist.

- [ ] **Step 3: Implement — `base.py`**

```python
# StoredMessage gains a field (default keeps every existing construction site valid):
@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None
    delivery_status: str | None = None
```

In the `ConversationStore` Protocol, change:
```python
    async def append_message(self, conversation_id: int, role: str, content: str) -> None: ...
```
to:
```python
    async def append_message(self, conversation_id: int, role: str, content: str) -> int: ...
```
and add, near the other message-related methods:
```python
    # Attaches WhatsApp's message id to an already-persisted message row, once the actual send
    # (which happens AFTER persist_turn writes the row) succeeds and returns a wamid. No-op if the
    # message id doesn't exist (should not happen in practice, defensive only).
    async def set_message_wamid(self, message_id: int, wamid: str) -> None: ...

    # Applies a Meta delivery/read status update, found by wamid, through the ordering guard in
    # app.core.delivery_status. Returns True if a matching row was found (whether or not the
    # ordering guard actually changed anything) so the webhook handler can decide whether to also
    # try the outbound_messages table -- False means "not this table, try the other one."
    async def apply_message_delivery_status(self, wamid: str, status: str) -> bool: ...
```

- [ ] **Step 4: Implement — `postgres.py`**

Read `PostgresConversationStore.append_message`'s CURRENT implementation first (exact current SQL/line numbers), then modify it to `RETURNING id` and return that:

```python
    async def append_message(self, conversation_id: int, role: str, content: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)"
                " RETURNING id",
                conversation_id, role, content,
            )
        return int(row["id"])

    async def set_message_wamid(self, message_id: int, wamid: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET wamid = $2 WHERE id = $1", message_id, wamid
            )

    async def apply_message_delivery_status(self, wamid: str, status: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, delivery_status FROM messages WHERE wamid = $1", wamid
            )
            if row is None:
                return False
            if should_apply_delivery_status(
                None if row["delivery_status"] is None else str(row["delivery_status"]), status
            ):
                await conn.execute(
                    "UPDATE messages SET delivery_status = $2 WHERE id = $1", row["id"], status
                )
        return True
```

Add `from app.core.delivery_status import should_apply_delivery_status` to this file's imports. Also update `recent_messages`/`find_messages_by_user_id`'s SELECT statements and `StoredMessage(...)` construction to include `delivery_status=r["delivery_status"]` (read their current implementations first — both query the same table, both need the new column added to their SELECT list and their return construction).

- [ ] **Step 5: Implement — `memory.py`**

Read the CURRENT `InMemoryConversationStore` class fully first (its `_messages` storage shape, `append_message`, `recent_messages`, `find_messages_by_user_id` implementations) before changing anything — this requires restructuring the internal storage to track a per-message id, wamid, and delivery_status, since `StoredMessage` itself is a frozen return-value type, not the internal storage representation. Follow the SAME pattern this file already established for `outbound_messages` tracking (`_OutboundRow`, a mutable dataclass alongside the frozen `OutboundDraft`, with an id-based lookup helper `_meta_by_id`): introduce a mutable per-message internal row (id, role, content, created_at, wamid, delivery_status), keep a `dict[int, ...]` (or similar) indexed by message id for O(1) `set_message_wamid`/wamid-lookup, and convert to `StoredMessage` (public view, includes `delivery_status`, excludes internal id/wamid) only at the `recent_messages`/`find_messages_by_user_id` read boundary — exactly mirroring how `_OutboundRow` (internal, mutable) differs from `OutboundView`/`OutboundEntry` (public, frozen, read-only).

`append_message` must return the new row's int id. `set_message_wamid(message_id, wamid)` updates the internal row's wamid field (no-op if the id doesn't exist). `apply_message_delivery_status(wamid, status)` finds the internal row by wamid (an id-or-wamid index, your choice how to store it, but must be O(1) or the equivalent of a small linear scan is fine given this is a test-only in-memory store with no production data volume concerns), applies `should_apply_delivery_status` from Task 1, updates in place, returns whether a matching row was found (`True`) or not (`False`) — matching `postgres.py`'s exact return-value contract.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/test_chat_reads.py -k "wamid or delivery_status or returns_the_new_message_id" -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

Note: `test_chat_reads_pg.py`'s new PG-gated tests will SKIP without `TEST_DATABASE_URL` (expected in this sandbox, matching this session's established precedent) — report this explicitly rather than treating a skip as a pass.

- [ ] **Step 8: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/tests/store/test_chat_reads.py backend/tests/store/test_chat_reads_pg.py
git commit -m "feat(store): track wamid and delivery status on AI-reply messages"
```

---

### Task 3: `outbound_messages` store — delivery-status tracking

**Files:**
- Modify: `backend/app/store/base.py` (`OutboundEntry`, `IngestStore` protocol)
- Modify: `backend/app/store/postgres.py` (`find_outbound_by_phone`, new method)
- Modify: `backend/app/store/memory.py` (`find_outbound_by_phone`, new method, `_OutboundRow`)
- Test: `backend/tests/store/test_ingest_store.py` (or wherever `find_outbound_by_phone`/`mark_outbound_sent` are already tested — check the existing test file layout first)

**Interfaces:**
- Consumes: `should_apply_delivery_status` (Task 1).
- Produces: `IngestStore.apply_outbound_delivery_status(wamid: str, status: str) -> bool` (same return-value contract as Task 2's `apply_message_delivery_status` — found-or-not); `OutboundEntry` gains `delivery_status: str | None = None`. Consumed by Task 5 (webhook wiring) and Task 6 (admin API).

- [ ] **Step 1: Write the failing tests**

Read the existing test file that covers `mark_outbound_sent`/`find_outbound_by_phone` first to match its exact fixture conventions. Add:

```python
async def test_apply_outbound_delivery_status_updates_by_wamid(store) -> None:
    # (Use this test file's existing helper/fixture for creating a queued outbound row with a
    # known wamid via mark_outbound_sent -- follow the exact existing pattern, don't invent a new
    # one.)
    ...
    await store.mark_outbound_sent(row_id, "wamid.OUT123")

    applied = await store.apply_outbound_delivery_status("wamid.OUT123", "delivered")

    assert applied is True
    entries = await store.find_outbound_by_phone(phone_e164)
    assert entries[-1].delivery_status == "delivered"


async def test_apply_outbound_delivery_status_unknown_wamid_returns_false(store) -> None:
    applied = await store.apply_outbound_delivery_status("wamid.NEVER_SEEN", "delivered")
    assert applied is False


async def test_apply_outbound_delivery_status_respects_ordering_guard(store) -> None:
    ...
    await store.mark_outbound_sent(row_id, "wamid.OUT456")
    await store.apply_outbound_delivery_status("wamid.OUT456", "read")

    regressed = await store.apply_outbound_delivery_status("wamid.OUT456", "delivered")

    assert regressed is False
    entries = await store.find_outbound_by_phone(phone_e164)
    assert entries[-1].delivery_status == "read"
```

(The `...` sections need the exact existing helper this test file already uses to enqueue+claim+mark-sent an outbound row — read the file and use it verbatim, do not invent a new fixture pattern.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/test_ingest_store.py -k delivery_status -v`
(Adjust the path if the actual test file covering this area has a different name — confirmed by your Step 1 read.)
Expected: FAIL

- [ ] **Step 3: Implement — `base.py`**

```python
# OutboundEntry gains a field:
@dataclass(frozen=True)
class OutboundEntry:
    dedupe_key: str
    kind: str
    state: str
    payload_json: str
    created_at: str | None
    delivery_status: str | None = None
```

Add to `IngestStore` Protocol, near `mark_outbound_sent`:
```python
    # Applies a Meta delivery/read status update, found by template_wamid, through the ordering
    # guard in app.core.delivery_status. Returns True if a matching row was found.
    async def apply_outbound_delivery_status(self, wamid: str, status: str) -> bool: ...
```

- [ ] **Step 4: Implement — `postgres.py`**

```python
    async def apply_outbound_delivery_status(self, wamid: str, status: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, delivery_status FROM outbound_messages WHERE template_wamid = $1",
                wamid,
            )
            if row is None:
                return False
            if should_apply_delivery_status(
                None if row["delivery_status"] is None else str(row["delivery_status"]), status
            ):
                await conn.execute(
                    "UPDATE outbound_messages SET delivery_status = $2, updated_at = now()"
                    " WHERE id = $1",
                    row["id"], status,
                )
        return True
```

Add `from app.core.delivery_status import should_apply_delivery_status` if not already imported by Task 2's edit to this same file. Update `find_outbound_by_phone`'s SELECT and `OutboundEntry(...)` construction to include `delivery_status=r["delivery_status"]` (read its current implementation first, shown earlier in this plan's design-discovery — currently selects `dedupe_key, kind, state, payload_json, created_at`, needs `delivery_status` added to both the SELECT column list and the returned dataclass).

- [ ] **Step 5: Implement — `memory.py`**

Add a `delivery_status: str | None = None` field to `_OutboundRow` (the existing mutable per-row class). Implement:
```python
    async def apply_outbound_delivery_status(self, wamid: str, status: str) -> bool:
        for meta in self._outbound_meta.values():
            if meta.template_wamid == wamid:
                if should_apply_delivery_status(meta.delivery_status, status):
                    meta.delivery_status = status
                return True
        return False
```
Update `find_outbound_by_phone`'s `OutboundEntry(...)` construction to include `delivery_status=self._outbound_meta[dedupe_key].delivery_status` (read its current implementation first — shown earlier in this plan's design-discovery).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/test_ingest_store.py -k delivery_status -v` (adjust path per Step 1's finding)

- [ ] **Step 7: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 8: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/tests/store/
git commit -m "feat(store): track WhatsApp delivery status on outbound template sends"
```

---

### Task 4: Parse Meta's status webhook events

**Files:**
- Modify: `backend/app/channels/whatsapp_inbound.py`
- Test: `backend/tests/test_whatsapp_inbound.py` (check exact filename — likely already exists given `extract_events` is already tested; match its conventions)

**Interfaces:**
- Consumes: nothing new (pure payload parsing, same style as `extract_events`).
- Produces: `InboundStatus` dataclass (`wamid: str`, `status: str`, `timestamp: str`); `extract_statuses(payload: dict, expected_phone_number_id: str | None = None) -> list[InboundStatus]`. Consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

Read `backend/tests/test_whatsapp_inbound.py` (or wherever `extract_events` is tested) first to match its exact payload-construction helpers/conventions. Add:

```python
def test_extract_statuses_parses_a_delivered_event():
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123456"},
                    "statuses": [{
                        "id": "wamid.ABC123",
                        "status": "delivered",
                        "timestamp": "1755500000",
                        "recipient_id": "919876543210",
                    }],
                }
            }]
        }]
    }
    statuses = extract_statuses(payload, expected_phone_number_id="123456")
    assert len(statuses) == 1
    assert statuses[0].wamid == "wamid.ABC123"
    assert statuses[0].status == "delivered"
    assert statuses[0].timestamp == "1755500000"


def test_extract_statuses_tenant_guard_rejects_mismatched_phone_number_id():
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "WRONG"},
            "statuses": [{"id": "wamid.X", "status": "read", "timestamp": "1"}],
        }}]}]
    }
    assert extract_statuses(payload, expected_phone_number_id="123456") == []


def test_extract_statuses_skips_malformed_entries_never_raises():
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123456"},
            "statuses": [
                {"id": "wamid.OK", "status": "sent", "timestamp": "1"},
                {"status": "read"},  # missing id -- unparseable, skipped
                "not a dict",         # malformed entry -- skipped
                {"id": "wamid.NOSTATUS"},  # missing status -- unparseable, skipped
            ],
        }}]}]
    }
    statuses = extract_statuses(payload, expected_phone_number_id="123456")
    assert len(statuses) == 1
    assert statuses[0].wamid == "wamid.OK"


def test_extract_statuses_multiple_entries_and_batched_statuses():
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123456"},
            "statuses": [
                {"id": "wamid.A", "status": "sent", "timestamp": "1"},
                {"id": "wamid.B", "status": "delivered", "timestamp": "2"},
            ],
        }}]}]
    }
    statuses = extract_statuses(payload, expected_phone_number_id="123456")
    assert {s.wamid for s in statuses} == {"wamid.A", "wamid.B"}


def test_extract_statuses_no_statuses_key_returns_empty():
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "123456"}, "messages": [],
    }}]}]}
    assert extract_statuses(payload, expected_phone_number_id="123456") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_whatsapp_inbound.py -k extract_statuses -v`
Expected: FAIL — `ImportError`/`AttributeError: extract_statuses`

- [ ] **Step 3: Implement**

In `backend/app/channels/whatsapp_inbound.py`, add near the other dataclasses:

```python
@dataclass(frozen=True)
class InboundStatus:
    wamid: str
    status: str
    timestamp: str
```

Add, mirroring `extract_events`'s exact structure (tenant guard, attacker-typed defensive parsing, skip-never-raise discipline) — reuse the existing `_tenant_matches` helper:

```python
def extract_statuses(
    payload: dict[str, Any], expected_phone_number_id: str | None = None
) -> list[InboundStatus]:
    """Parse EVERY WhatsApp delivery/read status event across all entries/changes.

    Mirrors extract_events exactly (same tenant guard, same attacker-typed defensive parsing) but
    walks value.statuses instead of value.messages -- a distinct part of the same webhook envelope
    that this codebase otherwise ignores entirely.
    """
    statuses: list[InboundStatus] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return statuses
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            if expected_phone_number_id is not None and not _tenant_matches(
                value, expected_phone_number_id
            ):
                continue
            raw_statuses = value.get("statuses")
            if not isinstance(raw_statuses, list):
                continue
            for raw in raw_statuses:
                status = _parse_status(raw)
                if status is not None:
                    statuses.append(status)
    return statuses


def _parse_status(raw: Any) -> InboundStatus | None:
    if not isinstance(raw, dict):
        return None
    wamid = raw.get("id")
    status = raw.get("status")
    if not isinstance(wamid, str) or not isinstance(status, str):
        return None
    timestamp = raw.get("timestamp")
    return InboundStatus(
        wamid=wamid, status=status, timestamp=str(timestamp) if timestamp is not None else ""
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_whatsapp_inbound.py -k extract_statuses -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 6: Commit**

```bash
git add backend/app/channels/whatsapp_inbound.py backend/tests/test_whatsapp_inbound.py
git commit -m "feat(channels): parse Meta's WhatsApp message-status webhook events"
```

---

### Task 5: Wire status processing into the webhook + AI-reply wamid capture

**Files:**
- Create: `backend/app/core/apply_status.py` (or add to an existing small module — see Step 3 note)
- Modify: `backend/app/channels/whatsapp.py` (`receive_webhook`)
- Modify: `backend/app/core/memory.py` (`persist_turn`)
- Modify: `backend/app/core/conversation.py` (`_run_turn`)
- Test: `backend/tests/test_whatsapp_webhook.py`, `backend/tests/core/test_memory.py` (or wherever `persist_turn` is tested)

**Interfaces:**
- Consumes: `extract_statuses` (Task 4), `IngestStore.apply_outbound_delivery_status` (Task 3), `ConversationStore.apply_message_delivery_status`/`set_message_wamid` (Task 2), `ConversationStore.append_message` (now returns `int`, Task 2).
- Produces: nothing consumed by a later task in this plan.

- [ ] **Step 1: Write the failing tests — `persist_turn` returns an id**

Read `backend/tests/core/` (or wherever `persist_turn`/`load_history` are tested) first. Add:

```python
async def test_persist_turn_returns_the_assistant_message_id(store) -> None:
    conv_id = await store.get_or_create("+919876500060")
    msg_id = await persist_turn(store, conv_id, "hi", "hello there")
    assert isinstance(msg_id, int)
```

- [ ] **Step 2: Write the failing tests — AI-reply wamid capture**

In `backend/tests/test_whatsapp_webhook.py`, read the existing test conventions for mocking `send_text`/asserting on store state after a turn (several such tests already exist in this file, given today's earlier handoff-trigger work touched this same test file). Add a test asserting that after a successful inline reply send, the assistant's persisted message row has its wamid/delivery_status wired correctly — follow this file's exact existing mock-`send_text`-and-inspect-store pattern rather than inventing a new one; the specific assertion is: given a mocked `send_text` returning `SendResult(ok=True, wamid="wamid.REPLY1", ...)`, the conversation's messages (read via `store.find_messages_by_user_id` or equivalent) show the assistant reply, and a subsequent `store.apply_message_delivery_status("wamid.REPLY1", "delivered")` call returns `True` (proving the wamid was actually attached).

- [ ] **Step 3: Write the failing tests — webhook status wiring**

In `backend/tests/test_whatsapp_webhook.py`, add (matching the file's existing `POST /webhook/whatsapp` test conventions — valid HMAC signature, correct phone_number_id, etc.):

```python
async def test_webhook_status_event_updates_outbound_delivery_status(client, ...) -> None:
    # Seed an outbound row with a known wamid via this file's existing helper (mirroring how
    # existing tests in this file seed outbound_messages state before asserting on it).
    ...
    resp = post_signed_webhook(client, {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": TEST_PHONE_NUMBER_ID},
            "statuses": [{"id": "wamid.SEEDED", "status": "delivered", "timestamp": "1"}],
        }}]}]
    })
    assert resp.status_code == 200
    # assert the seeded outbound row's delivery_status is now "delivered" via the same read path
    # this file's other tests already use.


async def test_webhook_status_processing_exception_still_acks_200(client, monkeypatch) -> None:
    # Monkeypatch the status-applying function to raise, matching this file's existing pattern for
    # asserting dispatch_button's exception-swallowing behavior on the same endpoint.
    ...
    resp = post_signed_webhook(client, {...})
    assert resp.status_code == 200
```

(The `...` sections need this file's ACTUAL existing helper names for signing a webhook body and seeding store state — read the file fully first and use its real helpers, do not invent new ones.)

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_whatsapp_webhook.py -k status -v`
Expected: FAIL

- [ ] **Step 5: Implement — orchestrator function**

Create a small new module (`app/core/apply_status.py` — this belongs in `core/` per this project's layering rules, since `channels/` should stay a thin parsing/dispatch layer and this function coordinates two different stores):

```python
import logging

from app.channels.whatsapp_inbound import InboundStatus
from app.deps import Container

logger = logging.getLogger("app.core.apply_status")


async def apply_delivery_status(c: Container, status: InboundStatus) -> None:
    """Apply one Meta delivery/read status update, trying outbound_messages first (template
    sends) then messages (AI replies) -- a wamid belongs to exactly one of the two tables, so
    the first match wins and the second lookup is skipped. A status for a wamid this app never
    sent (or sent before this feature existed) is a silent no-op, never an error.
    """
    found = await c.ingest.apply_outbound_delivery_status(status.wamid, status.status)
    if not found:
        await c.conversations.apply_message_delivery_status(status.wamid, status.status)
```

(`Container` is defined in `app/deps.py:30` — confirmed, `from app.deps import Container` is correct.)

- [ ] **Step 6: Implement — wire into `receive_webhook`**

In `backend/app/channels/whatsapp.py`, read the CURRENT `receive_webhook` function fully (shown earlier in this plan's design-discovery, but re-read for exact current line numbers before editing — today's earlier tasks may have touched this file). After the existing `events = extract_events(...)` block and its full processing loop (right before the final `return JSONResponse(...)`), add:

```python
    statuses = extract_statuses(payload, expected_phone_number_id=cfg.phone_number_id)
    for status in statuses:
        try:
            await apply_delivery_status(c, status)
        except Exception:
            # A status-processing failure must never fail the signed webhook's 200 ack (same
            # discipline already applied to dispatch_button's exception handling above).
            logger.exception("delivery-status processing failed; webhook still acks 200")
```

Add the necessary imports (`extract_statuses` from `app.channels.whatsapp_inbound`, `apply_delivery_status` from `app.core.apply_status`).

- [ ] **Step 7: Implement — `persist_turn` returns the assistant message id**

In `backend/app/core/memory.py`, change:
```python
async def persist_turn(
    store: ConversationStore, conversation_id: int, user_text: str, assistant_reply: str
) -> None:
    await store.append_message(conversation_id, "user", user_text)
    await store.append_message(conversation_id, "assistant", assistant_reply)
```
to:
```python
async def persist_turn(
    store: ConversationStore, conversation_id: int, user_text: str, assistant_reply: str
) -> int:
    await store.append_message(conversation_id, "user", user_text)
    return await store.append_message(conversation_id, "assistant", assistant_reply)
```

- [ ] **Step 8: Implement — capture wamid after the AI reply send succeeds**

In `backend/app/core/conversation.py`, read the CURRENT `_run_turn` function fully first (re-confirm current line numbers — today's earlier work in this file may have shifted them from what's shown in this plan's design-discovery). Change:
```python
    await persist_turn(c.conversations, conversation_id, event.text, reply_text)
```
to:
```python
    assistant_message_id = await persist_turn(c.conversations, conversation_id, event.text, reply_text)
```
and change:
```python
    result = await send_text(c.http, wa_cfg, event.wa_id, reply_text)
    if not result.ok:
        logger.warning(
            "whatsapp send failed: status=%s error=%s", result.status_code, result.error
        )
```
to:
```python
    result = await send_text(c.http, wa_cfg, event.wa_id, reply_text)
    if not result.ok:
        logger.warning(
            "whatsapp send failed: status=%s error=%s", result.status_code, result.error
        )
    elif result.wamid:
        await c.conversations.set_message_wamid(assistant_message_id, result.wamid)
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_whatsapp_webhook.py -k status -v` and the Task 5 Step 1 test.

- [ ] **Step 10: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 11: Commit**

```bash
git add backend/app/core/apply_status.py backend/app/channels/whatsapp.py backend/app/core/memory.py backend/app/core/conversation.py backend/tests/
git commit -m "feat(channels,core): wire WhatsApp status events into outbound/message delivery tracking"
```

---

### Task 6: Admin API — expose `delivery_status`

**Files:**
- Modify: `backend/app/admin/router.py` (`get_conversation_thread`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `OutboundEntry.delivery_status` (Task 3), `StoredMessage.delivery_status` (Task 2).
- Produces: `template_sent` and `ai_reply` entries in `GET /admin/conversations/{thread_id}`'s response gain `"delivery_status": str | None`. Consumed by Task 7.

- [ ] **Step 1: Write the failing tests**

Read the CURRENT `get_conversation_thread()` entry-building loops first (shown earlier in this plan's design-discovery, re-confirm exact current state — several earlier tasks today modified this exact function). Add to `backend/tests/admin/test_views.py`:

```python
def test_conversation_thread_template_entry_includes_delivery_status(client: TestClient) -> None:
    login(client)
    normalized = "+919876500070"
    _seed_outbound_at(
        normalized, "gid://shopify/Order/dstatus1",
        json.dumps({"template": "order_shipped", "language": "en", "body_params": ["A", "B", "C", "D"]}),
    )
    thread_id = _thread_id_for(client, normalized)
    order_gid_row_id = ...  # use whatever this test file's existing seeding helper exposes to get
                             # the row id, or query it back -- follow existing test conventions.
    asyncio.run(get_container().ingest.apply_outbound_delivery_status("<the row's wamid>", "delivered"))
    # (Adjust: _seed_outbound_at may not currently set a wamid -- if it doesn't, either extend it
    # or call mark_outbound_sent directly with a known wamid before asserting. Match whatever this
    # test file's actual seeding helpers support; do not invent behavior _seed_outbound_at doesn't have.)

    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["delivery_status"] == "delivered"


def test_conversation_thread_ai_reply_entry_includes_delivery_status_field(client: TestClient) -> None:
    login(client)
    normalized = "+919876500071"
    _send_ai_message(normalized, "hi", "hello there")

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "ai_reply")
    assert "delivery_status" in entry
    assert entry["delivery_status"] is None  # no status ever reported for this test message
```

(The first test's exact seeding mechanics depend on `_seed_outbound_at`'s current signature — read it and adapt; the point being tested is simply "the entry's `delivery_status` field reflects the underlying row's value," however that value gets seeded.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k delivery_status -v`
Expected: FAIL — `KeyError: 'delivery_status'`

- [ ] **Step 3: Implement**

In `backend/app/admin/router.py`, in `get_conversation_thread()`, the `template_sent` entry-building loop (reading from `find_outbound_by_phone`) adds one field:
```python
        entries.append({
            "type": "template_sent",
            "timestamp": row.created_at,
            "text": _template_message_text(row.payload_json),
            "status": row.state,
            "delivery_status": row.delivery_status,
        })
```
The `ai_reply`-producing branch of the `customer_message`/`ai_reply` loop (reading from `find_messages_by_user_id`) adds the field only for `role == "assistant"` entries (a `customer_message` entry has no send-direction, `delivery_status` doesn't apply to it):
```python
        entry: dict[str, object] = {
            "type": "customer_message" if msg.role == "user" else "ai_reply",
            "timestamp": msg.created_at,
            "text": msg.content,
        }
        if msg.role == "assistant":
            entry["delivery_status"] = msg.delivery_status
        entries.append(entry)
```
(Read the CURRENT loop's exact code first — this plan's design-discovery snapshot may not match today's latest state of this function; adapt the exact variable names/structure to what's actually there.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k delivery_status -v`

- [ ] **Step 5: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): expose delivery_status on template and AI-reply chat entries"
```

---

### Task 7: Frontend — render ticks / red exclamation

**Files:**
- Modify: `backend/app/admin/static/chats.js` (`renderBubble`)
- Modify: `backend/app/admin/static/chats.html` (CSS)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `entry.delivery_status`, `entry.status` (Task 6's new/existing fields).
- Produces: nothing consumed by a later task — this is the final task in the plan.

- [ ] **Step 1: Write the failing smoke test**

```python
def test_chats_js_renders_delivery_ticks(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "delivery_status" in js
    assert "delivered" in js
    assert "read" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k renders_delivery_ticks -v`
Expected: FAIL

- [ ] **Step 3: Implement**

Read the CURRENT `renderBubble()` function fully first (shown earlier in this plan's design-discovery, but today's other tasks in this same session may have touched it further — re-confirm before editing). Add a tick/exclamation-rendering step. A tick makes sense for a `template_sent` entry with `status === "sent"`, or ANY `ai_reply` entry (which has no `status` field to gate on):

```js
function renderDeliveryMark(entry) {
  const eligible = entry.type === "ai_reply" || (entry.type === "template_sent" && entry.status === "sent");
  if (!eligible) return null;
  const mark = document.createElement("span");
  mark.className = "delivery-mark";
  if (entry.delivery_status === "failed") {
    mark.textContent = "!";
    mark.classList.add("delivery-mark-failed");
  } else if (entry.delivery_status === "read") {
    mark.textContent = "✓✓";
    mark.classList.add("delivery-mark-read");
  } else if (entry.delivery_status === "delivered") {
    mark.textContent = "✓✓";
    mark.classList.add("delivery-mark-delivered");
  } else {
    mark.textContent = "✓";
    mark.classList.add("delivery-mark-sent");
  }
  return mark;
}
```

In `renderBubble()`, after building `ts` (the timestamp element) but before `div.appendChild(ts)`, append the mark if present:
```js
  const mark = renderDeliveryMark(entry);
  if (mark) ts.appendChild(mark);
```
(Read the current structure of `ts`'s construction/append order first and adapt — the intent is the mark renders inline with/immediately next to the timestamp, matching the design's "to the right of the timestamp" placement.)

Add CSS to `chats.html`:
```css
    .delivery-mark { margin-left: .3rem; font-size: .7rem; }
    .delivery-mark-sent, .delivery-mark-delivered { color: #8696a0; }
    .delivery-mark-read { color: #53bdeb; }
    .delivery-mark-failed { color: #dc2626; font-weight: 700; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k renders_delivery_ticks -v`

- [ ] **Step 5: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/static/chats.js backend/app/admin/static/chats.html backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): render WhatsApp-style delivery/read tick marks on chat bubbles"
```

---

## Post-Implementation Notes

- No task touches `backend/app/core/order_actions.py` — verify empty diff before handoff to review.
- **This plan touches webhook-parsing code (`app/channels/whatsapp_inbound.py`, `app/channels/whatsapp.py`) — a `security-reviewer` pass is required after `code-reviewer`, per this project's rule on sensitive surfaces, before this deploys.**
- The schema change (Global Constraints section) must be run by the owner against production before Task 2/5's Postgres code paths work correctly there — flag this again explicitly when handing off for review/deploy.
- Manual browser verification is still required (same known gap as every prior frontend change on this page): confirm the ticks actually render and update as expected once real WhatsApp status events start arriving.
