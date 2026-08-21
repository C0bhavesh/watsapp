# Exchange replacement-tracking-url + dd/mm/yyyy dates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second admin-editable tracking-URL field (`replacement_tracking_url`) to exchange requests, and fix the admin panel's date rendering to always show dd/mm/yyyy regardless of the admin's browser locale.

**Architecture:** Mirror the existing `return_tracking_url` field end-to-end (schema column → dataclass field → store Protocol method → memory/Postgres implementations → admin request model/endpoint → `_order_summary` payload → chats.js input box), then fix the single shared `formatBubbleDate` function in chats.js to build the date string manually instead of relying on `toLocaleDateString()`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, asyncpg, pytest + pytest-asyncio, vanilla JS (chats.js, no framework/build step, no JS test runner in this repo).

## Global Constraints

- Full type hints on every function signature; `mypy` clean (python-style.md).
- No bare `except:`; catch specific exceptions (python-style.md) — not touched by this plan, no new exception handling introduced.
- Pydantic v2 models for request/response validation (python-style.md) — `ExchangeUpdateRequest` field addition follows this.
- `ruff` + `pytest` green before committing app code (git-workflow.md).
- Schema changes use the existing idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern already used throughout `schema.sql` — not a manual/blind migration (spec, CLAUDE.md rule 5).
- Both new tracking-URL boxes are always visible/editable regardless of exchange status (owner-confirmed in brainstorming, mirrors existing `return_tracking_url` box).
- Date format fix must not change behavior beyond day/month order — 4-digit year, zero-padded day/month (owner-confirmed: `21/08/2026`, not `21/08/26`).

---

### Task 1: Store layer — `replacement_tracking_url` column, model field, Protocol + both backends

**Files:**
- Modify: `backend/app/store/schema.sql` (after line 284, the closing `);` of the `exchange_requests` table)
- Modify: `backend/app/core/exchange_models.py:25` (add field after `return_tracking_url`)
- Modify: `backend/app/store/base.py:443` (add Protocol method after `set_return_tracking_url`)
- Modify: `backend/app/store/memory.py:964-975` (`create`), `:989-995` (add new method after `set_return_tracking_url`)
- Modify: `backend/app/store/postgres.py:1556-1563` (`_exchange_from_row`), `:1570-1582` (`create`), `:1584-1592` (`list_for_phone`), `:1594-1602` (`get`), `:1611-1617` (add new method after `set_return_tracking_url`)
- Test: `backend/tests/store/test_exchange_store.py`

**Interfaces:**
- Produces: `ExchangeRequest.replacement_tracking_url: str | None`; `ExchangeStore.set_replacement_tracking_url(id: int, url: str) -> None` (both `InMemoryExchangeStore` and `PostgresExchangeStore` implement it, same signature as `set_return_tracking_url`).

- [ ] **Step 1: Write the failing tests**

In `backend/tests/store/test_exchange_store.py`, add after `test_memory_set_return_tracking_url_updates_the_row` (currently ending at line 52):

```python
async def test_memory_set_replacement_tracking_url_updates_the_row() -> None:
    store = InMemoryExchangeStore()
    created = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.set_replacement_tracking_url(created.id, "https://track/repl-abc")
    updated = await store.get(created.id)
    assert updated is not None
    assert updated.replacement_tracking_url == "https://track/repl-abc"
```

Also update the existing `test_memory_create_returns_a_requested_row` (line 12-20) to assert the new field starts `None` too — change:

```python
    assert row.return_tracking_url is None
```

to:

```python
    assert row.return_tracking_url is None
    assert row.replacement_tracking_url is None
```

And add, after `test_pg_set_status_and_return_tracking_url_round_trip` (currently ending at line 90):

```python
@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_set_replacement_tracking_url_round_trip(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    created = await store.create("gid://o/pg4", "tavas9004", "+919000000004", "S")
    await store.set_replacement_tracking_url(created.id, "https://track/repl-pg4")
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.replacement_tracking_url == "https://track/repl-pg4"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/test_exchange_store.py -v`
Expected: FAIL — `AttributeError` on `updated.replacement_tracking_url` / `store.set_replacement_tracking_url` not existing, and `ExchangeRequest(...)` construction errors once the field is referenced.

- [ ] **Step 3: Add the schema column**

In `backend/app/store/schema.sql`, immediately after line 284 (`);` closing the `exchange_requests` table) and before line 285 (`CREATE INDEX IF NOT EXISTS idx_exchange_requests_order ...`), insert:

```sql
-- Tracking link for the replacement item shipped back to the customer, mirroring
-- return_tracking_url (2026-08-21, admin panel replacement-tracking-url field).
ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS replacement_tracking_url text;
```

- [ ] **Step 4: Add the dataclass field**

