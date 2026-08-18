# Resume AI Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin manually clear a customer's AI handoff pause from the chats page, so a conversation stuck under the 24h pause window can be handed back to the AI immediately.

**Architecture:** One new response field on the existing thread-detail endpoint, one new admin-gated mutation endpoint reusing the already-existing `pause_until`/`get_paused_until` store primitives, and a conditional button in the chat page's frontend.

**Tech Stack:** Python 3.12 / FastAPI (backend), vanilla JS (frontend), pytest (backend tests), Python `TestClient`-based markup/JS-substring assertions (frontend smoke tests — no browser test runner in this repo).

## Global Constraints

- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- No schema/migration changes — reuses `ConversationStore.pause_until`/`get_paused_until`, both already implemented in `app/store/memory.py` and `app/store/postgres.py`.
- `backend/app/core/order_actions.py` is never touched.
- No new secrets, no new Shopify/Meta API calls.
- Design source of truth: `docs/superpowers/specs/2026-08-17-resume-ai-button-design.md`.

---

### Task 1: Backend field + endpoint, frontend button

**Files:**
- Modify: `backend/app/admin/router.py` (`get_conversation_thread`, new `resume_conversation` endpoint)
- Modify: `backend/app/admin/static/chats.html` (button element + CSS)
- Modify: `backend/app/admin/static/chats.js` (`loadThread`, new resume-click handler)
- Test: `backend/tests/admin/test_views.py`, `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `ConversationStore.get_paused_until(conversation_id: int) -> datetime | None` and `ConversationStore.pause_until(conversation_id: int, until: datetime) -> None` (`app/store/base.py:287,289` — both already implemented, no changes to either).
- Produces: `GET /admin/conversations/{thread_id}`'s response gains `"paused_until": str | None` (ISO-8601 or `null`). New `POST /admin/conversations/{thread_id}/resume` returns `{"ok": True}` on success, 404 on an unknown `thread_id`.

- [ ] **Step 1: Write the failing backend tests**

Add to `backend/tests/admin/test_views.py` (near the other `test_conversation_thread_*` tests):

```python
def test_conversation_thread_reports_paused_until_when_paused(client: TestClient) -> None:
    login(client)
    normalized = "+919876500030"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    future = datetime.now(UTC) + timedelta(hours=24)
    asyncio.run(get_container().conversations.pause_until(thread_id, future))

    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    paused_until = resp.json()["paused_until"]
    assert paused_until is not None
    assert paused_until.startswith(future.date().isoformat())


def test_conversation_thread_paused_until_null_when_not_paused(client: TestClient) -> None:
    login(client)
    normalized = "+919876500031"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    assert resp.json()["paused_until"] is None


def test_resume_conversation_clears_the_pause(client: TestClient) -> None:
    login(client)
    normalized = "+919876500032"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    future = datetime.now(UTC) + timedelta(hours=24)
    asyncio.run(get_container().conversations.pause_until(thread_id, future))

    resp = client.post(f"/admin/conversations/{thread_id}/resume")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    cleared = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert cleared is None or cleared <= datetime.now(UTC)


def test_resume_conversation_unknown_thread_id_returns_404(client: TestClient) -> None:
    login(client)

    resp = client.post("/admin/conversations/900000000001/resume")

    assert resp.status_code == 404


def test_resume_conversation_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/conversations/1/resume")

    assert resp.status_code == 401


def test_resume_conversation_on_unpaused_thread_is_a_harmless_noop(client: TestClient) -> None:
    login(client)
    normalized = "+919876500033"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    resp = client.post(f"/admin/conversations/{thread_id}/resume")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
