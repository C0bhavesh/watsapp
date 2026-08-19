# Admin Chat Unread Marker + Filter Chips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the admin chat page (`chats.html`/`chats.js`) a WhatsApp-style unread count badge per thread and a single-select filter chip row (`All` / `Unread` / `Handed to human`), driven by a new `conversations.last_read_at` column and two new `ConversationStore` methods.

**Architecture:** One additive DB column (`conversations.last_read_at`, default `now()`). `GET /admin/conversations/{thread_id}` (already called on thread-open and by the existing 3s poll) stamps it via a new `mark_read`. `GET /admin/conversations` (the thread list) gains two per-thread response fields, `unread_count` (new `count_unread_messages` store method) and `ai_paused` (derived from the existing `get_paused_until`). The frontend adds a filter-chip row and a badge, both pure client-side filtering/rendering over the already-fetched thread list — no new frontend polling.

**Tech Stack:** Python 3.12+, FastAPI, asyncpg (Postgres) + in-memory store (dual implementation per this repo's `ConversationStore` Protocol), vanilla JS (`chats.js`), pytest + pytest-asyncio.

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean.
- `ruff check .` (whole project, not just touched files) must stay clean.
- No `print()` — use `logging` (not needed here, no new logging).
- No bare `except:`.
- Every `ConversationStore` method needs BOTH a Postgres (`app/store/postgres.py`) and in-memory (`app/store/memory.py`) implementation, kept behaviorally equivalent (mirrors every existing method in that Protocol).
- Schema changes are idempotent additive migrations only (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), added to `backend/app/store/schema.sql` — never a blind/destructive migration (CLAUDE.md Rule 5). The owner runs it manually in Supabase; no code path auto-applies it.
- `app/core/order_actions.py` must stay untouched (`git diff` on it must be empty) — this feature has no mutation-surface reason to touch it.
- After writing any file under `app/`, run the secrets-compliance grep (no-secrets.md) — expected empty, no secret-shaped literals are introduced by this feature.
- Compliance grep after each task: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>` on every touched file — must be empty.

---

### Task 1: Schema migration — `conversations.last_read_at`

**Files:**
- Modify: `backend/app/store/schema.sql` (near the existing `handoff_attempted_at` ALTER at line 103, same additive-column pattern)

**Interfaces:**
- Produces: a `last_read_at timestamptz NOT NULL DEFAULT now()` column on `conversations`, which Task 2/3's store methods read/write.

- [ ] **Step 1: Add the migration line**

In `backend/app/store/schema.sql`, immediately after the existing block:

```sql
-- Tracks whether ONE AI handoff attempt has already been used in the current conversation
-- window (client decision, round 3 2026-08-06: one AI attempt, then immediate handoff on a
-- second request). Distinct from paused_until, which marks a human has already taken over.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS handoff_attempted_at timestamptz;
```

add:

```sql
-- Admin "unread" tracking (2026-08-19): stamped to now() whenever the owner opens a thread in
-- the admin chat page (see ConversationStore.mark_read). DEFAULT now() is deliberate -- every
-- conversation that already exists at migration time starts "read as of now", so pre-existing
-- message history never floods the admin UI with unread badges the moment this ships. Only
-- customer messages arriving strictly after a thread's last_read_at ever count as unread
-- (see count_unread_messages).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_read_at timestamptz NOT NULL DEFAULT now();
```

- [ ] **Step 2: Verify the file is valid SQL by inspection**

Run: `grep -n "last_read_at" backend/app/store/schema.sql`
Expected: one line, the ALTER TABLE statement above.

- [ ] **Step 3: Commit**

```bash
git add backend/app/store/schema.sql
git commit -m "feat(admin): add conversations.last_read_at migration for unread tracking"
```

**⚠ This is the one manual DB step the owner must run separately (see the note at the end of this plan) — this task only adds it to the migration file, it does not run against production.**

---

### Task 2: `ConversationStore.mark_read` + `count_unread_messages` — in-memory

**Files:**
- Modify: `backend/app/store/base.py` (Protocol)
- Modify: `backend/app/store/memory.py` (`InMemoryConversationStore`)
- Test: `backend/tests/store/test_conversation_store.py`

**Interfaces:**
- Consumes: `InMemoryConversationStore.get_or_create(user_id: str) -> int`, `.append_message(conversation_id: int, role: str, content: str, sender: str | None = None) -> int` (both already exist).
- Produces: `async def mark_read(self, conversation_id: int, at: datetime) -> None` and `async def count_unread_messages(self, conversation_id: int) -> int`, used by Task 4 (router).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/store/test_conversation_store.py` (same file/style as `test_paused_until_roundtrip`):

```python
async def test_mark_read_defaults_to_creation_time_so_new_thread_has_no_unread() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    assert await store.count_unread_messages(conversation_id) == 0


async def test_unread_count_reflects_only_user_messages_after_last_read() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await store.mark_read(conversation_id, old)
    await store.append_message(conversation_id, "user", "hi")
    await store.append_message(conversation_id, "assistant", "hello")
    await store.append_message(conversation_id, "user", "still there?")
    assert await store.count_unread_messages(conversation_id) == 2


async def test_mark_read_clears_unread_count() -> None:
    store = InMemoryConversationStore()
    conversation_id = await store.get_or_create("919999999999")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await store.mark_read(conversation_id, old)
    await store.append_message(conversation_id, "user", "hi")
    assert await store.count_unread_messages(conversation_id) == 1
    await store.mark_read(conversation_id, datetime.now(UTC))
    assert await store.count_unread_messages(conversation_id) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/store/test_conversation_store.py -k "unread or mark_read" -v`
Expected: FAIL — `AttributeError: 'InMemoryConversationStore' object has no attribute 'mark_read'`

- [ ] **Step 3: Add the Protocol methods**

In `backend/app/store/base.py`, in `class ConversationStore(Protocol)`, immediately after `async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None: ...` (line 391), add:

```python

    # Admin "unread" tracking (2026-08-19). Stamps the moment the owner opened this thread in the
    # admin chat page -- called from GET /admin/conversations/{thread_id}, which fires both on
    # thread-open and on every 3s poll tick while that thread stays open, so "read on open" and
    # "stays read while viewing" both fall out of this one call site.
    async def mark_read(self, conversation_id: int, at: datetime) -> None: ...

    # Count of customer (role="user") messages strictly newer than this conversation's
    # last_read_at. Encapsulates the "since" comparison in the store so callers never read a
    # raw last_read_at themselves. A brand-new conversation (mark_read never called) has
    # last_read_at defaulted to its creation time (mirrors the Postgres column's DEFAULT now()),
    # so it starts at 0, not a flood of pre-existing history.
    async def count_unread_messages(self, conversation_id: int) -> int: ...
```

- [ ] **Step 4: Implement in `InMemoryConversationStore`**

In `backend/app/store/memory.py`:

In `__init__` (after the `self._last_active_at: dict[int, datetime] = {}` line, ~755), add:

```python
        # Mirrors conversations.last_read_at (DEFAULT now() in Postgres). Stamped at row creation
        # so a brand-new thread starts with zero unread, exactly like the Postgres column default.
        self._last_read_at: dict[int, datetime] = {}
```

In `get_or_create`, alongside the existing `self._last_active_at[self._next_id] = datetime.now(UTC)` line, add the sibling stamp:

```python
            self._last_active_at[self._next_id] = datetime.now(UTC)
            self._last_read_at[self._next_id] = datetime.now(UTC)
```

After `get_handoff_attempted_at` (the last method in the class, ~912), add:

```python

    async def mark_read(self, conversation_id: int, at: datetime) -> None:
        self._last_read_at[conversation_id] = at

    async def count_unread_messages(self, conversation_id: int) -> int:
        last_read = self._last_read_at.get(conversation_id, datetime.min.replace(tzinfo=UTC))
        return sum(
            1
            for row in self._messages.get(conversation_id, [])
            if row.role == "user" and _parse_iso(row.created_at) is not None
            and _parse_iso(row.created_at) > last_read  # type: ignore[operator]
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/store/test_conversation_store.py -k "unread or mark_read" -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run mypy + ruff on touched files**

Run: `python -m mypy app/store/base.py app/store/memory.py` (from `backend/`)
Run: `python -m ruff check app/store/base.py app/store/memory.py backend/tests/store/test_conversation_store.py`
Expected: both clean. If the `# type: ignore[operator]` on the double `_parse_iso` call is flagged as redundant or the comparison mypy-fails differently, refactor to bind the parsed value to a local first:

```python
    async def count_unread_messages(self, conversation_id: int) -> int:
        last_read = self._last_read_at.get(conversation_id, datetime.min.replace(tzinfo=UTC))
        count = 0
        for row in self._messages.get(conversation_id, []):
            if row.role != "user":
                continue
            created = _parse_iso(row.created_at)
            if created is not None and created > last_read:
                count += 1
        return count
```

Prefer this second version directly in Step 4 — it's cleaner than the `# type: ignore[operator]` version and avoids the mypy question entirely.

- [ ] **Step 7: Commit**

```bash
git add backend/app/store/base.py backend/app/store/memory.py backend/tests/store/test_conversation_store.py
git commit -m "feat(admin): add mark_read/count_unread_messages to ConversationStore (in-memory)"
```

---

### Task 3: `ConversationStore.mark_read` + `count_unread_messages` — Postgres

**Files:**
- Modify: `backend/app/store/postgres.py` (`PostgresConversationStore`)
- Test: `backend/tests/store/test_conversation_store.py` (gated Postgres tests, mirroring `test_conversation_roundtrip_postgres` at line 119)

**Interfaces:**
- Consumes: Task 1's `conversations.last_read_at` column; the Protocol methods from Task 2.
- Produces: the Postgres-backed implementations of `mark_read`/`count_unread_messages`, behaviorally equivalent to Task 2's in-memory ones.

- [ ] **Step 1: Write the failing gated tests**

Find the gated-Postgres test fixture pattern already in `backend/tests/store/test_conversation_store.py` around `test_conversation_roundtrip_postgres` (uses `pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), ...)` and a `PostgresConversationStore` against `TEST_DATABASE_URL`, with a fresh `uuid`-suffixed phone per test to avoid collisions). Read that test in full first (`sed -n '119,150p' backend/tests/store/test_conversation_store.py` or open it in the editor) to copy its exact fixture/cleanup shape, then add:

```python
@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_unread_count_roundtrip_postgres() -> None:
    store = PostgresConversationStore(os.environ["TEST_DATABASE_URL"])
    user_id = f"91{uuid.uuid4().int % 10**9:09d}"
    conversation_id = await store.get_or_create(user_id)
    assert await store.count_unread_messages(conversation_id) == 0

    old = datetime(2020, 1, 1, tzinfo=UTC)
    await store.mark_read(conversation_id, old)
    await store.append_message(conversation_id, "user", "hi")
    await store.append_message(conversation_id, "assistant", "hello")
    assert await store.count_unread_messages(conversation_id) == 1

    await store.mark_read(conversation_id, datetime.now(UTC))
    assert await store.count_unread_messages(conversation_id) == 0
```

Match the exact import name of the Postgres store class, its constructor signature, and whether `uuid`/`os` are already imported in the file — adjust the snippet to match what's actually there rather than assuming.

- [ ] **Step 2: Run test to verify it fails (or skips if no TEST_DATABASE_URL)**

Run: `python -m pytest backend/tests/store/test_conversation_store.py -k unread_count_roundtrip_postgres -v`
Expected: SKIP if `TEST_DATABASE_URL` is unset (matches every other gated Postgres test in this repo — acceptable, per `error_learnings.md` 2026-07-28/08 entries, this sandbox has no live Postgres); FAIL with `AttributeError` if it is set.

- [ ] **Step 3: Implement in `PostgresConversationStore`**

In `backend/app/store/postgres.py`, immediately after `get_handoff_attempted_at` (~line 1500), add:

```python

    async def mark_read(self, conversation_id: int, at: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET last_read_at = $1 WHERE id = $2", at, conversation_id
            )

    async def count_unread_messages(self, conversation_id: int) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.conversation_id = $1 AND m.role = 'user' AND m.created_at > c.last_read_at
                """,
                conversation_id,
            )
        return int(row["n"]) if row is not None else 0
```

- [ ] **Step 4: Run the gated test against a real Postgres if `TEST_DATABASE_URL` is available**

Run: `TEST_DATABASE_URL=<scratch-db-dsn> python -m pytest backend/tests/store/test_conversation_store.py -k unread_count_roundtrip_postgres -v`
Expected: PASS if a scratch DB is available in this environment; otherwise document as SKIP (same documented gap as every prior Postgres-gated feature in this repo — never run against production).

- [ ] **Step 5: Run mypy + ruff**

Run: `python -m mypy app/store/postgres.py` (from `backend/`)
Run: `python -m ruff check app/store/postgres.py backend/tests/store/test_conversation_store.py`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/store/postgres.py backend/tests/store/test_conversation_store.py
git commit -m "feat(admin): add mark_read/count_unread_messages to ConversationStore (postgres)"
```

---

### Task 4: Wire into the admin API — `GET /admin/conversations/{thread_id}` marks read, `GET /admin/conversations` returns `unread_count`/`ai_paused`

**Files:**
- Modify: `backend/app/admin/router.py`
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `c.conversations.mark_read(thread_id: int, at: datetime) -> None`, `c.conversations.count_unread_messages(conversation_id: int) -> int`, `c.conversations.get_paused_until(conversation_id: int) -> datetime | None` (existing).
- Produces: `GET /admin/conversations` response rows gain `unread_count: int` and `ai_paused: bool`. `GET /admin/conversations/{thread_id}` behavior is unchanged except for the new side effect (marks the thread read) — response shape unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/admin/test_views.py`, after `test_conversations_list_shows_recent_threads` (~line 133):

```python
def test_conversations_list_shows_unread_count_for_new_customer_message(
    client: TestClient,
) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")
    # Opening the thread marks it read as of now; a message that arrives AFTER that must count.
    client.get(f"/admin/conversations/{thread_id}")

    async def _new_customer_message() -> None:
        await get_container().conversations.append_message(thread_id, "user", "still there?")

    asyncio.run(_new_customer_message())

    rows = client.get("/admin/conversations").json()
    row = next(r for r in rows if r["thread_id"] == thread_id)
    assert row["unread_count"] == 1


def test_opening_thread_clears_unread_count(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    async def _new_customer_message() -> None:
        await get_container().conversations.append_message(thread_id, "user", "still there?")

    asyncio.run(_new_customer_message())
    before = client.get("/admin/conversations").json()
    row_before = next(r for r in before if r["thread_id"] == thread_id)
    assert row_before["unread_count"] >= 1

    client.get(f"/admin/conversations/{thread_id}")

    after = client.get("/admin/conversations").json()
    row_after = next(r for r in after if r["thread_id"] == thread_id)
    assert row_after["unread_count"] == 0


def test_conversations_list_reports_ai_paused_while_handed_off(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")
    future = datetime.now(UTC) + timedelta(hours=1)
    asyncio.run(get_container().conversations.pause_until(thread_id, future))

    rows = client.get("/admin/conversations").json()
    row = next(r for r in rows if r["thread_id"] == thread_id)
    assert row["ai_paused"] is True


def test_conversations_list_reports_ai_not_paused_by_default(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")

    rows = client.get("/admin/conversations").json()
    row = next(r for r in rows if r["phone"] == "+919664290413")
    assert row["ai_paused"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/admin/test_views.py -k "unread or ai_paused" -v`
Expected: FAIL — `KeyError: 'unread_count'` / `'ai_paused'`

- [ ] **Step 3: Wire `mark_read` into `get_conversation_thread`**

In `backend/app/admin/router.py`, in `get_conversation_thread` (~line 885), change:

```python
    paused_until = await c.conversations.get_paused_until(thread_id)
    return {
        "entries": entries,
        "orders": order_summaries,
        "paused_until": paused_until.isoformat() if paused_until else None,
    }
```

to:

```python
    paused_until = await c.conversations.get_paused_until(thread_id)
    # Opening a thread (this endpoint fires both on click-open and on every 3s poll tick while it
    # stays open) marks it read. Placed last so a failure here can never prevent the entries/orders
    # payload from returning -- worst case a badge stays stale one tick, never a broken thread view.
    await c.conversations.mark_read(thread_id, datetime.now(UTC))
    return {
        "entries": entries,
        "orders": order_summaries,
        "paused_until": paused_until.isoformat() if paused_until else None,
    }
```

- [ ] **Step 4: Add `unread_count`/`ai_paused` to `list_conversations`**

In the same file, in `list_conversations`'s per-thread loop (~line 750-776), change:

```python
    result: list[dict[str, object]] = []
    for norm in ordered_phones:
        # Materialize a stable conversation.id for the thread (idempotent -- repeated list views
        # fetch the existing id, they do not create duplicates). This is the one place a create-on-
        # miss is intended: the next sub-project (manual send) needs every thread to have an id.
        thread_id = await c.conversations.get_or_create(norm)
        recent = await c.conversations.find_messages_by_user_id(norm, limit=1)
        preview = recent[-1].content[:120] if recent else ""
        # Prefer the latest message timestamp; fall back to the MAX-across-sources last_active
        # captured above (used for outbound-only / tap-only threads that have no messages).
        last_active = recent[-1].created_at if recent else last_active_by_phone.get(norm)

        orders_for_phone = await c.ingest.find_mirrored_orders_by_phone(norm)
        orders_sorted_for_name = sorted(
            orders_for_phone, key=lambda o: str(o.updated_at or ""), reverse=True
        )
        customer_name: str | None = None
        if orders_sorted_for_name and orders_sorted_for_name[0].customer:
            cust = orders_sorted_for_name[0].customer
            name = " ".join(p for p in (cust.first_name, cust.last_name) if p).strip()
            customer_name = name or None
        order_names = [o.name for o in orders_for_phone]

        result.append(
            {"thread_id": thread_id, "phone": norm, "last_active_at": last_active,
             "preview": preview, "customer_name": customer_name, "order_names": order_names}
        )
```

to:

```python
    result: list[dict[str, object]] = []
    for norm in ordered_phones:
        # Materialize a stable conversation.id for the thread (idempotent -- repeated list views
        # fetch the existing id, they do not create duplicates). This is the one place a create-on-
        # miss is intended: the next sub-project (manual send) needs every thread to have an id.
        thread_id = await c.conversations.get_or_create(norm)
        recent = await c.conversations.find_messages_by_user_id(norm, limit=1)
        preview = recent[-1].content[:120] if recent else ""
        # Prefer the latest message timestamp; fall back to the MAX-across-sources last_active
        # captured above (used for outbound-only / tap-only threads that have no messages).
        last_active = recent[-1].created_at if recent else last_active_by_phone.get(norm)

        orders_for_phone = await c.ingest.find_mirrored_orders_by_phone(norm)
        orders_sorted_for_name = sorted(
            orders_for_phone, key=lambda o: str(o.updated_at or ""), reverse=True
        )
        customer_name: str | None = None
        if orders_sorted_for_name and orders_sorted_for_name[0].customer:
            cust = orders_sorted_for_name[0].customer
            name = " ".join(p for p in (cust.first_name, cust.last_name) if p).strip()
            customer_name = name or None
        order_names = [o.name for o in orders_for_phone]

        unread_count = await c.conversations.count_unread_messages(thread_id)
        paused_until = await c.conversations.get_paused_until(thread_id)
        ai_paused = paused_until is not None and paused_until > datetime.now(UTC)

        result.append(
            {"thread_id": thread_id, "phone": norm, "last_active_at": last_active,
             "preview": preview, "customer_name": customer_name, "order_names": order_names,
             "unread_count": unread_count, "ai_paused": ai_paused}
        )
```

Confirm `datetime`/`UTC` are already imported at the top of `router.py` (they are — used elsewhere in this same file, e.g. `resume_conversation`); do not add a duplicate import.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/admin/test_views.py -k "unread or ai_paused" -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full admin test file + mypy/ruff**

Run: `python -m pytest backend/tests/admin/test_views.py -v`
Expected: all pass, no regressions in the existing conversations tests.
Run: `python -m mypy app/admin/router.py` (from `backend/`)
Run: `python -m ruff check app/admin/router.py backend/tests/admin/test_views.py`
Expected: both clean.

- [ ] **Step 7: Secrets-compliance grep**

Run (from `backend/`): `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/admin/router.py`
Expected: empty.

- [ ] **Step 8: Confirm `order_actions.py` untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): expose unread_count/ai_paused on /admin/conversations, mark-read on thread open"
```

---

### Task 5: Frontend — filter chips + unread badge (`chats.html`/`chats.js`)

**Files:**
- Modify: `backend/app/admin/static/chats.html`
- Modify: `backend/app/admin/static/chats.js`

**Interfaces:**
- Consumes: `GET /admin/conversations` rows now carrying `unread_count: number` and `ai_paused: boolean` (Task 4).
- Produces: no new interfaces for other tasks (this is the leaf/UI task) — a visible filter-chip row and green count badges.

- [ ] **Step 1: Add filter-chip markup to `chats.html`**

In `backend/app/admin/static/chats.html`, change (line 120-121):

```html
      <input id="thread-search" type="text" placeholder="Search name, phone, or order #" />
      <div id="thread-list"></div>