In `backend/app/core/exchange_models.py`, change:

```python
    return_tracking_url: str | None
    updated_at: str  # raw ISO-8601
```

to:

```python
    return_tracking_url: str | None
    replacement_tracking_url: str | None
    updated_at: str  # raw ISO-8601
```

- [ ] **Step 5: Add the Protocol method**

In `backend/app/store/base.py`, change:

```python
    async def set_return_tracking_url(self, id: int, url: str) -> None: ...
```

to:

```python
    async def set_return_tracking_url(self, id: int, url: str) -> None: ...

    async def set_replacement_tracking_url(self, id: int, url: str) -> None: ...
```

- [ ] **Step 6: Implement in `InMemoryExchangeStore`**

In `backend/app/store/memory.py`, change the `create` method's row construction:

```python
        row = ExchangeRequest(
            id=self._next_id, order_gid=order_gid, order_name=order_name,
            phone_e164=phone_e164, requested_size=requested_size, status="requested",
            requested_at=now, return_tracking_url=None, updated_at=now,
        )
```

to:

```python
        row = ExchangeRequest(
            id=self._next_id, order_gid=order_gid, order_name=order_name,
            phone_e164=phone_e164, requested_size=requested_size, status="requested",
            requested_at=now, return_tracking_url=None, replacement_tracking_url=None,
            updated_at=now,
        )
```

Then add, after `set_return_tracking_url` (lines 989-995):

```python
    async def set_replacement_tracking_url(self, id: int, url: str) -> None:
        row = self._rows.get(id)
        if row is None:
            return
        self._rows[id] = replace(
            row, replacement_tracking_url=url, updated_at=datetime.now(UTC).isoformat()
        )
```

- [ ] **Step 7: Implement in `PostgresExchangeStore`**

In `backend/app/store/postgres.py`, change `_exchange_from_row`:

```python
def _exchange_from_row(row: asyncpg.Record) -> ExchangeRequest:
    return ExchangeRequest(
        id=row["id"], order_gid=row["order_gid"], order_name=row["order_name"],
        phone_e164=row["phone_e164"], requested_size=row["requested_size"],
        status=row["status"], requested_at=row["requested_at"].isoformat(),
        return_tracking_url=row["return_tracking_url"],
        updated_at=row["updated_at"].isoformat(),
    )
```

to:

```python
def _exchange_from_row(row: asyncpg.Record) -> ExchangeRequest:
    return ExchangeRequest(
        id=row["id"], order_gid=row["order_gid"], order_name=row["order_name"],
        phone_e164=row["phone_e164"], requested_size=row["requested_size"],
        status=row["status"], requested_at=row["requested_at"].isoformat(),
        return_tracking_url=row["return_tracking_url"],
        replacement_tracking_url=row["replacement_tracking_url"],
        updated_at=row["updated_at"].isoformat(),
    )
```

Change `create`'s `RETURNING` clause:

```python
                "RETURNING id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, updated_at",
```

to:

```python
                "RETURNING id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, replacement_tracking_url, updated_at",
```

Change `list_for_phone`'s `SELECT` clause:

```python
                "SELECT id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, updated_at "
                "FROM exchange_requests WHERE phone_e164 = $1 ORDER BY requested_at DESC",
```

to:

```python
                "SELECT id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, replacement_tracking_url, updated_at "
                "FROM exchange_requests WHERE phone_e164 = $1 ORDER BY requested_at DESC",
```

Change `get`'s `SELECT` clause:

```python
                "SELECT id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, updated_at "
                "FROM exchange_requests WHERE id = $1",
```

to:

```python
                "SELECT id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, replacement_tracking_url, updated_at "
                "FROM exchange_requests WHERE id = $1",
```

Then add, after `set_return_tracking_url` (lines 1611-1617):

```python
    async def set_replacement_tracking_url(self, id: int, url: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE exchange_requests SET replacement_tracking_url = $1, updated_at = now() "
                "WHERE id = $2",
                url, id,
            )
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/test_exchange_store.py -v`
Expected: PASS (Postgres-backed tests skip unless `TEST_DATABASE_URL` is set — same as today).

- [ ] **Step 9: Type-check and lint**

Run: `cd backend && python -m mypy app/store/base.py app/store/memory.py app/store/postgres.py app/core/exchange_models.py && python -m ruff check app/store/base.py app/store/memory.py app/store/postgres.py app/core/exchange_models.py`
Expected: no errors.

