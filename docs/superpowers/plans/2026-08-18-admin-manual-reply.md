# Admin Manual Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the store owner type and send a free-text WhatsApp message directly from the admin chat page, with the same delivery tracking, auto-retry, and AI-consistency guarantees as an AI-generated reply.

**Architecture:** One new nullable `messages.sender` column (display-only), one new optional `sender` keyword param threaded through the existing `append_message` store method, one new admin-gated `POST /admin/conversations/{thread_id}/messages` endpoint that mirrors `core/conversation.py`'s existing AI-reply send path, and a message box + send button added to the chat page's frontend.

**Tech Stack:** Python 3.12 / FastAPI (backend), vanilla JS (frontend), pytest (backend tests), Python `TestClient`-based markup/JS-substring assertions (frontend smoke tests — no browser test runner in this repo).

## Global Constraints

- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- `backend/app/core/order_actions.py` is never touched.
- No schema/migration auto-applied by any code path — the new `messages.sender` column is owner-run manual DDL (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), same pattern as every prior migration in this repo.
- Manual sends bypass `send_decision`/`send_mode`/`allowlist_phones` entirely (owner's explicit decision — the kill switch governs automated/bot traffic, not a deliberate one-to-one human reply). This is a **deliberate, documented exception** to this project's otherwise-universal "every send goes through send_decision" rule — do not treat it as a bug in review.
- A manual reply is persisted with `role="assistant"` (never a new role value) so it stays in the AI's memory window (`core/memory.py::load_history`) and stays eligible for the existing delivery-failure auto-retry's `role = 'assistant'` filter (`get_message_retry_info`, added in sub-project 1e's final review) — `sender="admin"` is a separate, retry/memory-inert column purely for UI display.
- Design source of truth: `docs/superpowers/specs/2026-08-18-admin-manual-reply-design.md`.
- This feature sends real outbound WhatsApp messages — a `security-reviewer` pass is required after `code-reviewer`, same as every other send-path feature this project has shipped.

---

### Task 1: Store layer — `sender` column + threading it through `append_message`

**Files:**
- Modify: `backend/app/store/base.py` (`StoredMessage`, `ConversationStore.append_message` Protocol signature)
- Modify: `backend/app/store/postgres.py` (`append_message`, `recent_messages`, `find_messages_by_user_id`)
- Modify: `backend/app/store/memory.py` (`_MessageRow`, `_message_view`, `append_message`)
- Modify: `backend/app/store/schema.sql` (new `ALTER TABLE`)
- Test: `backend/tests/store/test_postgres_store.py` (if it exists — otherwise `backend/tests/store/test_memory_store.py` or the closest existing store-level test file; grep first) and `backend/tests/core/test_memory.py` (confirm `sender` doesn't break `load_history`)

**Interfaces:**
- Consumes: nothing new from earlier tasks (this is the foundation task).
- Produces: `ConversationStore.append_message(conversation_id: int, role: str, content: str, sender: str | None = None) -> int`. `StoredMessage.sender: str | None = None` (new field, default preserves every existing construction call site). Every existing caller of `append_message` (in `core/memory.py::persist_turn`) is unaffected — it doesn't pass `sender`, so rows it creates get `sender=None` (rendered as "AI").

- [ ] **Step 1: Write the failing test for the widened `append_message` signature and `StoredMessage.sender`**

First, grep for the existing store-level test file covering `append_message`/`StoredMessage` (run `grep -rl "append_message" backend/tests/store/` — the plan cannot assume the exact file name since it wasn't read at plan-writing time). Add this test to whichever file already covers `find_messages_by_user_id`/`recent_messages` for BOTH the Postgres and in-memory store test classes/fixtures that file already parametrizes over (mirror the existing test's parametrization exactly — do not write a Postgres-only or memory-only test if the file already runs shared tests against both backends):

```python
async def test_append_message_defaults_sender_to_none(store) -> None:
    conversation_id = await store.get_or_create("+919876500099")
    await store.append_message(conversation_id, "assistant", "hi")
    messages = await store.recent_messages(conversation_id, limit=10)
    assert messages[0].sender is None


async def test_append_message_records_admin_sender(store) -> None:
    conversation_id = await store.get_or_create("+919876500098")
    await store.append_message(conversation_id, "assistant", "manual reply", sender="admin")
    messages = await store.recent_messages(conversation_id, limit=10)
    assert messages[0].sender == "admin"


async def test_find_messages_by_user_id_includes_sender(store) -> None:
    conversation_id = await store.get_or_create("+919876500097")
    await store.append_message(conversation_id, "assistant", "manual reply", sender="admin")
    messages = await store.find_messages_by_user_id("+919876500097", limit=10)
    assert messages[0].sender == "admin"


async def test_manual_reply_row_is_visible_to_ai_memory(store) -> None:
    """A manual reply (role='assistant', sender='admin') must load into AI context exactly like
    an AI-generated row -- core/memory.py::load_history only branches on `role`, never `sender`.
    """
    from app.core.memory import load_history

    conversation_id = await store.get_or_create("+919876500096")
    await store.append_message(conversation_id, "user", "where is my order")
    await store.append_message(
        conversation_id, "assistant", "It shipped yesterday.", sender="admin"
    )
    history = await load_history(store, conversation_id)
    assert any(m.role == "assistant" and m.content == "It shipped yesterday." for m in history)


async def test_manual_reply_row_is_eligible_for_delivery_retry_lookup(store) -> None:
    """A manual reply must pass delivery_retry.py's `role = 'assistant'` filter (added in
    sub-project 1e's final review) so a failed manual send still auto-retries.
    """
    conversation_id = await store.get_or_create("+919876500095")
    message_id = await store.append_message(
        conversation_id, "assistant", "Delayed by a day, sorry!", sender="admin"
    )
    await store.set_message_wamid(message_id, "wamid.MANUALRETRY1")
    info = await store.get_message_retry_info("wamid.MANUALRETRY1")
    assert info is not None
    assert info.content == "Delayed by a day, sorry!"
```

Adapt the `store`/fixture name to whatever the existing file's fixture is actually called (read the file first — do not guess).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/ -k "sender or ai_memory or delivery_retry_lookup" -v`
Expected: FAIL — `TypeError: append_message() got an unexpected keyword argument 'sender'` (or `AttributeError: 'StoredMessage' object has no attribute 'sender'`).

- [ ] **Step 3: Widen `StoredMessage` and the `ConversationStore.append_message` Protocol**

In `backend/app/store/base.py`, change:

```python
@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None
    delivery_status: str | None = None
```

to:

```python
@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None
    delivery_status: str | None = None
    sender: str | None = None
```

And change the Protocol method:

```python
    async def append_message(self, conversation_id: int, role: str, content: str) -> int: ...
```

to:

```python
    async def append_message(
        self, conversation_id: int, role: str, content: str, sender: str | None = None
    ) -> int: ...
```

- [ ] **Step 4: Implement in `postgres.py`**

In `backend/app/store/postgres.py`, change `append_message`:

```python
    async def append_message(self, conversation_id: int, role: str, content: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)"
                " RETURNING id",
                conversation_id,
                role,
                content,
            )
        return int(row["id"])
```

to:

```python
    async def append_message(
        self, conversation_id: int, role: str, content: str, sender: str | None = None
    ) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO messages (conversation_id, role, content, sender) VALUES"
                " ($1, $2, $3, $4) RETURNING id",
                conversation_id,
                role,
                content,
                sender,
            )
        return int(row["id"])
```

Change `recent_messages`'s SELECT from `"SELECT role, content, created_at, delivery_status FROM messages ..."` to `"SELECT role, content, created_at, delivery_status, sender FROM messages ..."`, and its `StoredMessage(...)` construction to add `sender=r["sender"]`.

Change `find_messages_by_user_id`'s SELECT from `"SELECT m.role, m.content, m.created_at, m.delivery_status FROM messages m ..."` to `"SELECT m.role, m.content, m.created_at, m.delivery_status, m.sender FROM messages m ..."`, and its `StoredMessage(...)` construction to add `sender=r["sender"]`.

- [ ] **Step 5: Implement in `memory.py`**

In `backend/app/store/memory.py`, add a `sender: str | None` field to `_MessageRow` (near the existing `delivery_status: str | None` field), add `sender=row.sender` to `_message_view`'s `StoredMessage(...)` construction, and widen `append_message`:

```python
    async def append_message(
        self, conversation_id: int, role: str, content: str, sender: str | None = None
    ) -> int:
        row = _MessageRow(
            id=self._message_next_id,
            role=role,
            content=content,
            created_at=datetime.now(UTC).isoformat(),
            wamid=None,
            delivery_status=None,
            sender=sender,
        )
        ...
```

(Read the current full method body first — only the `_MessageRow(...)` construction's arguments change; the rest of the method, whatever it does after constructing `row`, is unmodified.)

- [ ] **Step 6: Add the schema migration**

In `backend/app/store/schema.sql`, immediately after the existing `retry_count` ALTER for `messages` (the line `ALTER TABLE messages ADD COLUMN IF NOT EXISTS retry_count int NOT NULL DEFAULT 0;`), add:

```sql
-- Display-only marker distinguishing a manually-typed admin reply from an AI-generated one
-- (admin manual-reply feature). NULL means "AI" (every existing row, and every future
-- AI-generated row); 'admin' marks a row sent via the admin panel's message box. Deliberately
-- NOT part of `role` (which stays 'assistant' for both) -- `role` is load-bearing for
-- core/memory.py's AI context window and delivery_retry.py's role='assistant' filter, and this
-- column must never affect either. Additive + idempotent, so no live migration is required.
-- NOTE: an OWNER-RUN manual migration -- nothing in the app executes schema.sql automatically;
-- documented here as the source-of-truth DDL.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender text;
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/ -k "sender or ai_memory or delivery_retry_lookup" -v`
Expected: PASS (5 new tests, ×2 if the file parametrizes shared tests over both Postgres and in-memory backends — adjust expectation to whatever the actual fixture structure produces; the `load_history`/`get_message_retry_info` tests only need to run once each if the file's fixture isn't already dual-backend for this test class).

- [ ] **Step 8: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures. In particular, confirm `core/memory.py::load_history` (which constructs its own `Message(role=..., content=...)` objects from `StoredMessage` — it does not read `.sender`) still passes unmodified; the new field is additive and inert there by construction.

- [ ] **Step 9: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/app/store/schema.sql backend/tests/store/
git commit -m "feat(store): add messages.sender display marker for admin manual replies"
```

---

### Task 2: Backend endpoint — `POST /admin/conversations/{thread_id}/messages`

**Files:**
- Modify: `backend/app/admin/router.py` (new `ManualReplyRequest` model, new endpoint, `get_conversation_thread`'s entry construction gains `sender`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `ConversationStore.append_message(conversation_id, role, content, sender=None) -> int` and `StoredMessage.sender` (Task 1). `send_text(http, cfg, to, body, timeout=20.0) -> SendResult` and `WhatsAppSendError` (`app/channels/whatsapp_sender.py`, unchanged, already used identically in `core/conversation.py`). `HANDOFF_PAUSE_WINDOW` (`app/core/conversation.py`, unchanged — `timedelta(hours=24)`). `ConversationStore.set_message_wamid(message_id, wamid) -> None` and `ConversationStore.pause_until(conversation_id, until) -> None` (both already implemented, unchanged). `load_whatsapp_config(c.config) -> WhatsAppConfig` (already imported in `router.py`).
- Produces: `POST /admin/conversations/{thread_id}/messages` — 401 without admin auth, 404 for an unknown `thread_id`, 400 for empty/whitespace `text`, `{"ok": true, "wamid": str | None}` (200) on a send that at least persisted (wamid present only if the WhatsApp send itself succeeded), `{"ok": false, "error": str}` (502) if the WhatsApp send failed. `GET /admin/conversations/{thread_id}`'s existing response gains a `"sender"` field on every `ai_reply` entry (`"admin"` or `null`).

- [ ] **Step 1: Write the failing backend tests**

Add to `backend/tests/admin/test_views.py`:

```python
class _FakeTextSender:
    def __init__(self, result: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(self, http, cfg, to, body, timeout=20.0):
        self.calls.append({"to": to, "body": body, "timeout": timeout})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _seed_whatsapp_config() -> None:
    c = get_container()
    asyncio.run(c.config.set_secret("whatsapp:access_token", "tok"))
    asyncio.run(c.config.set_secret("whatsapp:app_secret", "sec"))
    asyncio.run(c.config.set_secret("whatsapp:verify_token", "ver"))
    asyncio.run(c.config.set_plain("whatsapp:phone_number_id", "1298805403309058"))
    asyncio.run(c.config.set_plain("whatsapp:waba_id", "2454816495000045"))
    asyncio.run(c.config.set_plain("whatsapp:api_version", "v23.0"))


def test_manual_reply_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/conversations/1/messages", json={"text": "hi"})
    assert resp.status_code == 401


def test_manual_reply_unknown_thread_id_returns_404(client: TestClient, monkeypatch) -> None:
    login(client)
    _seed_whatsapp_config()
    resp = client.post("/admin/conversations/900000000002/messages", json={"text": "hi"})
    assert resp.status_code == 404


def test_manual_reply_rejects_empty_text(client: TestClient) -> None:
    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500040"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "   "})
    assert resp.status_code == 400


def test_manual_reply_sends_persists_and_pauses_ai(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500041"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    fake = _FakeTextSender(SendResult(ok=True, status_code=200, wamid="wamid.MANUAL1", error=None))
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "On its way!"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "wamid": "wamid.MANUAL1"}
    assert fake.calls == [{"to": normalized, "body": "On its way!", "timeout": 20.0}]

    messages = asyncio.run(
        get_container().conversations.find_messages_by_user_id(normalized, limit=10)
    )
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "On its way!"
    assert messages[-1].sender == "admin"

    paused_until = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert paused_until is not None
    assert paused_until > datetime.now(UTC) + timedelta(hours=23)


def test_manual_reply_send_mode_off_still_sends(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual admin reply deliberately bypasses the send_mode kill switch (design decision)."""
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500042"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    controls = asyncio.run(get_container().config.get_plain("send_mode"))  # sanity: default
    from app.admin.controls import save_controls

    asyncio.run(
        save_controls(
            get_container().config,
            AdminControls(
                send_mode="off",
                allowlist_phones=[],
                owner_alert_number="",
                default_language="en",
            ),
        )
    )

    fake = _FakeTextSender(SendResult(ok=True, status_code=200, wamid="wamid.MANUAL2", error=None))
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "hello"})

    assert resp.status_code == 200
    assert len(fake.calls) == 1


def test_manual_reply_reports_send_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.whatsapp_sender import WhatsAppSendError

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500043"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    fake = _FakeTextSender(WhatsAppSendError("timeout"))
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "hello"})

    assert resp.status_code == 502
    assert resp.json()["ok"] is False

    messages = asyncio.run(
        get_container().conversations.find_messages_by_user_id(normalized, limit=10)
    )
    assert messages[-1].content == "hello"
    assert messages[-1].sender == "admin"

    paused_until = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert paused_until is not None


def test_conversation_thread_reports_sender_on_manual_reply(client: TestClient) -> None:
    login(client)
    normalized = "+919876500044"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    asyncio.run(
        get_container().conversations.append_message(
            thread_id, "assistant", "manual text", sender="admin"
        )
    )

    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    ai_entries = [e for e in resp.json()["entries"] if e["type"] == "ai_reply"]
    assert ai_entries[-1]["sender"] == "admin"
```

This requires adding `import pytest` and `from app.admin.controls import AdminControls` to `test_views.py`'s imports if not already present — read the file's current imports first (Task 1's summary above already lists `asyncio`, `json`, `datetime`/`UTC`/`timedelta`, `TestClient`, `order_from_webhook_payload`, `get_container`, `MappingUpsert`/`OutboundDraft` as currently present; `pytest` and `AdminControls` are new).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "manual_reply" -v`
Expected: FAIL — 404/405 (route doesn't exist yet) or `AttributeError` for `app.admin.router.send_text` (not yet imported into that module).

- [ ] **Step 3: Implement the backend**

In `backend/app/admin/router.py`, add to the imports:

```python
from app.channels.whatsapp_sender import SendResult, WhatsAppSendError, send_text
from app.core.conversation import HANDOFF_PAUSE_WINDOW
```

Add a new request model near the other `*Request` models (e.g. near `ErasureRequest`):

```python
class ManualReplyRequest(BaseModel):
    text: str
```

Add a new endpoint directly below `resume_conversation()`:

```python
@admin_router.post(
    "/conversations/{thread_id}/messages", dependencies=[Depends(require_admin)]
)
async def send_manual_reply(thread_id: int, body: ManualReplyRequest) -> dict[str, object]:
    """Send a free-text WhatsApp message typed by the admin, mirroring core/conversation.py's
    AI-reply send path so the same delivery-status tick marks and auto-retry machinery apply
    for free (both key off a wamid on a `messages` row, regardless of how the row was created).

    Deliberately bypasses send_decision/send_mode/allowlist_phones -- a manual, targeted admin
    reply is not what the kill switch exists to stop (design decision, see
    docs/superpowers/specs/2026-08-18-admin-manual-reply-design.md). Persisted with
    role="assistant" (so it stays in the AI's memory window and stays eligible for
    delivery_retry.py's role='assistant' filter) and sender="admin" (display-only marker, never
    read by memory/retry logic).
    """
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")

    message_id = await c.conversations.append_message(
        thread_id, "assistant", text, sender="admin"
    )

    wa_cfg = await load_whatsapp_config(c.config)
    try:
        result = await send_text(c.http, wa_cfg, user_id, text)
    except WhatsAppSendError as exc:
        await c.conversations.pause_until(thread_id, datetime.now(UTC) + HANDOFF_PAUSE_WINDOW)
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=502, detail="failed to send message") from exc

    await c.conversations.pause_until(thread_id, datetime.now(UTC) + HANDOFF_PAUSE_WINDOW)

    if not result.ok:
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=502, detail=result.error or "send failed")

    if result.wamid:
        try:
            await c.conversations.set_message_wamid(message_id, result.wamid)
        except Exception:
            logger.exception("failed to record wamid for manual admin reply")

    _audit("admin_manual_reply", "success", resource=f"thread:{thread_id}")
    return {"ok": True, "wamid": result.wamid}
```

Check the actual current signature of `load_whatsapp_config` before pasting (`async def load_whatsapp_config(config) -> WhatsAppConfig` is assumed here, matching its existing use elsewhere in this same file — confirm by reading its other call site in `router.py` first).

In `get_conversation_thread()`, change the `ai_reply` entry construction:

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

to:

```python
        entry: dict[str, object] = {
            "type": "customer_message" if msg.role == "user" else "ai_reply",
            "timestamp": msg.created_at,
            "text": msg.content,
        }
        if msg.role == "assistant":
            entry["delivery_status"] = msg.delivery_status
            entry["sender"] = msg.sender
        entries.append(entry)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "manual_reply" -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures. Confirm `backend/app/core/order_actions.py` is untouched: `git diff -- backend/app/core/order_actions.py` returns empty.

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): add POST /admin/conversations/{id}/messages for manual WhatsApp replies"
```

---

### Task 3: Frontend — message box, 24h-window gating, sender-aware bubble

**Files:**
- Modify: `backend/app/admin/static/chats.html` (input + send button markup/CSS)
- Modify: `backend/app/admin/static/chats.js` (`renderBubble`, `loadThread`, new send handler, `api()` gains an optional body)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `POST /admin/conversations/{thread_id}/messages` (Task 2), the `entries[].sender` field on `ai_reply` entries (Task 2), the already-existing `entries[].timestamp`/`type` fields.
- Produces: no new backend interface — purely presentational.

- [ ] **Step 1: Write the failing frontend smoke tests**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_page_has_manual_reply_box(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="reply-input"' in resp.text
    assert 'id="reply-send-btn"' in resp.text


def test_chats_js_wires_manual_reply_send(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/messages" in js
    assert "reply-send-btn" in js
    assert "reply-input" in js
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "manual_reply" -v`
Expected: FAIL.

- [ ] **Step 3: Implement the frontend — HTML**

In `backend/app/admin/static/chats.html`, add a reply bar inside `#chat-pane`, directly after the existing `<div class="status" id="thread-status"></div>` line:

```html
      <div class="status" id="thread-status"></div>
      <div id="reply-bar">
        <input id="reply-input" type="text" placeholder="Type a message" />
        <button id="reply-send-btn">Send</button>
      </div>
      <div class="status" id="reply-status"></div>
```

CSS, near the existing `#chat-empty` rule:

```css
    #reply-bar { display: flex; gap: .5rem; padding: .6rem 1.2rem; background: #f0f2f5;
      border-top: 1px solid #e9edef; }
    #reply-input { flex: 1; padding: .5rem .7rem; border: 1px solid #d1d7db; border-radius: 8px;
      font-size: .85rem; outline: none; }
    #reply-input:disabled { background: #e9edef; color: #8696a0; }
    #reply-send-btn { background: #00a884; color: #fff; border: none; border-radius: 6px;
      padding: .5rem 1.1rem; font-size: .82rem; cursor: pointer; }
    #reply-send-btn:disabled { background: #8696a0; cursor: not-allowed; }
    #reply-status { font-size: .72rem; color: #dc2626; padding: 0 1.2rem .3rem; min-height: 1em; }
```

(`.status` already exists as a shared rule with `color: #dc2626`; `#reply-status` above just adds the padding/min-height specific to this bar — read the current stylesheet first to avoid a duplicate/conflicting rule if `.status` already covers enough.)

- [ ] **Step 4: Implement the frontend — JS: sender-aware bubble label**

In `chats.js`, change `renderBubble`'s label line:

```javascript
  label.textContent = entry.type.replace("_", " ");
```

to:

```javascript
  label.textContent =
    entry.type === "ai_reply" && entry.sender === "admin" ? "you" : entry.type.replace("_", " ");
```

- [ ] **Step 5: Implement the frontend — JS: extend `api()` to accept a body**

Change:

```javascript
async function api(path, method = "GET") {
  const opts = { method, credentials: "same-origin" };
  if (method !== "GET") {
    // A bodyless POST sends no Content-Length, which Vercel's edge rejects with a 411 before the
    // request reaches the app. Attach an empty JSON body so the edge lets it through; the FastAPI
    // route ignores the body.
    opts.headers = { "Content-Type": "application/json" };
    opts.body = "{}";
  }
  const res = await fetch(path, opts);
```

to:

```javascript
async function api(path, method = "GET", body = null) {
  const opts = { method, credentials: "same-origin" };
  if (method !== "GET") {
    // A bodyless POST sends no Content-Length, which Vercel's edge rejects with a 411 before the
    // request reaches the app. Default to an empty JSON body so the edge lets it through even when
    // the caller has nothing to send; a caller with real data passes it via `body`.
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body === null ? {} : body);
  }
  const res = await fetch(path, opts);
```

- [ ] **Step 6: Implement the frontend — JS: 24h-window gating + send handler**

Add a helper near `formatBubbleTime`:

```javascript
const REPLY_WINDOW_MS = 24 * 60 * 60 * 1000;

function lastCustomerMessageAt(entries) {
  for (let i = entries.length - 1; i >= 0; i--) {
    if (entries[i].type === "customer_message") return new Date(entries[i].timestamp);
  }
  return null;
}
```

In `loadThread`, after the existing `resumeBtn.style.display = ...` line, add:

```javascript
    const lastCustomerAt = lastCustomerMessageAt(data.entries);
    const withinWindow = lastCustomerAt && (Date.now() - lastCustomerAt.getTime()) < REPLY_WINDOW_MS;
    const replyInput = el("reply-input");
    const replySendBtn = el("reply-send-btn");
    replyInput.disabled = !withinWindow;
    replySendBtn.disabled = !withinWindow;
    replyInput.placeholder = withinWindow
      ? "Type a message"
      : "Outside the 24-hour reply window — send a template instead";
```

Add a send handler near the other top-level event listener registrations (e.g. near `resume-ai-btn`'s):

```javascript
el("reply-send-btn").addEventListener("click", async () => {
  if (currentThreadId === null) return;
  const input = el("reply-input");
  const text = input.value.trim();
  if (!text) return;
  const btn = el("reply-send-btn");
  btn.disabled = true;
  el("reply-status").textContent = "";
  try {
    await api(
      "/admin/conversations/" + encodeURIComponent(currentThreadId) + "/messages",
      "POST",
      { text }
    );
    input.value = "";
    await loadThread(currentThreadId, currentPhone);
  } catch (e) {
    el("reply-status").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

el("reply-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") el("reply-send-btn").click();
});
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "manual_reply" -v`
Expected: PASS (2 tests).

- [ ] **Step 8: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures.

- [ ] **Step 9: Manual browser verification**

No browser test runner exists in this repo (same known gap as every other admin-panel frontend change). Before handing off to review, manually verify in a browser: the reply box is disabled with the correct placeholder for a thread whose last customer message is >24h old; enabled for a recent thread; typing and sending a message shows it immediately with a tick mark; the "Resume AI" button appears after sending (confirming the pause was set); pressing Enter sends the same as clicking the button.

- [ ] **Step 10: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): add manual-reply message box to the chat page"
```

---

## Post-Implementation Notes

- No task in this plan touches `backend/app/core/order_actions.py` — verify with `git diff <base-commit> HEAD -- backend/app/core/order_actions.py` returning empty before handing off to review.
- The `security-reviewer` pass should specifically confirm: (1) the deliberate kill-switch bypass is exactly as scoped in the design (a targeted single-thread admin send, not a batch/broadcast primitive — `require_admin` plus the per-`thread_id` shape structurally prevents anything broader); (2) no path allows sending to a phone number not already tied to an existing conversation (the endpoint resolves `user_id` from `thread_id` server-side — the request body carries no phone number); (3) `text` is sent to WhatsApp as-is with no way to inject the customer's own data or trigger a mutation (this endpoint only calls `send_text`, never anything in `order_actions.py`).
- Owner must run the `messages.sender` migration (Task 1, Step 6) before this deploys — narrow blast radius: `sender` is read only by the admin UI's rendering and the new endpoint's write path, never by any hot path or by `core/memory.py`/`delivery_retry.py`.
