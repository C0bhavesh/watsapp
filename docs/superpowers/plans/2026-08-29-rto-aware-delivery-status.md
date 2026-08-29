# RTO-aware Delivery Status + Live Tracking Q&A — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every code-writing task is delegated to the `developer` agent per CLAUDE.md.

**Goal:** Stop `order_delivered` firing for RTO (return-to-origin) parcels, and let the order-tracking Q&A agent answer live "where is my parcel / when will it arrive" questions from ad2ship.

**Architecture:** A new stdlib-`re` ad2ship page-parse adapter (`app/shopify/ad2ship.py`) is the shared source of truth for real delivery vs RTO. The `fulfillments/update` webhook's delivered branch stops sending inline and instead records a row in a new `pending_delivery_confirmations` table (due +2h). A new cron job `delivery_confirm` sweeps due rows: it reads ad2ship first (badge `delivered` → send; badge `rto_*` → silent status update, no send), falling back to a Shopify `displayStatus` check only when ad2ship is unreadable. The order-tracking agent gains a cache-gated ad2ship enrichment for in-flight orders, gated by the existing `tracking` reveal permission.

**Tech Stack:** Python 3.12+, FastAPI, asyncpg (Postgres mirror) + in-memory store, httpx, Shopify Admin GraphQL 2026-07, pytest + pytest-asyncio, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-29-rto-aware-delivery-status-design.md` (read it — this plan implements it verbatim).

## Global Constraints

- Python 3.12+, full type hints every signature, `mypy` clean, `ruff` clean. No `print()` — `logging`.
- No new runtime dependency. `backend/pyproject.toml` has no HTML parser; the ad2ship adapter uses stdlib `re`.
- No bare `except`. Wrap every external call (httpx, GraphQL) with an explicit timeout and typed/`None` degrade.
- **PII-free logging:** never log `str(exc)`, the AWB/tracking number, tracking URL, phone, or customer name. Log exception **type** + code location only (mirror `_log_notify_failure` in `app/channels/shopify_webhook.py` and `error_learnings.md` 2026-08-13).
- The LLM never triggers a mutation or a send. The sweep job's send is deterministic (row-driven), gated by the existing `send_mode` kill switch / allowlist via `send_inline_outbound`.
- Never 500 the Shopify webhook ack or exceed its <5s budget. The webhook change is one DB upsert, no network call.
- Both `IngestStore` implementations (`app/store/postgres.py`, `app/store/memory.py`) stay behaviourally identical — every store change lands in both, with matching guards.
- Schema changes are additive + idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), same convention as the rest of `app/store/schema.sql`.
- Conventional commits, one per task minimum. Run `ruff` + `pytest` green before each commit. **Never `git push`** — the owner does that.
- Branch: `feat/rto-aware-delivery-status` (already created; spec + fixtures already committed there).

---

## File Structure

**Create:**
- `backend/app/shopify/ad2ship.py` — `Ad2shipTracking` dataclass + `fetch_tracking(http, awb, *, timeout=4.0) -> Ad2shipTracking | None`. Stdlib `re` parse of the public track page. No Shopify coupling.
- `backend/app/core/delivery_outcome.py` — `fulfillment_is_genuinely_delivered(f: Fulfillment) -> bool` (pure Shopify-fallback rule).
- `backend/app/jobs/delivery_confirm.py` — `run_delivery_confirm(c: Container) -> dict[str, object]` sweep job.
- `backend/tests/test_ad2ship.py`
- `backend/tests/core/test_delivery_outcome.py`
- `backend/tests/test_delivery_confirm_job.py`
- `backend/tests/agents/fixtures/ad2ship/in_transit_<awb>.html` + `malformed.html` (Task 1 saves these; the 4 terminal fixtures already exist on-branch).

**Modify:**
- `backend/app/store/schema.sql` — new table `pending_delivery_confirmations`; 6 new `fulfillments` columns.
- `backend/app/shopify/models.py` — `Fulfillment` gains `display_status`, `events`, `shipment_status` (all defaulted); new frozen `FulfillmentEvent`.
- `backend/app/shopify/client.py` — `FULFILLMENT_FIELDS` gains `displayStatus` + `events{…}`; `_fulfillments_from_node` maps them.
- `backend/app/store/base.py` — `IngestStore` Protocol: new `PendingDeliveryConfirmation` dataclass + 4 methods; `Fulfillment`-carrying reads unaffected (defaults).
- `backend/app/store/postgres.py` — implement the 4 methods; `_FULFILLMENT_COLUMNS` + `_fulfillment_from_row` + `_upsert_fulfillment_on_conn` carry `shipment_status` + `tracking_*`.
- `backend/app/store/memory.py` — same 4 methods + same fulfillment field handling in `InMemoryIngestStore`.
- `backend/app/channels/shopify_webhook.py` — `_notify_fulfillment_events` delivered branch: record pending confirmation instead of send.
- `backend/app/jobs/router.py` — register `"delivery_confirm": run_delivery_confirm` in `JOBS`.
- `backend/app/agents/order_tracking.py` — cache-gated ad2ship enrichment inside the `tracking` reveal block.
- Registry/status docs (Task 8).

---

## Task 1: ad2ship adapter

**Files:**
- Create: `backend/app/shopify/ad2ship.py`
- Create: `backend/tests/test_ad2ship.py`
- Create fixtures: `backend/tests/agents/fixtures/ad2ship/in_transit_<awb>.html`, `.../malformed.html`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Ad2shipTracking:
      status: str            # status-badge class, lowercased; "unknown" if absent
      status_label: str
      current_city: str | None
      current_hub: str | None
      last_scan: str | None
      last_scan_remark: str | None
      last_scan_at: str | None
      expected_date: str | None
      def is_delivered_to_customer(self) -> bool: ...   # status == "delivered"
      def is_rto(self) -> bool: ...                     # status.startswith("rto")
      def is_terminal(self) -> bool: ...                # delivered / rto* / cancelled

  async def fetch_tracking(
      http: httpx.AsyncClient, awb: str, *, timeout: float = 4.0
  ) -> Ad2shipTracking | None
  ```