- [ ] **Step 10: Compliance grep (no-secrets.md)**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/store/schema.sql backend/app/store/base.py backend/app/store/memory.py backend/app/store/postgres.py backend/app/core/exchange_models.py`
Expected: empty output.

- [ ] **Step 11: Commit**

```bash
git add backend/app/store/schema.sql backend/app/core/exchange_models.py backend/app/store/base.py backend/app/store/memory.py backend/app/store/postgres.py backend/tests/store/test_exchange_store.py
git commit -m "feat(store): add replacement_tracking_url to exchange requests"
```

---

### Task 2: Admin router — request model, endpoint, `_order_summary` payload

**Files:**
- Modify: `backend/app/admin/router.py:566-573` (`ExchangeUpdateRequest`), `:807-865` (`_order_summary`), `:954-975` (`update_exchange`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `ExchangeRequest.replacement_tracking_url` (Task 1), `ExchangeStore.set_replacement_tracking_url(id, url)` (Task 1).
- Produces: `ExchangeUpdateRequest.replacement_tracking_url: str | None`; `_order_summary(...)["exchange"]["replacement_tracking_url"]` in the JSON payload returned by `GET /admin/conversations/{thread_id}`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/admin/test_views.py`, update `test_conversation_thread_includes_exchange_details_when_a_request_exists` (lines 671-674) — change:

```python
    assert orders[0]["exchange"] == {
        "id": created.id, "requested_size": "M", "status": "requested",
        "requested_at": created.requested_at, "return_tracking_url": None,
    }
```

to:

```python
    assert orders[0]["exchange"] == {
        "id": created.id, "requested_size": "M", "status": "requested",
        "requested_at": created.requested_at, "return_tracking_url": None,
        "replacement_tracking_url": None,
    }
```

Then add, after `test_update_exchange_sets_return_tracking_url` (currently ending at line 780):

```python
def test_update_exchange_sets_replacement_tracking_url(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/4", "tavas4", "+919999999999", "XL")
    )
    resp = client.post(
        f"/admin/exchanges/{created.id}",
        json={"replacement_tracking_url": "https://track/repl-xyz"},
    )
    assert resp.status_code == 200
    updated = asyncio.run(get_container().exchanges.get(created.id))
    assert updated is not None
    assert updated.replacement_tracking_url == "https://track/repl-xyz"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k exchange -v`
Expected: FAIL — the dict-equality assertion fails (missing key), and the new test fails with a 422 (unknown field rejected... actually Pydantic ignores unknown fields by default, so it will 200 but `updated.replacement_tracking_url` stays `None`, failing the final assertion) or an `AttributeError` if Task 1 isn't merged yet in this run.

- [ ] **Step 3: Add the request field**

In `backend/app/admin/router.py`, change:

```python
class ExchangeUpdateRequest(BaseModel):
    """Admin-driven advance of an exchange request's status and/or return-tracking link.

    Both fields optional -- a single call can set either, both, or (rejected below) neither.
    """

    status: ExchangeStatus | None = None
    return_tracking_url: str | None = Field(default=None, max_length=2048)
```

to:

```python
class ExchangeUpdateRequest(BaseModel):
    """Admin-driven advance of an exchange request's status and/or tracking links.

    All fields optional -- a single call can set any combination, or (rejected below) none.
    """

    status: ExchangeStatus | None = None
    return_tracking_url: str | None = Field(default=None, max_length=2048)
    replacement_tracking_url: str | None = Field(default=None, max_length=2048)
```

- [ ] **Step 4: Wire the endpoint**

In `backend/app/admin/router.py`, change:

```python
    if body.return_tracking_url is not None:
        await c.exchanges.set_return_tracking_url(exchange_id, body.return_tracking_url)
    _audit("exchange_update", "success", resource=f"exchange:{exchange_id}")
```

to:

```python
    if body.return_tracking_url is not None:
        await c.exchanges.set_return_tracking_url(exchange_id, body.return_tracking_url)
    if body.replacement_tracking_url is not None:
        await c.exchanges.set_replacement_tracking_url(exchange_id, body.replacement_tracking_url)
    _audit("exchange_update", "success", resource=f"exchange:{exchange_id}")
```

- [ ] **Step 5: Add the field to `_order_summary`**

In `backend/app/admin/router.py`, change:

```python
    if exchange is not None:
        summary["exchange"] = {
            "id": exchange.id,
            "requested_size": exchange.requested_size,
            "status": exchange.status,
            "requested_at": exchange.requested_at,
            "return_tracking_url": exchange.return_tracking_url,
        }
```

to:

```python
    if exchange is not None:
        summary["exchange"] = {
            "id": exchange.id,
            "requested_size": exchange.requested_size,
            "status": exchange.status,
            "requested_at": exchange.requested_at,
            "return_tracking_url": exchange.return_tracking_url,
            "replacement_tracking_url": exchange.replacement_tracking_url,
        }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k exchange -v`
Expected: PASS.

- [ ] **Step 7: Run the full backend suite**