```

to:

```html
      <input id="thread-search" type="text" placeholder="Search name, phone, or order #" />
      <div id="thread-filters"></div>
      <div id="thread-list"></div>
```

- [ ] **Step 2: Add filter-chip CSS to `chats.html`**

In the same file's `<style>` block, immediately after the existing `.thread-row .ts { ... }` rule (line 28), add:

```css
    #thread-filters { display: flex; gap: .4rem; padding: .5rem 1rem; border-bottom: 1px solid #f0f2f5; }
    .filter-chip { font-size: .76rem; padding: .28rem .7rem; border-radius: 999px; cursor: pointer;
      background: #f0f2f5; color: #3b4a54; border: none; white-space: nowrap; }
    .filter-chip.active { background: #00a884; color: #fff; }
    .unread-badge { display: inline-block; min-width: 1.1rem; height: 1.1rem; line-height: 1.1rem;
      text-align: center; border-radius: 999px; background: #00a884; color: #fff;
      font-size: .68rem; font-weight: 600; margin-left: .4rem; padding: 0 .3rem; }
```

- [ ] **Step 3: Add the filter-chip state, definitions, and rendering to `chats.js`**

In `backend/app/admin/static/chats.js`, immediately before `let allThreads = [];` (line 460), add:

```javascript
const FILTERS = [
  { id: "all", label: "All", predicate: () => true },
  { id: "unread", label: "Unread", predicate: (t) => (t.unread_count || 0) > 0 },
  { id: "handoff", label: "Handed to human", predicate: (t) => !!t.ai_paused },
];
let activeFilterId = "all";

function renderFilterChips() {
  const container = el("thread-filters");
  container.innerHTML = "";
  for (const f of FILTERS) {
    const btn = document.createElement("button");
    btn.className = "filter-chip" + (f.id === activeFilterId ? " active" : "");
    btn.textContent = f.label;
    btn.addEventListener("click", () => {
      activeFilterId = f.id;
      renderFilterChips();
      renderThreadRows(applyThreadFilters(allThreads));
    });
    container.appendChild(btn);
  }
}

function applyThreadFilters(threads) {
  const chip = FILTERS.find((f) => f.id === activeFilterId) || FILTERS[0];
  const query = el("thread-search").value;
  return threads.filter((t) => chip.predicate(t) && threadMatchesQuery(t, query));
}
```

- [ ] **Step 4: Wire `renderFilterChips()` into initial load**

Change the last line of the file (currently `loadThreadList();`) to:

```javascript
renderFilterChips();
loadThreadList();
```

- [ ] **Step 5: Replace every `threadMatchesQuery`-only filter call site with `applyThreadFilters`**

There are 3 call sites (verify with `grep -n "threadMatchesQuery" backend/app/admin/static/chats.js` — expect matches inside the function definition itself plus 3 callers at the `loadThreadList`, the `thread-search` input listener, and `pollTick`). In each of the 3 CALLER lines (not the `threadMatchesQuery` function definition itself), replace:

```javascript
renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
```

with:

```javascript
renderThreadRows(applyThreadFilters(allThreads));
```

- [ ] **Step 6: Add the unread badge to `renderThreadRows`**

In `renderThreadRows` (~line 482-505), change:

```javascript
    const phone = document.createElement("div");
    phone.className = "phone";
    phone.textContent = t.customer_name || t.phone;
    phone.appendChild(ts);
```

to:

```javascript
    const phone = document.createElement("div");
    phone.className = "phone";
    phone.textContent = t.customer_name || t.phone;
    if (t.unread_count > 0) {
      const badge = document.createElement("span");
      badge.className = "unread-badge";
      badge.textContent = String(t.unread_count);
      phone.appendChild(badge);
    }
    phone.appendChild(ts);
```

- [ ] **Step 7: Include `unread_count`/`ai_paused` in the poll diff key**

In `threadListKey` (~line 567-569), change:

```javascript
function threadListKey(threads) {
  return threads.map((t) => t.thread_id + ":" + (t.last_active_at || "")).join("|");
}
```

to:

```javascript
function threadListKey(threads) {
  return threads
    .map((t) => t.thread_id + ":" + (t.last_active_at || "") + ":" + t.unread_count + ":" + t.ai_paused)
    .join("|");
}
```

This ensures a badge appearing/disappearing (which changes neither `thread_id` nor `last_active_at`) still triggers the poll's re-render — same rationale already documented in this file for the `delivery_status` fold into `threadEntriesKey`.

- [ ] **Step 8: Manual browser verification**

This repo has no frontend test runner (documented gap, e.g. sub-project 1a/1b rows in `_pipeline_status.md`). Run the `run` skill (or start the FastAPI dev server manually) and, in a browser at `/admin/ui/chats.html`:
1. Confirm the `All` / `Unread` / `Handed to human` chip row renders, `All` active by default.
2. Send a test customer message (or seed one via the admin's existing test tooling) to a thread that is NOT currently open — confirm a green count badge appears on that thread row, and clicking the `Unread` chip narrows the list to it.
3. Open that thread — confirm the badge disappears (may take one poll tick, ≤3s) and the thread drops out of the `Unread` filter.
4. Trigger an AI handoff (or manually call `pause_until` via the admin API on a test thread) — confirm it appears under `Handed to human` while paused, and drops out once resumed.
5. Confirm the search box still narrows results within the active chip (type a name while `Unread` is active).

Record the outcome in the task's completion notes — this step cannot be automated in this sandbox (no browser), so it must be explicitly performed and reported, not skipped silently.

- [ ] **Step 9: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js
git commit -m "feat(admin): add unread badge + filter chips to chat page"
```

---

## Self-review notes (plan author)

- **Spec coverage:** migration (Task 1) ✓; `mark_read`/`count_unread_messages` in-memory (Task 2) + Postgres (Task 3) ✓; `GET /admin/conversations/{thread_id}` marks read (Task 4 step 3) ✓; `GET /admin/conversations` gains `unread_count`/`ai_paused` (Task 4 step 4) ✓; filter chips single-select + search AND-combine + badge + extensible `FILTERS` array (Task 5) ✓. Testing section of the spec covered by Tasks 2-4's store/router tests + Task 5's mandatory manual browser pass.
- **Placeholder scan:** no TBD/TODO; every step has literal code, not a description.
- **Type consistency:** `mark_read(conversation_id: int, at: datetime) -> None` and `count_unread_messages(conversation_id: int) -> int` are identical across the Protocol (Task 2), in-memory (Task 2), Postgres (Task 3), and router call sites (Task 4). `unread_count`/`ai_paused` field names match between the router response (Task 4) and the frontend consumers (Task 5).

## Next steps after all 5 tasks are GREEN

1. Route to `code-reviewer` (scoped to the 5 files touched: `schema.sql`, `store/base.py`, `store/memory.py`, `store/postgres.py`, `admin/router.py`, `static/chats.html`, `static/chats.js`).
2. This does NOT touch credentials, webhooks, or a mutation path — per `.claude/rules/common/agents.md`, `security-reviewer` is conditional on a sensitive surface; this feature is read-tracking only (no new outbound sends, no new writes to `order_actions`/Shopify), so a security-reviewer pass is optional, not mandatory, at the owner's discretion.
3. `doc-updater` updates `docs/memory/component_registry.md` + `api_registry.md` + `docs/FR/_pipeline_status.md`.
4. **Owner runs the Task 1 migration in Supabase** (see below) before this is ever pushed.
5. Owner reviews → push after approval (never auto-push, per CLAUDE.md Rule 7).

## Database change the owner must run (repeated here per your request)

One statement, safe to run any time before deploy (additive, idempotent, no lock beyond a normal `ADD COLUMN`):

```sql
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_read_at timestamptz NOT NULL DEFAULT now();
```

Run this in the Supabase SQL editor against production before this feature is pushed. It is also captured in `backend/app/store/schema.sql` for local/fresh-database setups.