- Consumes: `httpx.AsyncClient` (from `Container.http`).

**Parse hooks** (verified against the on-branch fixtures — see spec §6.1):
- `status`: first match of `class="status-badge ([a-z_]+)"`, `.lower()`. No match → return `None` (page not a track page).
- `status_label`: text inside that badge element (`>\s*<i[^>]*></i>\s*([^<]+)` after the badge open tag), stripped; `""` if not found.
- First `<div class="history-item">…</div>` block:
  - `last_scan`: `<div class="h-status"><strong>(?:<i[^>]*></i>\s*)?([^<]+)</strong>`
  - `last_scan_remark`: `<div class="h-remarks">([^<]*)</div>`
  - `h-location` inner text `([^<]*)` after the map-marker `<i>` → split on trailing `\s*\(([^)]+)\)\s*$`: group before = `current_hub`, parenthesised = `current_city`. Missing → both `None`.
  - `last_scan_at`: the item's time/date `<span>` text; `None` if absent.
- `expected_date`: a `.date-box` `<span>LABEL</span><strong>VALUE</strong>` whose LABEL matches `re.I` `/(expected|estimated)/`; the `Delivered Date` box does NOT count. `None` if absent.

**Failure posture:** `httpx` error, non-200, body with no `status-badge` match, or any parse exception → return `None`, `logger.warning("ad2ship fetch failed: type=%s", type(exc).__name__)` (no awb/url). Never raises.

- [ ] **Step 1 — capture the two missing fixtures.** From a shell:
  `curl -sS -A "Mozilla/5.0" "https://ad2ship.com/track-order/<an-in-transit-awb>" -o backend/tests/agents/fixtures/ad2ship/in_transit_<awb>.html`
  (find an in-transit AWB from the `fulfillments` mirror whose ad2ship badge is `in_transit`/`out_for_delivery`; the plan executor picks one). Create `malformed.html` = `<!doctype html><html><body><p>no tracking</p></body></html>`.
  Commit fixtures.

- [ ] **Step 2 — write failing tests** (`backend/tests/test_ad2ship.py`). Use a mock `httpx.AsyncClient` via `httpx.MockTransport`. Fixtures loaded with `pathlib`.

```python
import pathlib
import httpx
import pytest
from app.shopify.ad2ship import Ad2shipTracking, fetch_tracking

FIX = pathlib.Path(__file__).parent / "agents" / "fixtures" / "ad2ship"

def _client(html: str | None, status_code: int = 200, exc: type[Exception] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc("boom")
        return httpx.Response(status_code, text=html or "")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))

@pytest.mark.asyncio
async def test_delivered_page_parses_as_customer_delivery():
    html = (FIX / "delivered_tavas4464.html").read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "57143610479732")
    assert t is not None
    assert t.status == "delivered"
    assert t.is_delivered_to_customer() and not t.is_rto()
    assert t.last_scan_remark == "Delivered"
    assert t.current_city == "Maharashtra"

@pytest.mark.asyncio
async def test_rto_page_parses_as_rto():
    html = (FIX / "rto_delivered_tavas3908.html").read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "57143610373752")
    assert t is not None and t.status == "rto_delivered"
    assert t.is_rto() and t.is_terminal() and not t.is_delivered_to_customer()
    assert (t.current_hub or "").startswith("Surat_")
    assert t.expected_date is None  # only a "Delivered Date" box present

@pytest.mark.asyncio
async def test_in_transit_page_is_non_terminal():
    html = next(FIX.glob("in_transit_*.html")).read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "x")
    assert t is not None and not t.is_terminal()

@pytest.mark.asyncio
async def test_malformed_page_returns_none():
    html = (FIX / "malformed.html").read_text(encoding="utf-8")
    async with _client(html) as c:
        assert await fetch_tracking(c, "x") is None

@pytest.mark.asyncio
async def test_http_error_returns_none():
    async with _client(None, status_code=500) as c:
        assert await fetch_tracking(c, "x") is None

@pytest.mark.asyncio
async def test_timeout_returns_none():
    async with _client(None, exc=httpx.TimeoutException) as c:
        assert await fetch_tracking(c, "x") is None
```