Run: `cd backend && python -m pytest -v`
Expected: PASS (no regressions elsewhere).

- [ ] **Step 8: Type-check and lint**

Run: `cd backend && python -m mypy app/admin/router.py && python -m ruff check app/admin/router.py`
Expected: no errors.

- [ ] **Step 9: Compliance grep (no-secrets.md)**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/admin/router.py`
Expected: empty output.

- [ ] **Step 10: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): expose replacement_tracking_url on the exchange-update endpoint"
```

---

### Task 3: Frontend — second input box + dd/mm/yyyy date format

**Files:**
- Modify: `backend/app/admin/static/chats.js:66-71` (`formatBubbleDate`), `:451-491` (`renderExchangeDetail`'s tracking input + Save handler)

**Interfaces:**
- Consumes: `order.exchange.replacement_tracking_url` (Task 2's `_order_summary` payload), `POST /admin/exchanges/{id}` body field `replacement_tracking_url` (Task 2).

No automated test exists for this file (confirmed: no `*chats*test*` file in the repo) — verification is by reading the diff and, if the dev server is reachable, exercising the panel manually. This deviates from strict TDD only because no JS test harness exists in this codebase; the equivalent behavior is already covered end-to-end by Task 1/2's Python tests plus manual verification here.

- [ ] **Step 1: Fix `formatBubbleDate` to build dd/mm/yyyy explicitly**

In `backend/app/admin/static/chats.js`, change:

```javascript
function formatBubbleDate(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString();
}
```

to:

```javascript
function formatBubbleDate(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  const day = String(d.getDate()).padStart(2, "0");
  const month = String(d.getMonth() + 1).padStart(2, "0");
  return day + "/" + month + "/" + d.getFullYear();
}
```

- [ ] **Step 2: Add the replacement-tracking-url input box**

In `backend/app/admin/static/chats.js`, change:

```javascript
  const trackingInput = document.createElement("input");
  trackingInput.type = "text";
  trackingInput.className = "exchange-tracking-input";
  trackingInput.placeholder = "Return tracking URL";
  trackingInput.value = order.exchange.return_tracking_url || "";
  container.appendChild(trackingInput);

  const saveBtn = document.createElement("button");
```

to:

```javascript
  const trackingInput = document.createElement("input");
  trackingInput.type = "text";
  trackingInput.className = "exchange-tracking-input";
  trackingInput.placeholder = "Return tracking URL";
  trackingInput.value = order.exchange.return_tracking_url || "";
  container.appendChild(trackingInput);

  const replacementTrackingInput = document.createElement("input");
  replacementTrackingInput.type = "text";
  replacementTrackingInput.className = "exchange-tracking-input";
  replacementTrackingInput.placeholder = "Replacement order tracking URL";
  replacementTrackingInput.value = order.exchange.replacement_tracking_url || "";
  container.appendChild(replacementTrackingInput);

  const saveBtn = document.createElement("button");
```

- [ ] **Step 3: Include the new field in the Save POST body**

In `backend/app/admin/static/chats.js`, change:

```javascript
      await api("/admin/exchanges/" + encodeURIComponent(order.exchange.id), "POST", {
        status: statusSelect.value,
        return_tracking_url: trackingInput.value || null,
      });
```

to:

```javascript
      await api("/admin/exchanges/" + encodeURIComponent(order.exchange.id), "POST", {
        status: statusSelect.value,
        return_tracking_url: trackingInput.value || null,
        replacement_tracking_url: replacementTrackingInput.value || null,
      });
```

- [ ] **Step 4: Compliance grep (no-secrets.md)**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/admin/static/chats.js`
Expected: empty output.

- [ ] **Step 5: Manual verification**

If the dev server is reachable, open an admin thread with an existing exchange request and confirm: two tracking-URL boxes render, "Requested on" shows dd/mm/yyyy, chat date dividers show dd/mm/yyyy, and Save persists both URLs (re-open the thread and confirm both values reload). If the dev server isn't reachable in this environment, state that explicitly instead of claiming it was tested.

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/static/chats.js
git commit -m "feat(admin-ui): add replacement tracking URL box and fix dates to dd/mm/yyyy"
```

---

## Self-Review Notes

- Spec coverage: field addition (schema/model/store/router/UI) → Tasks 1-3; date format fix → Task 3 Step 1. Both spec items covered.
- No placeholders: every step shows exact before/after code or an exact runnable command.
- Type consistency checked: `replacement_tracking_url: str | None` used identically in `exchange_models.py`, `base.py`, `memory.py`, `postgres.py`, `router.py`'s `ExchangeUpdateRequest`, and `_order_summary`'s dict key; JS variable `replacementTrackingInput` used consistently across Task 3 Steps 2-3.
