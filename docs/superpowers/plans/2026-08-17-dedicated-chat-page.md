# Dedicated WhatsApp-Style Chat Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chat card embedded in the settings panel with a standalone, three-pane WhatsApp-styled page (`/admin/ui/chats.html`) that also shows the customer's order(s) alongside the conversation.

**Architecture:** One small additive change to the already-reviewed thread endpoint (adds an `orders` field), removal of the now-superseded embedded card, and a brand-new self-contained static page/script pair consuming the (slightly widened) existing API. No new backend concepts — this reuses `find_mirrored_orders_by_phone`, `get_or_create`, and the merge logic already built and reviewed.

**Tech Stack:** Python 3.12, FastAPI, pytest, plain HTML/JS (no framework, no build step) — matching this repo's existing admin panel conventions exactly.

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean (64 files today).
- `ruff check .` clean. No bare `except:`. No `print()`.
- The new page requires the exact same `Depends(require_admin)` session-cookie auth as every other admin route — no new auth mechanism, no separate login form on the new page.
- `backend/app/core/order_actions.py` must remain byte-identical throughout — this feature only reads `order_actions`/mirrored orders, never mutates. Verify via `git diff` at the end of every task.
- No schema/migration changes — `find_mirrored_orders_by_phone` already exists and is already populated.
- The chat-entry merge logic, dedupe, and opaque `thread_id` scheme (already built and reviewed) are NOT touched — only a new field is added alongside them.
- Secrets/print/bare-except compliance grep (from `no-secrets.md`) must return empty on every touched `.py` file:
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>`
- Do not push to git — commit locally only, per this repo's standing rule (owner approves pushes separately).

---

## File Structure

- **Modify** `backend/app/admin/router.py` — `get_conversation_thread`'s response shape changes from a bare `list[dict]` to `{"entries": [...], "orders": [...]}`; new `_order_summary(order)` helper; new `Order` import.
- **Modify** `backend/app/admin/static/index.html` — remove the `chats-card` block and its now-unused chat-bubble CSS (superseded by the new dedicated page).
- **Modify** `backend/app/admin/static/admin.js` — remove `renderChatEntry`/`loadChatThread`/`loadChatList` and the `chats-refresh` listener; drop `loadChatList()` from `loadAll()`'s `Promise.all`.
- **Create** `backend/app/admin/static/chats.html` — the new standalone three-pane page.
- **Create** `backend/app/admin/static/chats.js` — its own JS, independent of `admin.js` (no shared state between the settings panel and this page).
- **Test files** (extend existing, no new files beyond what's listed): `backend/tests/admin/test_views.py`, `backend/tests/admin/test_static_mount.py`.

---

### Task 1: Add order details to the thread endpoint

**Files:**
- Modify: `backend/app/admin/router.py:682-722` (`get_conversation_thread`), imports at the top of the file
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Produces: `GET /admin/conversations/{thread_id}` now returns `{"entries": list[dict], "orders": list[dict]}` instead of a bare `list[dict]`. Each `orders` entry: `{"order_name": str, "financial_status": str | None, "fulfillment_status": str | None, "cancelled_at": str | None, "is_cod": bool, "total_amount": str | None, "total_currency": str | None, "tags": list[str], "tracking_company": str | None, "tracking_number": str | None, "tracking_url": str | None}`, sorted most-recently-updated first.
- Consumed by: Task 3 (the new `chats.js`).

- [ ] **Step 1: Write the failing tests**

First, the two EXISTING tests that treat the response as a bare list need updating for the new wrapper shape. In `backend/tests/admin/test_views.py`, change `test_conversation_thread_merges_all_three_sources` (currently lines 143-174) — change:

```python
    assert resp.status_code == 200
    entries = resp.json()
    types = [e["type"] for e in entries]
```

to:

```python
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    types = [e["type"] for e in entries]
```

Change `test_conversation_thread_non_dict_payload_degrades_not_500` (currently lines 185-197) — change:

```python
    assert resp.status_code == 200
    template_entry = next(e for e in resp.json() if e["type"] == "template_sent")
```

to:

```python
    assert resp.status_code == 200
    template_entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