- [ ] **Step 3 — run, verify RED:** `pytest backend/tests/test_ad2ship.py -v` → import/parse failures.
- [ ] **Step 4 — implement `backend/app/shopify/ad2ship.py`** per the hooks above. `fetch_tracking` does `await http.get(f"https://ad2ship.com/track-order/{awb}", headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, follow_redirects=False)`; wrap in `try/except (httpx.HTTPError, ValueError)`; `if resp.status_code != 200: return None`; parse; on any parse `Exception` return `None` after the PII-free log.
- [ ] **Step 5 — run, verify GREEN:** `pytest backend/tests/test_ad2ship.py -v`.
- [ ] **Step 6 — `ruff check` + `mypy app/shopify/ad2ship.py`; compliance grep** (no-secrets rule) → EMPTY.
- [ ] **Step 7 — commit:** `feat(tracking): ad2ship page-parse adapter (fetch_tracking)`.

---

## Task 2: Fulfillment model + GraphQL displayStatus/events

**Files:**
- Modify: `backend/app/shopify/models.py`
- Modify: `backend/app/shopify/client.py` (`FULFILLMENT_FIELDS` ~L88-91, `_fulfillments_from_node` ~L141-164)
- Test: `backend/tests/test_models.py`, `backend/tests/test_client_reads.py` (or `test_client_graphql.py`)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class FulfillmentEvent:
      status: str          # e.g. "DELIVERED", "ATTEMPTED_DELIVERY"
      happened_at: str     # raw ISO-8601

  # Fulfillment gains, all defaulted (backward compatible):
  display_status: str | None = None          # Shopify FulfillmentDisplayStatus
  events: tuple[FulfillmentEvent, ...] = ()
  shipment_status: str | None = None         # our normalized mirror column (Task 4 populates)
  ```
- Consumes: nothing new.

- [ ] **Step 1 — failing model test** (`test_models.py`): construct a `Fulfillment` with `display_status="DELIVERED"` and `events=(FulfillmentEvent("DELIVERED","2026-08-29T08:45:37Z"),)`; assert fields round-trip; assert a `Fulfillment(gid=…, status=None, tracking_company=None, tracking_number=None, tracking_url=None)` (old positional/kw call) still constructs with `display_status is None and events == () and shipment_status is None`.
- [ ] **Step 2 — run, verify RED.**
- [ ] **Step 3 — add `FulfillmentEvent` + the 3 fields** to `models.py` with doc comments (match the file's comment density; note `display_status`/`events` are populated only on the GraphQL read path, `shipment_status` only by the mirror).
- [ ] **Step 4 — run, verify GREEN** for model test.
- [ ] **Step 5 — failing client test** (`test_client_reads.py`): feed `_fulfillments_from_node` (or a mocked `_graphql`) a node whose `fulfillments[0]` has `displayStatus: "ATTEMPTED_DELIVERY"` and `events.nodes: [{status:"OUT_FOR_DELIVERY",happenedAt:"…T07:00Z"},{status:"DELIVERED",happenedAt:"…T08:45Z"},{status:"ATTEMPTED_DELIVERY",happenedAt:"…T10:40Z"}]`; assert the parsed `Fulfillment.display_status == "ATTEMPTED_DELIVERY"` and `events` has 3 entries in order.
- [ ] **Step 6 — run, verify RED.**
- [ ] **Step 7 — extend `FULFILLMENT_FIELDS`** to:
  `"fulfillments(first: 5) { id status displayStatus trackingInfo(first: 3) { company number url } createdAt updatedAt deliveredAt events(first: 250, sortKey: HAPPENED_AT) { nodes { status happenedAt } } }"`
  and map `displayStatus` + `events.nodes` in `_fulfillments_from_node` (guard: `node.get("events") or {}`, `.get("nodes") or []`).
- [ ] **Step 8 — run, verify GREEN;** run the full `test_client_reads.py` / `test_client_graphql.py` to confirm existing fulfillment reads unbroken.
- [ ] **Step 9 — `ruff` + `mypy app/shopify/`; commit:** `feat(shopify): read Fulfillment.displayStatus + events`.

---

## Task 3: `fulfillment_is_genuinely_delivered` helper

**Files:**
- Create: `backend/app/core/delivery_outcome.py`
- Create: `backend/tests/core/test_delivery_outcome.py`

**Interfaces:**
- Consumes: `app.shopify.models.Fulfillment`, `FulfillmentEvent`.
- Produces: `def fulfillment_is_genuinely_delivered(f: Fulfillment) -> bool`.

**Rule:** `True` iff `f.display_status == "DELIVERED"` AND `f.events` non-empty AND the `happened_at` of the first event whose `status == "DELIVERED"` equals `max(e.happened_at for e in f.events)` (ISO-8601 strings compare lexically). Else `False`. No exceptions.

- [ ] **Step 1 — failing tests:**

| case | `display_status` | events (`status`@`happened_at`) | expect |
|---|---|---|---|
| clean | `DELIVERED` | OFD@07, DELIVERED@08 | `True` |
| rto-late-scan | `DELIVERED` | DELIVERED@08, ATTEMPTED@10 | `False` |
| stuck | `ATTEMPTED_DELIVERY` | …, ATTEMPTED@10 | `False` |
| no events | `DELIVERED` | () | `False` |
| none | `None` | () | `False` |

- [ ] **Step 2 — run, verify RED.**
- [ ] **Step 3 — implement** (~8 lines, pure).
- [ ] **Step 4 — run, verify GREEN.**
- [ ] **Step 5 — `ruff` + `mypy`; commit:** `feat(core): fulfillment_is_genuinely_delivered (Shopify fallback rule)`.

---

## Task 4: schema + store — pending confirmations & fulfillment status columns

**Files:**
- Modify: `backend/app/store/schema.sql`
- Modify: `backend/app/store/base.py`
- Modify: `backend/app/store/postgres.py` (`_FULFILLMENT_COLUMNS` L149, `_fulfillment_from_row` L155, `_upsert_fulfillment_on_conn` L188)
- Modify: `backend/app/store/memory.py` (`InMemoryIngestStore`)
- Test: `backend/tests/test_ingest_store.py` (in-memory), `backend/tests/test_postgres_store.py` (guarded on `DATABASE_URL`)

**Interfaces:**
- Produces on `IngestStore`:
  ```python
  @dataclass(frozen=True)
  class PendingDeliveryConfirmation:
      fulfillment_gid: str
      order_gid: str
      phone_e164: str
      due_at: datetime
      state: str            # pending | sent | rto | abandoned

  async def record_pending_delivery_confirmation(
      self, *, fulfillment_gid: str, order_gid: str, phone_e164: str, due_at: datetime
  ) -> None: ...                                   # ON CONFLICT (fulfillment_gid) DO NOTHING
  async def due_delivery_confirmations(self, now: datetime, limit: int = 50) -> list[PendingDeliveryConfirmation]: ...
                                                   # state='pending' AND due_at <= now, ORDER BY due_at
  async def set_delivery_confirmation_state(self, fulfillment_gid: str, state: str) -> None: ...
  async def set_fulfillment_shipment_status(
      self, fulfillment_gid: str, shipment_status: str,
      *, tracking_city: str | None = None, tracking_hub: str | None = None,
      last_scan: str | None = None, expected_date: str | None = None,
      checked_at: datetime | None = None,
  ) -> None: ...
  # monotonic: never overwrite a terminal shipment_status ('delivered'|'failure'|'rto') with a
  # non-terminal one; always allowed to (re)write tracking_* + tracking_checked_at.
  ```
- `Fulfillment` reads from the mirror now also carry `shipment_status` (+ the `tracking_*` values are used only by Task 7's agent, surfaced via new optional `Fulfillment` attrs OR a dedicated read — plan executor: add `tracking_checked_at`/`tracking_city`/`tracking_hub`/`tracking_last_scan`/`tracking_expected_date` to `Fulfillment` as defaulted fields, same pattern as Task 2, so one mirror read serves both).

**Schema (append to `schema.sql`, additive/idempotent):**
```sql
CREATE TABLE IF NOT EXISTS pending_delivery_confirmations (
    fulfillment_gid text PRIMARY KEY,
    order_gid       text NOT NULL REFERENCES orders(gid) ON DELETE CASCADE,
    phone_e164      text NOT NULL,
    due_at          timestamptz NOT NULL,
    state           text NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pending_delivery_conf_due
    ON pending_delivery_confirmations (due_at) WHERE state = 'pending';

ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS shipment_status       text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_checked_at    timestamptz;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_city          text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_hub           text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_last_scan     text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_expected_date text;
```

**Postgres `_upsert_fulfillment_on_conn` note:** the existing webhook upsert must NOT wipe the new columns — they are written only by `set_fulfillment_shipment_status`, never by the webhook mirror path. Leave the `INSERT … ON CONFLICT DO UPDATE SET` column list unchanged (do not add the new columns to it); they simply keep their stored values across a webhook re-mirror. Add them to `_FULFILLMENT_COLUMNS` (SELECT list) + `_fulfillment_from_row` only.

- [ ] **Step 1 — failing in-memory tests** (`test_ingest_store.py`, in-memory store, no DB):
  - `record_pending_delivery_confirmation` twice for one `fulfillment_gid` → `due_delivery_confirmations(now+3h)` returns exactly one row with the FIRST `due_at`.
  - a row `due_at = now+2h` is NOT returned by `due_delivery_confirmations(now)`; IS returned by `due_delivery_confirmations(now+2h)`.
  - `set_delivery_confirmation_state(gid, "sent")` → row no longer in `due_*`.
  - `set_fulfillment_shipment_status(gid, "delivered")` then `get_mirrored_order` → that fulfillment's `.shipment_status == "delivered"`.
  - monotonic: after `"rto"`, a later `set_…(gid, "in_transit")` leaves it `"rto"`; but `tracking_city` still updates.
- [ ] **Step 2 — run, verify RED.**
- [ ] **Step 3 — implement in `memory.py`:** add `self._pending_confirmations: dict[str, _PendingRow]` and `self._fulfillment_status: dict[str, dict]` (or extend the stored `Fulfillment` via `replace`), the 4 methods, and surface `shipment_status`/`tracking_*` through `_with_fulfillments`.
- [ ] **Step 4 — run, verify GREEN (in-memory).**
- [ ] **Step 5 — schema + Postgres impl:** edit `schema.sql`; implement the 4 methods in `PostgresIngestStore` (mirror `reconcile`'s asyncpg style); extend `_FULFILLMENT_COLUMNS` + `_fulfillment_from_row`. `record_…` = `INSERT … ON CONFLICT (fulfillment_gid) DO NOTHING`. `set_fulfillment_shipment_status` monotonic via `CASE WHEN fulfillments.shipment_status IN ('delivered','failure','rto') THEN fulfillments.shipment_status ELSE $2 END` plus unconditional `tracking_* = COALESCE($n, fulfillments.tracking_*)`, `tracking_checked_at = COALESCE($n, …)`.
- [ ] **Step 6 — Postgres tests** in `test_postgres_store.py` behind the existing `DATABASE_URL` skip guard, mirroring the in-memory assertions. Run `python -m scripts.apply_schema` against a scratch DB if available; otherwise rely on the in-memory parity tests + a `ruff`/`mypy` pass and note the Postgres path is DB-guarded.
- [ ] **Step 7 — run full store test modules;** `ruff` + `mypy app/store/`.
- [ ] **Step 8 — commit:** `feat(store): pending_delivery_confirmations + fulfillment shipment_status`.

---

## Task 5: webhook delivered branch → record pending confirmation

**Files:**
- Modify: `backend/app/channels/shopify_webhook.py` (`_notify_fulfillment_events` L152-225)
- Test: `backend/tests/test_shopify_webhook.py`

**Interfaces:**
- Consumes: `c.ingest.record_pending_delivery_confirmation(...)` (Task 4), existing `get_mapping_phone` / `normalize_phone(best_phone())` / `get_mirrored_order`.
- Produces: no new symbol; behavioural change only.

**Change:** in `_notify_fulfillment_events`, keep `is_shipped` exactly as-is. Replace the `if is_delivered:` send block with:
1. resolve `to` (mapping phone → normalized best_phone) exactly as the current code does for the shared read;
2. if `to` is None → `logger.info("delivery confirm: no deliverable phone for %s; skipped", order_gid)`, return;
3. `due_at = datetime.now(UTC) + timedelta(hours=2)`;
4. `await c.ingest.record_pending_delivery_confirmation(fulfillment_gid=fulfillment.gid, order_gid=order_gid, phone_e164=to, due_at=due_at)` inside the existing `try/except` that never raises past the boundary (reuse `_log_notify_failure`).
No `order_delivered` send happens here any more. `TEMPLATE_NAME_DELIVERED` stays imported (used by Task 6 via the shared helper) — actually the helper takes the template name as a string arg, so the constant can move to `delivery_confirm.py` or stay; keep it in `shopify_webhook.py` and import it from the job to avoid a magic string (plan executor's call, note it).

- [ ] **Step 1 — failing tests** (`test_shopify_webhook.py`, in-memory container, pattern from existing fulfillment tests):
  - POST a valid-HMAC `fulfillments/update` body with `shipment_status: "delivered"` for a mirrored order → response 200; `c.ingest.due_delivery_confirmations(now + 3h)` has one row for that fulfillment gid with the resolved phone; **zero** `outbound_messages` rows with prefix `fulfillment_delivered:`.
  - Same delivery twice → still exactly one pending row, `due_at` unchanged.
  - `shipment_status: "delivered"` for an order with NO mirror row → 200, no pending row, no crash.
  - A `fulfillments/update` with tracking but `shipment_status` absent → still sends `order_shipped` (unchanged), no pending row.
- [ ] **Step 2 — run, verify RED** (current code sends `order_delivered`, so "zero fulfillment_delivered rows" fails).
- [ ] **Step 3 — implement the branch change.**
- [ ] **Step 4 — run, verify GREEN;** run the whole `test_shopify_webhook.py`.
- [ ] **Step 5 — `ruff` + `mypy`; compliance grep on the file → EMPTY; commit:** `feat(webhook): defer order_delivered — record pending confirmation`.

---

## Task 6: `delivery_confirm` sweep job

**Files:**
- Create: `backend/app/jobs/delivery_confirm.py`
- Modify: `backend/app/jobs/router.py` (`JOBS` dict)
- Create: `backend/tests/test_delivery_confirm_job.py`

**Interfaces:**
- Consumes: `c.ingest.due_delivery_confirmations`, `set_delivery_confirmation_state`, `set_fulfillment_shipment_status`, `get_mirrored_order`; `c.shopify.get_order_fulfillments(order_gid)`; `app.shopify.ad2ship.fetch_tracking`; `app.core.delivery_outcome.fulfillment_is_genuinely_delivered`; `app.channels.shopify_webhook._enqueue_and_send_fulfillment_notification` + `TEMPLATE_NAME_DELIVERED` + `customer_display_name`.
- Produces: `async def run_delivery_confirm(c: Container) -> dict[str, object]`.

**Constants:** `_BATCH_LIMIT = 50`, `_ABANDON_AFTER = timedelta(days=7)`.

**Per due row** (`now = datetime.now(UTC)`):
1. `order = await c.ingest.get_mirrored_order(row.order_gid)`; if `None` → leave `pending`, `errors += 1`, continue.
2. Find the fulfillment in `order.fulfillments` matching `row.fulfillment_gid`; its `tracking_number` is the AWB. If missing → `awb = None`.
3. `t = await ad2ship.fetch_tracking(c.http, awb)` when `awb` else `None`.
4. **Decision:**
   - `t and t.is_delivered_to_customer()` → `_send(...)`; `set_delivery_confirmation_state(gid,"sent")`; `set_fulfillment_shipment_status(gid,"delivered", tracking_city=t.current_city, tracking_hub=t.current_hub, last_scan=t.last_scan, expected_date=t.expected_date, checked_at=now)`; `sent += 1`.
   - `t and t.is_rto()` → `set_delivery_confirmation_state(gid,"rto")`; `set_fulfillment_shipment_status(gid,"rto", …, checked_at=now)`; **no send**; `rto += 1`.
   - else (`t is None` or non-terminal): **fallback** — `fulfs = await c.shopify.get_order_fulfillments(row.order_gid)` (wrap `ShopifyError`/`Exception` → leave `pending`, `errors += 1`, continue); pick the matching gid; `if fulfillment_is_genuinely_delivered(f)` → `_send(...)`; `state="sent"`; `set_fulfillment_shipment_status(gid,"delivered", checked_at=now)`; `sent += 1`. Else → leave `pending` (write `tracking_checked_at`/`tracking_*` from `t` if `t` was non-`None`); `pending += 1`.
5. **Abandon:** before the decision, `if now - row.due_at > _ABANDON_AFTER: set_delivery_confirmation_state(gid,"abandoned"); abandoned += 1; continue`.
6. Each row wrapped in `try/except Exception` → PII-free log (`type(exc).__name__` + `traceback` last frame), `errors += 1`, leave `pending`. One row never fails the run (mirror `reconcile_cancels`).

`_send(c, order_gid, phone, order)` = `await _enqueue_and_send_fulfillment_notification(c, order_gid=order_gid, dedupe_key=f"fulfillment_delivered:{gid}", phone=phone, template=TEMPLATE_NAME_DELIVERED, body_params=[customer_display_name(order), order.name])`.

**Return:** `{"swept": n, "sent": sent, "rto": rto, "pending": pending, "abandoned": abandoned, "errors": errors}`.

**Register:** add `"delivery_confirm": run_delivery_confirm` to `JOBS` in `app/jobs/router.py` and its import.

- [ ] **Step 1 — failing tests** (`test_delivery_confirm_job.py`, pattern from `test_reminders_job.py`: `monkeypatch.setenv("APP_MASTER_KEY")`, `delenv("DATABASE_URL")`, `reset_container`, in-memory store, configure whatsapp + `send_mode`). Monkeypatch `app.jobs.delivery_confirm.fetch_tracking` with an async stub returning a chosen `Ad2shipTracking | None`. Monkeypatch `c.shopify.get_order_fulfillments` with an async stub. Seed a mirrored order + fulfillment (`tracking_number` set) + a due `pending_delivery_confirmations` row (`record_…` with `due_at = now - 1min`).

| test | ad2ship stub | shopify stub | expect |
|---|---|---|---|
| ad2ship delivered | `Ad2shipTracking(status="delivered", …)` | — | 1 `fulfillment_delivered:` outbound row; conf `state` absent from `due_*`; fulfillment `shipment_status=="delivered"`; result `sent==1` |
| ad2ship rto | `status="rto_delivered"` | — | **0** outbound rows; `shipment_status=="rto"`; `rto==1` |
| ad2ship none → shopify delivered | `None` | fulfillment with `display_status="DELIVERED"`, DELIVERED latest | 1 outbound row; `sent==1` |
| ad2ship none → shopify not delivered | `None` | `display_status="ATTEMPTED_DELIVERY"` | 0 outbound; row still `pending` (re-appears in `due_*`); `pending==1` |
| ad2ship non-terminal → shopify not delivered | `status="in_transit"` | `display_status="IN_TRANSIT"` | 0 outbound; row `pending`; `tracking_checked_at` written |
| shopify raises | `None` | raises `ShopifyError` | 0 outbound; row `pending`; `errors==1` |
| abandon | row `due_at = now - 8 days`, `None` | — | `state=="abandoned"`; 0 outbound; `abandoned==1` |
| dedupe | run the "ad2ship delivered" case twice | — | exactly **1** outbound row total |
| kill switch | `send_mode="off"`, ad2ship delivered | — | 0 Meta calls; row not `sent` (or `sent` but suppressed per `send_inline_outbound` semantics — assert no WhatsApp HTTP call) |

- [ ] **Step 2 — run, verify RED.**
- [ ] **Step 3 — implement `delivery_confirm.py` + register in `JOBS`.**
- [ ] **Step 4 — run, verify GREEN;** run `test_jobs_router.py` to confirm the new job is routable and secret-gated.
- [ ] **Step 5 — `ruff` + `mypy app/jobs/`; compliance grep → EMPTY; commit:** `feat(jobs): delivery_confirm sweep — ad2ship-verified order_delivered`.

---

## Task 7: order-tracking agent — live ad2ship enrichment

**Files:**
- Modify: `backend/app/agents/order_tracking.py` (`_order_line` L130-167, `run` L175-207)
- Test: `backend/tests/agents/test_order_tracking.py`

**Interfaces:**
- Consumes: `app.shopify.ad2ship.fetch_tracking`; `Fulfillment.shipment_status` + `tracking_*` (Task 4); `AgentContext` (`.orders`, `.reveal_fields`; add `.http: httpx.AsyncClient` if not present — check; the container has it).
- Produces: a module-level `async def enrich_orders_with_live_tracking(http, orders, reveal_fields, *, now) -> dict[str, Ad2shipTracking]` keyed by `fulfillment_gid`, and `_order_line(order, reveal_fields, live)` gains an optional `live` param.

**Behaviour:**
- `enrich_…` runs at the top of `run()` before building the system prompt. For each `AuthorizedOrder` → each `Fulfillment` where `"tracking" in reveal_fields` AND `f.has_tracking()` AND `f.shipment_status not in ("delivered","rto","failure")`:
  - if `f.tracking_checked_at` is within 30 min of `now` → skip the HTTP call, synthesise an `Ad2shipTracking` from the stored `tracking_*` columns (status from `f.shipment_status`, label best-effort) and use that;
  - else `t = await fetch_tracking(http, f.tracking_number)`; on non-`None` → `await c.ingest.set_fulfillment_shipment_status(f.gid, _normalize(t.status), tracking_city=…, tracking_hub=…, last_scan=…, expected_date=…, checked_at=now)` and include it in the returned dict; on `None` → omit.
  - `_normalize(status)`: map ad2ship badge → our value set (`rto_*`→`rto`, `delivered`→`delivered`, `out_for_delivery`/`in_transit`/`attempted_delivery` pass through, else keep as-is; monotonic guard is in the store).
- `_order_line`: inside the existing `if "tracking" in reveal_fields:` block, after the `_tracking_line` loop, for each fulfillment with an entry in `live`, append (omitting any `None`):
  ```
  "  - Current status: {status_label}"
  "  - Currently at: {hub_or_city}"          # current_hub or current_city
  "  - Latest update: {last_scan_remark or last_scan} ({last_scan_at})"
  "  - Expected delivery: {expected_date}"
  ```
  Same "relay exactly as given, never invent, never compute a date" instruction already in `_SYSTEM_TEMPLATE` covers these — extend that sentence to name the new lines.
- Nothing renders when `live` is empty / `tracking` reveal off / order terminal.

- [ ] **Step 1 — failing tests** (`test_order_tracking.py`; monkeypatch `app.agents.order_tracking.fetch_tracking`):
  - reveal `tracking` on, in-flight fulfillment, stub returns `in_transit` `Ad2shipTracking` → system prompt contains `Current status:` + `Currently at:`; `fetch_tracking` called once.
  - `f.tracking_checked_at = now - 5min` → `fetch_tracking` NOT called (assert 0 calls); stored `tracking_*` still rendered.
  - stub returns `None` → no `Current status:` line; existing `Tracking:` link line still present.
  - `tracking` reveal OFF → no live call, no lines (existing test stays green).
  - `f.shipment_status = "rto"` → no live call; agent renders from stored state.
  - ownership: existing ownership tests unchanged/green.
- [ ] **Step 2 — run, verify RED.**
- [ ] **Step 3 — implement `enrich_orders_with_live_tracking` + wire into `run()` + extend `_order_line` + `_SYSTEM_TEMPLATE` sentence.**
- [ ] **Step 4 — run, verify GREEN;** run the whole `test_order_tracking.py` + `test_order_resolver`-adjacent suites.
- [ ] **Step 5 — `ruff` + `mypy app/agents/order_tracking.py`; commit:** `feat(agent): live ad2ship tracking in order-status answers`.

---

## Task 8: registries, status docs, error-learning

**Files (docs only — `doc-updater` agent, never touches app code):**
- Modify: `docs/memory/api_registry.md`, `docs/memory/component_registry.md`, `docs/FR/_pipeline_status.md`, `docs/architecture-plan.md`, `docs/memory/error_learnings.md`

- [ ] **Step 1** — `api_registry.md`: new job `GET|POST /internal/jobs/delivery_confirm` (return shape, secret-gated); `fulfillments/update` delivered-branch now records `pending_delivery_confirmations` instead of sending; GraphQL `FULFILLMENT_FIELDS` now selects `displayStatus` + `events`.
- [ ] **Step 2** — `component_registry.md`: `app/shopify/ad2ship.py` (`fetch_tracking`, `Ad2shipTracking`); `pending_delivery_confirmations` table; `fulfillments` new columns; `Fulfillment` new fields + `FulfillmentEvent`; `app/core/delivery_outcome.py`; `app/jobs/delivery_confirm.py`; `order_tracking` enrichment.
- [ ] **Step 3** — `_pipeline_status.md`: feature row (branch, spec, plan, phases, DEPLOYED-after-cron note).
- [ ] **Step 4** — `architecture-plan.md`: the deferred-confirmation pattern; live courier tracking (page-parse) now exists for the Q&A path (was "no live courier integration").
- [ ] **Step 5** — `error_learnings.md`: RTO-as-delivered root cause; **Shopify has no reliable RTO signal** (tavas3674/tavas3813 counterexamples); ad2ship `status-badge` is the source of truth.
- [ ] **Step 6 — commit:** `docs: registries + learnings for RTO-aware delivery status`.

---

## Owner action (last stage — cannot be done by Claude)

Add one Vercel Cron entry so the sweep runs. In the **deployed** `vercel.json` (the repo's `backend/vercel.json` currently has no `crons` array — the existing jobs are driven by external schedulers; confirm where `send_reminders` is scheduled and add alongside it):

```json
{ "crons": [
  { "path": "/internal/jobs/delivery_confirm", "schedule": "*/15 * * * *" }
] }
```

Until this is scheduled, `delivered` webhooks accumulate `pending_delivery_confirmations` rows and **no `order_delivered` messages are sent at all**. After scheduling, they send 2h–2h15m after the delivered scan. The endpoint is already testable by hand:
`curl -H "X-Cron-Secret: $CRON_SECRET" -X POST https://<deployment>/internal/jobs/delivery_confirm`

---

## Self-Review (done)

- **Spec coverage:** §4 schema → Task 4; §5.1 webhook → Task 5; §5.2 sweep → Task 6; §5.3 GraphQL → Task 2 + Task 3; §6.1 adapter → Task 1; §6.2 caching + §6.3 agent → Task 7; §7 error handling → folded into each task's failure-path steps; §9 fixtures → Task 1 Step 1; §10 registries → Task 8. D1–D5 all realised (D5 = Task 6 fallback branch).
- **Placeholder scan:** no "TBD/TODO"; each code step names exact files, signatures, SQL, and test assertions. Two executor judgement calls are explicitly flagged (in-transit fixture AWB choice; where `TEMPLATE_NAME_DELIVERED` lives) — both narrow and safe.
- **Type consistency:** `Ad2shipTracking` / `FulfillmentEvent` / `PendingDeliveryConfirmation` field names and the 4 store method signatures are used identically in Tasks 4, 6, 7. `fetch_tracking(http, awb, *, timeout=4.0)` signature consistent across Tasks 1/6/7. `shipment_status` value set (`in_transit|out_for_delivery|attempted_delivery|delivered|failure|rto`) consistent.