```

This requires `from datetime import UTC, datetime, timedelta` to be added to `test_views.py`'s imports — confirmed by reading the file's current imports (`asyncio`, `json`, `fastapi.testclient.TestClient`, `app.channels.shopify_orders.order_from_webhook_payload`, `app.deps.get_container`, `app.store.base.{MappingUpsert, OutboundDraft}`): none of `datetime`/`UTC`/`timedelta` are currently imported in this file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "paused_until or resume_conversation" -v`
Expected: FAIL — `paused_until` key missing from the response, and `/resume` route returns 404/405 (route doesn't exist yet).

- [ ] **Step 3: Implement the backend**

In `backend/app/admin/router.py`, modify `get_conversation_thread()`'s return statement (currently `return {"entries": entries, "orders": order_summaries}`):

```python
    paused_until = await c.conversations.get_paused_until(thread_id)
    return {
        "entries": entries,
        "orders": order_summaries,
        "paused_until": paused_until.isoformat() if paused_until else None,
    }
```

Add a new endpoint directly below `get_conversation_thread()`:

```python
@admin_router.post(
    "/conversations/{thread_id}/resume", dependencies=[Depends(require_admin)]
)
async def resume_conversation(thread_id: int) -> dict[str, object]:
    """Manually clear a conversation's AI handoff pause, putting the AI back in charge.

    Reuses the existing pause_until/get_paused_until primitives (core/conversation.py's pause
    check is `now < paused_until`) -- setting `until` to now is equivalent to clearing the pause,
    no new store method needed. A conversation that isn't currently paused is a harmless no-op.
    """
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")
    await c.conversations.pause_until(thread_id, datetime.now(UTC))
    _audit("resume_ai", "success", resource=f"thread:{thread_id}")
    return {"ok": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "paused_until or resume_conversation" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Write the failing frontend smoke tests**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_page_has_resume_ai_button(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="resume-ai-btn"' in resp.text


def test_chats_js_calls_the_resume_endpoint(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/resume" in js
    assert "paused_until" in js
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "resume_ai or resume_endpoint" -v`
Expected: FAIL

- [ ] **Step 7: Implement the frontend — HTML**

In `backend/app/admin/static/chats.html`, add a button inside `#chat-header` (currently just `<div id="chat-header"></div>` — read the current file first, since Task 6/7 of the earlier plan may have changed this element's exact markup):

```html
    <div id="chat-header">
      <span id="chat-header-phone"></span>
      <button id="resume-ai-btn" style="display:none">Resume AI</button>
    </div>
```

If `#chat-header` currently has other content set via `el("chat-header").textContent = phone || ""` in `chats.js`, this HTML change requires updating that JS to target a new `#chat-header-phone` span instead of the container directly (see Step 8) — read the current `loadThread()` implementation first to confirm the exact current behavior before changing it.

CSS, near the existing `#chat-header` rule:

```css
    #chat-header { display: flex; align-items: center; justify-content: space-between; }
    #resume-ai-btn { background: #00a884; color: #fff; border: none; border-radius: 6px;
      padding: .35rem .8rem; font-size: .78rem; cursor: pointer; }
```

- [ ] **Step 8: Implement the frontend — JS**

In `backend/app/admin/static/chats.js`, read the CURRENT `loadThread()` function first (it has been modified by several earlier tasks today — the poll mechanism, the `silent` parameter, etc. — do not assume the plan's line numbers are current). Make these changes:

1. Change the line that sets the header text from `el("chat-header").textContent = phone || "";` to `el("chat-header-phone").textContent = phone || "";` (matching the new HTML structure from Step 7).
2. After the thread data is fetched and rendered (near where `renderOrderPanel(data.orders)` is called), add:
```js
    const resumeBtn = el("resume-ai-btn");
    const isPaused = data.paused_until && new Date(data.paused_until) > new Date();
    resumeBtn.style.display = isPaused ? "inline-block" : "none";
```
3. Add a one-time click handler (near the other top-level event listener registrations, e.g. near the `refresh-btn` listener):
```js
el("resume-ai-btn").addEventListener("click", async () => {
  if (currentThreadId === null) return;
  await api("/admin/conversations/" + encodeURIComponent(currentThreadId) + "/resume", "POST");
  await loadThread(currentThreadId, currentPhone);
});
```
4. Check the existing `api()` helper's signature — as of today it only takes a `path` argument (`async function api(path) { ... fetch(path, { method: "GET", ... }) ... }`) and always issues a GET. Extend it to accept an optional `method` parameter defaulting to `"GET"`, e.g.:
```js
async function api(path, method = "GET") {
  const res = await fetch(path, { method, credentials: "same-origin" });
  ...
```
(read the current implementation first — the exact current shape may differ slightly from what's shown above since multiple tasks have touched this file today; adapt these snippets to whatever the file currently contains rather than pasting them verbatim if they don't match.)

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "resume_ai or resume_endpoint" -v`
Expected: PASS (2 tests)

- [ ] **Step 10: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures.

- [ ] **Step 11: Commit**

```bash
git add backend/app/admin/router.py backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_views.py backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): add Resume AI button to manually clear a conversation's handoff pause"
```

---

## Post-Implementation Notes

- No task in this plan touches `backend/app/core/order_actions.py` — verify with `git diff <base-commit> HEAD -- backend/app/core/order_actions.py` returning empty before handing off to review.
- After this ships and is reviewed/deployed, the owner should use the button to resume Ravi Pandey's specific conversation (+918238232528) — that is a manual operational action on the owner's part, not something this plan automates.
- Manual browser verification is still required (same known gap as every other frontend change in this admin panel — no browser test runner exists here): confirm the button actually shows/hides correctly and that clicking it visibly restores AI replies on the next customer message.
- Route to `code-reviewer` after this lands, per this project's standard post-feature process. Given the live-incident urgency, `security-reviewer` is optional here (no new auth, no PII exposure, no mutation-safety-core changes) but is available if the code-reviewer's findings suggest it's warranted.