```

`order_from_webhook_payload` is NOT currently imported in this file — add it to the existing import block at the top (currently lines 1-7):

```python
import asyncio
import json

from fastapi.testclient import TestClient

from app.channels.shopify_orders import order_from_webhook_payload
from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft
```

Now add new tests proving the `orders` field, right after `test_conversation_thread_non_dict_payload_degrades_not_500`:

```python
def test_conversation_thread_includes_order_summary(client: TestClient) -> None:
    login(client)
    normalized = "+919876543210"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/orders1",
        "name": "tavas500",
        "phone": normalized,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "tags": "vip, repeat",
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "1299.00",
        "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    summary = orders[0]
    assert summary["order_name"] == "tavas500"
    assert summary["financial_status"] == "paid"
    assert summary["fulfillment_status"] == "fulfilled"
    assert summary["is_cod"] is True
    assert summary["total_amount"] == "1299.00"
    assert summary["total_currency"] == "INR"
    assert "vip" in summary["tags"]
    assert summary["tracking_company"] is None


def test_conversation_thread_no_orders_returns_empty_list(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919111222333", "hi", "hello there")

    thread_id = _thread_id_for(client, "+919111222333")
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    assert resp.json()["orders"] == []


def test_conversation_thread_multiple_orders_sorted_most_recent_first(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919555666777"

    older = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/older",
        "name": "tavas-older", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "500.00", "currency": "INR", "updated_at": "2026-08-01T00:00:00+05:30",
    })
    newer = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/newer",
        "name": "tavas-newer", "phone": normalized, "financial_status": "pending",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "700.00", "currency": "INR", "updated_at": "2026-08-15T00:00:00+05:30",
    })
    assert older is not None and newer is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(older))
    asyncio.run(get_container().ingest.upsert_order_mirror(newer))

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    order_names = [o["order_name"] for o in resp.json()["orders"]]
    assert order_names == ["tavas-newer", "tavas-older"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -v`
Expected: FAIL — the two updated tests fail because `resp.json()` is currently a bare list, not a dict with `["entries"]`; the three new tests fail with `KeyError: 'orders'`.

- [ ] **Step 3: Implement the `orders` field**

In `backend/app/admin/router.py`, add `Order` to the existing `app.shopify.models` import — check whether such an import already exists in this file (grep for `from app.shopify.models import`); if not, add a new line near the other model imports:

```python
from app.shopify.models import Order
```

Add the summary helper right before `get_conversation_thread` (currently starts at line 682):

```python
def _order_summary(order: Order) -> dict[str, object]:
    tracking_company = tracking_number = tracking_url = None
    if order.fulfillments:
        first = order.fulfillments[0]
        tracking_company = first.tracking_company
        tracking_number = first.tracking_number
        tracking_url = first.tracking_url
    return {
        "order_name": order.name,
        "financial_status": order.financial_status,
        "fulfillment_status": order.fulfillment_status,
        "cancelled_at": order.cancelled_at,
        "is_cod": order.is_cod(),
        "total_amount": order.total.amount if order.total else None,
        "total_currency": order.total.currency if order.total else None,
        "tags": list(order.tags),
        "tracking_company": tracking_company,
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
    }
```

Change `get_conversation_thread`'s signature and return (currently lines 682-722) from:

```python
@admin_router.get("/conversations/{thread_id}", dependencies=[Depends(require_admin)])
async def get_conversation_thread(thread_id: int) -> list[dict[str, object]]:
    c = get_container()
    # Resolve the conversation's normalized phone from the opaque id; a bad literal id genuinely
    # does not exist -> 404 (distinct from "no messages yet", which is a real empty thread).
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")

    entries: list[dict[str, object]] = []
```

to:

```python
@admin_router.get("/conversations/{thread_id}", dependencies=[Depends(require_admin)])
async def get_conversation_thread(thread_id: int) -> dict[str, object]:
    c = get_container()
    # Resolve the conversation's normalized phone from the opaque id; a bad literal id genuinely
    # does not exist -> 404 (distinct from "no messages yet", which is a real empty thread).
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")

    entries: list[dict[str, object]] = []
```

(Only the return-type annotation changes here, from `list[dict[str, object]]` to `dict[str, object]` — the body up to the entries-building loop is otherwise identical.)

Change the function's ending (currently):

```python
    # Timestamps are ISO 8601 strings (or None -> ""), which sort lexicographically in
    # chronological order. str() keeps the key type mypy-checkable (object -> str).
    entries.sort(key=lambda e: str(e["timestamp"] or ""))
    return entries
```

to:

```python
    # Timestamps are ISO 8601 strings (or None -> ""), which sort lexicographically in
    # chronological order. str() keeps the key type mypy-checkable (object -> str).
    entries.sort(key=lambda e: str(e["timestamp"] or ""))

    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    orders_sorted = sorted(orders, key=lambda o: str(o.updated_at or ""), reverse=True)
    order_summaries = [_order_summary(o) for o in orders_sorted]

    return {"entries": entries, "orders": order_summaries}
```

- [ ] **Step 4: Run tests to verify they pass, plus the full admin test suite**

Run: `cd backend && python -m pytest tests/admin/ -v`
Expected: PASS — every test, including the 2 updated and 3 new ones.

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
git commit -m "feat(admin): add order details to the conversation thread response"
```

---

### Task 2: Remove the embedded Chats card from the settings panel

**Files:**
- Modify: `backend/app/admin/static/index.html`
- Modify: `backend/app/admin/static/admin.js`
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — this task only removes now-superseded code.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/admin/test_static_mount.py`, replace `test_chats_panel_present`/`test_chats_panel_js_calls_the_new_endpoints` (currently lines 26-37) with tests proving the OLD embedded UI is gone:

```python
def test_old_embedded_chats_card_removed(client: TestClient) -> None:
    html = client.get("/admin/ui/").text
    assert 'id="chats-card"' not in html
    assert 'id="chats-list-table"' not in html


def test_old_embedded_chats_js_removed(client: TestClient) -> None:
    js = client.get("/admin/ui/admin.js").text
    assert "loadChatList" not in js
    assert "loadChatThread" not in js
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: FAIL — the old markup/JS is still present.

- [ ] **Step 3: Remove the card from `index.html`**

In `backend/app/admin/static/index.html`, remove the chat-specific CSS block (currently lines 44-53):

```html
    #chat-header { margin-top: .6rem; font-size: .82rem; color: #71717a; }
    #chat-thread { margin-top: .8rem; max-height: 460px; overflow-y: auto;
      display: flex; flex-direction: column; }
    .bubble { padding: .4rem .6rem; margin: .3rem 0; border-radius: 8px; max-width: 72%;
      font-size: .82rem; word-break: break-word; }
    .bubble-in { background: #f1f5f9; color: #18181b; align-self: flex-start; }
    .bubble-out { background: #e0e7ff; color: #18181b; align-self: flex-end; }
    .bubble-label { font-size: .68rem; font-weight: 500; text-transform: uppercase;
      letter-spacing: .02em; color: #71717a; margin-bottom: .15rem; }
    .bubble-ts { font-size: .66rem; color: #a1a1aa; margin-top: .15rem; }
```

Delete these 9 lines entirely (the `<style>` block's remaining rules on either side stay as-is).

Remove the `chats-card` HTML block (currently lines 219-228):

```html
    <div class="card" id="chats-card">
      <h2>Chats</h2>
      <button class="small" id="chats-refresh">Refresh threads</button>
      <div class="scroll"><table id="chats-list-table">
        <thead><tr><th>Phone</th><th>Last active</th><th>Preview</th></tr></thead>
        <tbody></tbody></table></div>
      <div id="chat-header"></div>
      <div id="chat-thread"></div>
      <div class="status" id="chats-status"></div>
    </div>
```

Delete this entire block. The preceding `views-card` closes right before it and `</div>` + `<script src="admin.js"></script>` follow right after — the file should read directly from the `views-card`'s closing `</div>` to the outer panel's closing `</div>` with nothing in between.

- [ ] **Step 4: Remove the JS from `admin.js`**

In `backend/app/admin/static/admin.js`, remove the entire `// ---- chats ----` section (currently lines 333-385):

```javascript
// ---- chats -----------------------------------------------------------------
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

async function loadChatThread(threadId, phone) {
  try {
    const entries = await api("GET", "/admin/conversations/" + encodeURIComponent(threadId));
    el("chat-header").textContent = phone ? "Thread: " + phone : "";
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
      // Load the thread by its opaque id; keep the phone OUT of the URL. Phone is still shown.
      tr.addEventListener("click", () => loadChatThread(t.thread_id, t.phone));
      for (const val of [t.phone, t.last_active_at || "", t.preview || ""]) {
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

Delete this entire section (including its trailing blank line before the `// ---- boot ----` comment).

Change `loadAll()`'s `Promise.all` array (currently):

```javascript
async function loadAll() {
  try {
    await Promise.all([
      loadShopify(), loadWhatsApp(), loadProviders(), loadKnowledge(), loadControls(),
      loadViews(), loadChatList(),
    ]);
```

to:

```javascript
async function loadAll() {
  try {
    await Promise.all([
      loadShopify(), loadWhatsApp(), loadProviders(), loadKnowledge(), loadControls(),
      loadViews(),
    ]);
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: PASS — all tests including the two new ones.

- [ ] **Step 6: Run the full suite + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/admin/static/index.html app/admin/static/admin.js
```
Expected: full suite green, grep empty. (mypy/ruff don't apply to `.html`/`.js` — omitted for this task.)

- [ ] **Step 7: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/admin/static/index.html backend/app/admin/static/admin.js backend/tests/admin/test_static_mount.py
git commit -m "refactor(admin): remove embedded Chats card, superseded by the dedicated page"
```

---

### Task 3: The new dedicated chat page

**Files:**
- Create: `backend/app/admin/static/chats.html`
- Create: `backend/app/admin/static/chats.js`
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `GET /admin/conversations` (unchanged), `GET /admin/conversations/{thread_id}` (Task 1's new `{entries, orders}` shape).
- Produces: no new interface consumed by later work — this plan's final task.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_page_served(client: TestClient) -> None:
    r = client.get("/admin/ui/chats.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="thread-list"' in r.text
    assert 'id="chat-messages"' in r.text
    assert 'id="order-panel"' in r.text


def test_chats_js_served_and_calls_the_conversations_api(client: TestClient) -> None:
    r = client.get("/admin/ui/chats.js")
    assert r.status_code == 200
    js = r.text
    assert "/admin/conversations" in js
    assert "loadThreadList" in js
    assert "loadThread" in js
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: FAIL — `chats.html`/`chats.js` don't exist yet (404).

- [ ] **Step 3: Write `chats.html`**

Create `backend/app/admin/static/chats.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Thetavas Chats</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; color: #111b21; height: 100vh; overflow: hidden; }
    #app { display: flex; height: 100vh; }

    #thread-list-pane { width: 320px; flex-shrink: 0; background: #fff;
      border-right: 1px solid #e9edef; display: flex; flex-direction: column; }
    #thread-list-header { background: #00a884; color: #fff; padding: .9rem 1rem;
      font-size: 1.05rem; font-weight: 600; display: flex; justify-content: space-between;
      align-items: center; }
    #thread-list-header button { background: rgba(255,255,255,.18); color: #fff; border: none;
      border-radius: 6px; padding: .3rem .7rem; font-size: .78rem; cursor: pointer; }
    #thread-list { flex: 1; overflow-y: auto; }
    .thread-row { padding: .7rem 1rem; border-bottom: 1px solid #f0f2f5; cursor: pointer; }
    .thread-row:hover { background: #f5f6f6; }
    .thread-row.active { background: #f0f2f5; }
    .thread-row .phone { font-weight: 600; font-size: .88rem; }
    .thread-row .preview { font-size: .78rem; color: #667781; margin-top: .15rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .thread-row .ts { font-size: .68rem; color: #8696a0; float: right; }

    #chat-pane { flex: 1; display: flex; flex-direction: column; background: #efeae2; }
    #chat-header { background: #f0f2f5; padding: .8rem 1.2rem; font-weight: 600;
      border-bottom: 1px solid #e9edef; min-height: 2.2rem; }
    #chat-messages { flex: 1; overflow-y: auto; padding: 1rem 1.5rem; display: flex;
      flex-direction: column; gap: .5rem; }
    .bubble { padding: .45rem .7rem; border-radius: 8px; max-width: 65%; font-size: .85rem;
      word-break: break-word; box-shadow: 0 1px 1px rgba(0,0,0,.08); }
    .bubble-in { background: #fff; align-self: flex-start; }
    .bubble-out { background: #d9fdd3; align-self: flex-end; }
    .bubble-label { font-size: .65rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: .03em; color: #667781; margin-bottom: .15rem; }
    .bubble-ts { font-size: .65rem; color: #8696a0; margin-top: .2rem; text-align: right; }
    #chat-empty { display: flex; align-items: center; justify-content: center; flex: 1;
      color: #667781; font-size: .9rem; }

    #order-panel { width: 300px; flex-shrink: 0; background: #fff; border-left: 1px solid #e9edef;
      padding: 1rem; overflow-y: auto; }
    #order-panel h3 { font-size: .9rem; margin-bottom: .6rem; }
    #order-select { width: 100%; padding: .4rem .5rem; border: 1px solid #d1d7db;
      border-radius: 6px; font-size: .82rem; margin-bottom: .8rem; }
    .order-field { font-size: .8rem; color: #3b4a54; margin-bottom: .4rem; }
    .order-field .label { color: #667781; }
    #order-empty { font-size: .82rem; color: #667781; }

    .status { font-size: .78rem; padding: .3rem 1rem; color: #dc2626; }
  </style>
</head>
<body>
  <div id="app">
    <div id="thread-list-pane">
      <div id="thread-list-header">
        <span>Chats</span>
        <button id="refresh-btn">Refresh</button>
      </div>
      <div id="thread-list"></div>
      <div class="status" id="list-status"></div>
    </div>
    <div id="chat-pane">
      <div id="chat-header"></div>
      <div id="chat-messages"><div id="chat-empty">Select a chat to view it</div></div>
      <div class="status" id="thread-status"></div>
    </div>
    <div id="order-panel">
      <h3>Order</h3>
      <div id="order-empty">No orders for this customer</div>
      <select id="order-select" style="display:none"></select>
      <div id="order-detail"></div>
    </div>
  </div>
  <script src="chats.js"></script>
</body>
</html>
```

- [ ] **Step 4: Write `chats.js`**

Create `backend/app/admin/static/chats.js`:

```javascript
// Dedicated WhatsApp-style chat page - standalone, no shared state with admin.js.
"use strict";

function el(id) { return document.getElementById(id); }

async function api(path) {
  const res = await fetch(path, { method: "GET", credentials: "same-origin" });
  if (res.status === 401) {
    window.location.href = "/admin/ui/";
    throw new Error("not authenticated");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const detail = data && data.detail ? JSON.stringify(data.detail) : res.status;
    throw new Error("Request failed: " + detail);
  }
  return data;
}

let currentThreadId = null;
let currentPhone = null;
let currentOrders = [];

function renderBubble(entry) {
  const div = document.createElement("div");
  const side = entry.type === "customer_message" ? "bubble-in" : "bubble-out";
  div.className = "bubble " + side;
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

function renderOrderDetail(order) {
  const container = el("order-detail");
  container.innerHTML = "";
  const fields = [
    ["Status", order.financial_status || "-"],
    ["Fulfillment", order.fulfillment_status || "not dispatched"],
    ["Cancelled", order.cancelled_at ? "yes" : "no"],
    ["Payment", order.is_cod ? "COD" : "prepaid"],
    ["Amount", order.total_amount ? order.total_amount + " " + (order.total_currency || "") : "-"],
    ["Tags", order.tags && order.tags.length ? order.tags.join(", ") : "-"],
    ["Courier", order.tracking_company || "-"],
    ["Tracking #", order.tracking_number || "-"],
  ];
  for (const [label, value] of fields) {
    const row = document.createElement("div");
    row.className = "order-field";
    row.innerHTML = "<span class='label'>" + label + ":</span> ";
    row.appendChild(document.createTextNode(value));
    container.appendChild(row);
  }
  if (order.tracking_url) {
    const link = document.createElement("a");
    link.href = order.tracking_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Track shipment";
    link.style.fontSize = ".8rem";
    container.appendChild(link);
  }
}

function renderOrderPanel(orders) {
  currentOrders = orders;
  const empty = el("order-empty");
  const select = el("order-select");
  if (!orders.length) {
    empty.style.display = "block";
    select.style.display = "none";
    el("order-detail").innerHTML = "";
    return;
  }
  empty.style.display = "none";
  select.style.display = orders.length > 1 ? "block" : "none";
  select.innerHTML = "";
  orders.forEach((o, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = o.order_name;
    select.appendChild(opt);
  });
  select.onchange = () => renderOrderDetail(currentOrders[Number(select.value)]);
  renderOrderDetail(orders[0]);
}

async function loadThread(threadId, phone) {
  currentThreadId = threadId;
  currentPhone = phone;
  document.querySelectorAll(".thread-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.threadId === String(threadId));
  });
  el("chat-header").textContent = phone || "";
  try {
    const data = await api("/admin/conversations/" + encodeURIComponent(threadId));
    const container = el("chat-messages");
    container.innerHTML = "";
    if (!data.entries.length) {
      container.innerHTML = '<div id="chat-empty">No messages yet</div>';
    } else {
      for (const entry of data.entries) {
        container.appendChild(renderBubble(entry));
      }
      container.scrollTop = container.scrollHeight;
    }
    renderOrderPanel(data.orders);
    el("thread-status").textContent = "";
  } catch (e) {
    el("thread-status").textContent = e.message;
  }
}

async function loadThreadList() {
  try {
    const threads = await api("/admin/conversations");
    const list = el("thread-list");
    list.innerHTML = "";
    for (const t of threads) {
      const row = document.createElement("div");
      row.className = "thread-row";
      row.dataset.threadId = String(t.thread_id);
      const ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = t.last_active_at ? t.last_active_at.slice(0, 10) : "";
      const phone = document.createElement("div");
      phone.className = "phone";
      phone.textContent = t.phone;
      phone.appendChild(ts);
      const preview = document.createElement("div");
      preview.className = "preview";
      preview.textContent = t.preview || "";
      row.appendChild(phone);
      row.appendChild(preview);
      row.addEventListener("click", () => loadThread(t.thread_id, t.phone));
      list.appendChild(row);
    }
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  }
}

el("refresh-btn").addEventListener("click", async () => {
  await loadThreadList();
  if (currentThreadId !== null) {
    await loadThread(currentThreadId, currentPhone);
  }
});

loadThreadList();
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: PASS — all tests including the two new ones.

- [ ] **Step 6: Manually verify in a browser**

This repo has no automated visual/interaction testing for the admin panel — per this project's own standing instruction, start the app locally (or use the deployed instance with admin credentials) and confirm at `/admin/ui/chats.html`: the three panes render with the WhatsApp-green styling, clicking "Refresh" populates the thread list, clicking a thread loads its bubbles (correct left/right alignment, all four entry types visible) and its order panel (if that customer has orders — a dropdown appears only when there's more than one), and that navigating to this page while logged out redirects to `/admin/ui/`. If you cannot access a browser in your environment, say so explicitly rather than claiming this step was completed — this matches how this exact caveat was already handled for the read-only thread view's own Task 3, whose manual-verification step was honestly reported as unperformed in that round.

- [ ] **Step 7: Run the full suite + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/admin/static/chats.html app/admin/static/chats.js
```
Expected: full suite green, grep empty.

- [ ] **Step 8: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): dedicated WhatsApp-style chat page with order details panel"
```

---

## Post-Implementation

After all three tasks are committed:
- Update `docs/FR/_pipeline_status.md` and `docs/memory/{component_registry,api_registry,error_learnings}.md` per this repo's standing protocol (route to `doc-updater`).
- Route to `code-reviewer`, then `security-reviewer` (this is a presentation-layer change over already-security-reviewed data — confirm the auth boundary on the new page/route is airtight, and that the `orders` field doesn't leak any data beyond what the thread's own resolved phone is entitled to see, same invariant already verified for the base thread endpoint).
- Do NOT push — commits stay local until the owner approves, per this repo's standing rule.
- Give the owner the direct URL (`/admin/ui/chats.html`) once deployed — no link is added from the settings panel, per the owner's own choice during design.
