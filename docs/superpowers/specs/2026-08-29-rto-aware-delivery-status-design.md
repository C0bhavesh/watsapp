# RTO-aware delivery status + live tracking Q&A — Design

- **Date:** 2026-08-29
- **Owner decision-maker:** Bhavesh (sole; no separate client gate)
- **Status:** Approved for planning
- **Trigger:** `bug::` — bot sent `order_delivered` for order `tavas3908` when the parcel was an RTO
  (Return To Origin — delivered *back to the warehouse*, not to the customer).

---

## 1. Problem & root cause (confirmed)

`_notify_fulfillment_events` in `backend/app/channels/shopify_webhook.py:170` decides "delivered" from a
single field on the raw `fulfillments/update` webhook body:

```python
is_delivered = raw_payload.get("shipment_status") == "delivered"
```

Delhivery stamps the **final RTO scan** (parcel delivered back to origin) with the same
`shipment_status == "delivered"`. Nothing in the codebase distinguishes a customer delivery from an
RTO delivery (`grep -i rto backend/app/` → 0 hits). So `order_delivered` fired for `tavas3908`.

### Evidence gathered (live Shopify GraphQL, 2026-08-29)

`tavas3908` fulfillment `gid://shopify/Fulfillment/6964400161136`:

| Field | Value |
|---|---|
| `displayStatus` | `ATTEMPTED_DELIVERY` |
| `deliveredAt` | `2026-08-29T08:45:37Z` (set — **unreliable**, RTO origin-delivery timestamp) |
| events (tail) | `… IN_TRANSIT (08-20) → DELIVERED (08-29 08:45:37) → ATTEMPTED_DELIVERY (08-29 10:40:11)` |
| every event `message` | `null` |

Six genuine deliveries from the same day, for comparison:

| Order | `displayStatus` | last event | event after `DELIVERED`? |
|---|---|---|---|
| tavas3674 | `DELIVERED` | `DELIVERED` | no |
| tavas3813 | `DELIVERED` | `DELIVERED` | no |
| tavas3953 | `DELIVERED` | `DELIVERED` | no |
| tavas4026 | `DELIVERED` | `DELIVERED` | no |
| tavas4464 | `DELIVERED` | `DELIVERED` | no |
| tavas4433 | `DELIVERED` | `DELIVERED` | no |

### What this rules out

- **Free-text / keyword matching is impossible.** Every `FulfillmentEvent.message` is `null`;
  Delhivery/ad2ship pushes zero text into Shopify.
- **Attempt count / `IN_TRANSIT` re-entry is not a signal.** tavas3674 had 3 failed attempts and
  re-entered `IN_TRANSIT`, identical shape to the RTO — but was genuinely delivered.
- **`deliveredAt` is not a signal.** Set on the RTO too.
- Shopify enums (`shipment_status`, `FulfillmentDisplayStatus`, `FulfillmentEventStatus`) have **no
  RTO value** — the RTO scan can only land as `DELIVERED` / `ATTEMPTED_DELIVERY`.

### The reliable signal

`Fulfillment.displayStatus` (Shopify's own derived rollup — GraphQL only, **not** on the REST
webhook). A genuine delivery ⇒ `displayStatus == "DELIVERED"` **and** the `DELIVERED` event is the
latest event. The RTO ⇒ Shopify refused to promote `displayStatus` past `ATTEMPTED_DELIVERY` because
a later scan superseded the `DELIVERED` one.

The signal appears **late**: for `tavas3908` the distinguishing scan arrived ~1h54m after the false
`delivered`. A synchronous check at webhook time cannot catch it — hence the deferred re-check below.

---

## 2. Scope

Two related changes, one spec, two build phases. They share the ad2ship adapter and the new
`fulfillments.shipment_status` column.

**Phase A — Delivered-notification fix.** `order_delivered` is confirmed by a deferred Shopify
`displayStatus` re-check instead of firing off the raw webhook string.

**Phase B — Live tracking Q&A.** A new ad2ship adapter parses the public track page; the
order-tracking agent relays live status / location / expected date for a shipped, non-delivered
order when the store's `tracking` reveal is on.

### Out of scope

- Address-change flow, human handoff, ad2ship **API** integration (page-parse only).
- Order creation, payment logic, WhatsApp inbound handling, webhook HMAC verification, RLS/security,
  unrelated admin functionality.
- Any new Meta-approved template. RTO is a **silent status update** — no customer message, no owner
  alert.
- `order_shipped` notification (unchanged — it keys off tracking presence, not delivery).

---

## 3. Owner decisions (locked)

| # | Decision |
|---|---|
| D1 | ad2ship data source: **parse the public track page** (`ad2ship.com/track-order/<awb>`). No API. Isolated in one adapter. |
| D2 | Deferred confirmation: **single re-check, 2h** after the `delivered` event. |
| D3 | RTO / not-delivered outcome: **silent status update only** — write `shipment_status`, send nothing. |
| D4 | Live tracking details gated **under the existing `tracking` reveal permission** — no new toggle. Ownership check always applies. |

**Residual risk accepted (D2):** an RTO correction scan arriving later than ~2h15m after the
`delivered` scan would still let a wrong `order_delivered` through. A second re-check at +12h would
close almost all of it; not built now, recorded here.

---

## 4. Data model changes

### 4.1 New table `pending_delivery_confirmations`

```sql
CREATE TABLE IF NOT EXISTS pending_delivery_confirmations (
    fulfillment_gid text PRIMARY KEY,
    order_gid       text NOT NULL REFERENCES orders(gid) ON DELETE CASCADE,
    phone_e164      text NOT NULL,               -- resolved at record time (same routing as send)
    due_at          timestamptz NOT NULL,        -- delivered-event time + 2h
    state           text NOT NULL DEFAULT 'pending',  -- pending | sent | rto | abandoned
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pending_delivery_conf_due
    ON pending_delivery_confirmations (due_at) WHERE state = 'pending';
```

Additive + idempotent (`CREATE … IF NOT EXISTS`), same migration convention as the rest of
`schema.sql`.

### 4.2 New columns on `fulfillments`

```sql
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS shipment_status       text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_checked_at    timestamptz;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_city          text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_hub           text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_last_scan     text;
ALTER TABLE fulfillments ADD COLUMN IF NOT EXISTS tracking_expected_date text;
```

`shipment_status` normalized value set (lowercase), in progression order:
`in_transit` → `out_for_delivery` → `attempted_delivery` → (`delivered` | `failure` | `rto`).
`delivered`, `failure`, `rto` are **terminal**.
`rto` = a `DELIVERED` event exists but is not the latest event, **or** ad2ship reports `rto_*`.

Writers:
- **Phase A sweep job** — authoritative for `delivered` vs `rto` (from GraphQL `displayStatus` +
  event ordering).
- **Phase B ad2ship fetch** — may refine `shipment_status` and fills the `tracking_*` columns.
  Monotonic: it only advances along the progression above and never overwrites a terminal value
  (once `delivered`/`failure`/`rto`, it stays).

---

## 5. Phase A — Delivered-notification fix

### 5.1 Webhook path change — `backend/app/channels/shopify_webhook.py`

`_notify_fulfillment_events` (`:152-225`): the `is_delivered` branch **no longer** calls
`_enqueue_and_send_fulfillment_notification`. It instead:

1. resolves the recipient phone exactly as today (`get_mapping_phone` → `normalize_phone(best_phone())`),
2. upserts a `pending_delivery_confirmations` row with `due_at = now(UTC) + 2h`,
   `ON CONFLICT (fulfillment_gid) DO NOTHING` (a replayed webhook must not reset the clock),
3. keeps the same never-raise / never-500 / PII-free-log posture.

`is_shipped` branch and `order_shipped` send are untouched.

### 5.2 New sweep job — `backend/app/jobs/delivery_confirm.py`

Registered in `app/jobs/router.py` `JOBS` as `"delivery_confirm": run_delivery_confirm`.

Per run:

- `SELECT … WHERE state = 'pending' AND due_at <= now() ORDER BY due_at LIMIT <batch cap>`
  (batch cap consistent with the other jobs).
- For each row, GraphQL-read the fulfillment (see 5.3):
  - **Genuine delivery** — `displayStatus == "DELIVERED"` AND no event `happenedAt` is later than
    the `DELIVERED` event's:
    → `send_inline_outbound` the `order_delivered` template via the existing
      `_enqueue_and_send_fulfillment_notification` path (dedupe key `fulfillment_delivered:{gid}`,
      body params `[name, order.name]`),
    → `pending_delivery_confirmations.state = 'sent'`,
    → `fulfillments.shipment_status = 'delivered'`.
  - **Not delivered** — anything else:
    → `state = 'rto'`,
    → `fulfillments.shipment_status = 'rto'` if a `DELIVERED` event exists but isn't latest, else the
      lowercased `displayStatus`,
    → **no send.**
  - **Shopify / GraphQL error** — leave the row `pending` (retried next run), log exception type +
    location only (mirrors `reconcile_cancels`).
  - **Abandon horizon** — `now() > due_at + 7 days` and still unresolved → `state = 'abandoned'`,
    stop retrying.
- Return `{"swept", "sent", "rto", "pending", "abandoned", "errors"}`.

Idempotency: the `outbound_messages` `dedupe_key UNIQUE` on `fulfillment_delivered:{gid}` is the real
guard — double-processing a row cannot double-send.

### 5.3 GraphQL read extension — `backend/app/shopify/client.py`

`FULFILLMENT_FIELDS` (`:88-91`) gains `displayStatus` and
`events(first: 250, sortKey: HAPPENED_AT) { nodes { status happenedAt } }`.
`_fulfillments_from_node` maps them onto the `Fulfillment` model (new fields
`display_status: str | None`, `events: tuple[FulfillmentEvent, ...]` where
`FulfillmentEvent` = `(status: str, happened_at: str)`). Existing callers ignore the new fields; the
model stays backward-compatible. This read already runs isolated from `ORDER_FIELDS` (the
`read_fulfillments`-scope safety split), so no blast radius on `get_order` / Confirm / Cancel.

Helper `fulfillment_is_genuinely_delivered(f: Fulfillment) -> bool` lives in `app/core/` (pure,
unit-tested): `f.display_status == "DELIVERED" and max(e.happened_at for e in f.events) == <the
DELIVERED event's happened_at>`.

### 5.4 Scheduling

Owner adds one cron entry to the deployed `vercel.json`:

```json
{ "path": "/internal/jobs/delivery_confirm", "schedule": "*/15 * * * *" }
```

Same mechanism as `send_reminders`. Net effect: `order_delivered` lands 2h–2h15m after the delivered
scan for a clean delivery. **This is a deploy-config change — owner applies it, not Claude.**

---

## 6. Phase B — Live tracking Q&A

### 6.1 New adapter — `backend/app/shopify/ad2ship.py`

Placed under `app/shopify/` for proximity to the tracking/fulfillment code where the AWB and
`trackingInfo` already live. It is a standalone HTTP adapter with **no Shopify coupling** — a single
`fetch_tracking` function behind which the page-parse detail is fully contained (a future ad2ship API
backing would replace the internals without touching callers).

```python
@dataclass(frozen=True)
class Ad2shipTracking:
    status: str            # in_transit | out_for_delivery | attempted_delivery | delivered
                           # | rto_in_transit | rto_delivered | unknown
    status_label: str      # human text as rendered, e.g. "Out for Delivery"
    current_city: str | None
    current_hub: str | None
    last_scan: str | None       # latest history line text + location
    last_scan_at: str | None
    expected_date: str | None   # only if ad2ship renders one

async def fetch_tracking(
    http: httpx.AsyncClient, awb: str, *, timeout: float = 4.0
) -> Ad2shipTracking | None
```

- `GET https://ad2ship.com/track-order/{awb}`, browser-like `User-Agent`, `follow_redirects=False`.
- Parses stable hooks: the `status-badge <class>` token → `status`; the courier/AWB block; the first
  `.history-item`; the "Expected"/"Estimated" date node if present.
- Returns `None` on **any** failure: timeout, non-200, missing expected nodes, unparseable. Never
  raises. Logs exception type only (no AWB, no URL — matches the tracking-number no-log rule).
- No secrets, no credentials.

### 6.2 Caching

- `fulfillments.tracking_checked_at` gates the live call: fetch only if null or `> 30 min` old;
  otherwise reuse the persisted `tracking_*` + `shipment_status` values.
- A successful fetch writes `shipment_status` (monotonic toward terminal), `tracking_checked_at`,
  `tracking_city`, `tracking_hub`, `tracking_last_scan`, `tracking_expected_date` onto the mirror
  row. This lets a customer-driven `rto_delivered` finding update status between sweep runs.

### 6.3 Order-tracking agent — `backend/app/agents/order_tracking.py`

Inside the existing `"tracking" in reveal_fields` block of `_render_order`, for a fulfillment that
`has_tracking()` and whose `shipment_status` is not `delivered`/`rto`:

1. resolve the AWB from `fulfillment.tracking_number`,
2. get tracking data via the 30-min cache rule (6.2),
3. if data present, append prompt lines under the same "relay exactly, never invent, never compute"
   instruction that already governs the estimated-delivery line:
   - `Current status: <status_label>`
   - `Currently at: <current_city / current_hub>` (omit if both null)
   - `Latest update: <last_scan> (<last_scan_at>)` (omit if null)
   - `Expected delivery: <expected_date>` (omit if null)
4. if `None`: append nothing — the existing tracking-link text + "offer to have the team check"
   remains the fallback.

Ownership check and reveal gating are unchanged — these lines only render after the existing
phone-ownership check and only inside the existing `tracking` gate. For a `delivered` order the agent
answers from stored state with no live fetch (terminal status).

---

## 7. Error handling (system-wide posture)

Every new external call is bounded and fail-soft:

| Call | On failure |
|---|---|
| Phase A sweep GraphQL read | row stays `pending`, retried next run; `errors` counter++ |
| Phase A `order_delivered` send | existing `send_inline_outbound` isolation — row not marked `sent`, re-attempted |
| Phase B `ad2ship.fetch_tracking` | returns `None`; agent uses existing fallback text |

No new code path can 500 a webhook or block the Shopify `<5s` ack (the webhook change only writes one
row). All logging is PII-free: exception type + code location, never `str(exc)`, never the tracking
number / URL / phone — matching `_log_notify_failure` and the 2026-08-13 error-learning.

`send_mode` kill switch, allowlist, and shadow mode apply unchanged — the sweep job sends through the
same `send_inline_outbound` path as every other outbound.

---

## 8. Edge cases

- **Split shipments** — one `pending_delivery_confirmations` row and one `shipment_status` per
  fulfillment gid; `order_delivered` already dedupes per fulfillment gid.
- **2h window tight** — see §3 residual risk.
- **Genuine delivery + stray later scan** — we withhold `order_delivered` (false negative). Safe
  direction; customer can still ask. Not seen in the 6-order sample.
- **`pending` row never resolves** — abandoned after `due_at + 7 days`.
- **AWB missing from `trackingInfo`** — Phase B skips the fetch, uses fallback text.
- **Customer asks about a `delivered` / `rto` order** — no live fetch; answer from stored state.
- **Webhook replay of the `delivered` event** — `ON CONFLICT DO NOTHING` keeps the original
  `due_at`.
- **ad2ship page redesign** — `fetch_tracking` returns `None`; Phase B degrades to today's behavior;
  Phase A is unaffected (it uses Shopify, not ad2ship).

---

## 9. Testing (TDD — pytest + pytest-asyncio, RED→GREEN)

### Phase A
- `fulfillment_from_webhook_payload` / webhook: a `delivered` payload writes a
  `pending_delivery_confirmations` row and sends **nothing**.
- Webhook replay: second `delivered` delivery does not change `due_at`.
- `fulfillment_is_genuinely_delivered`: unit cases — DELIVERED & latest → True; DELIVERED not latest
  → False; displayStatus ATTEMPTED_DELIVERY → False; no events → False.
- `run_delivery_confirm` sweep, fixture fulfillments:
  (a) clean delivery → `order_delivered` sent once, row `sent`, `shipment_status='delivered'`;
  (b) `displayStatus=ATTEMPTED_DELIVERY` → row `rto`, no send, `shipment_status='attempted_delivery'`;
  (c) `DELIVERED` present but not latest → row `rto`, `shipment_status='rto'`;
  (d) Shopify error → row stays `pending`;
  (e) past abandon horizon → row `abandoned`.
- Two sweep passes over the same due row → exactly one `order_delivered` (dedupe-key guard).
- `send_mode = off` → sweep marks nothing `sent`, zero Meta calls.

### Phase B
- `ad2ship.fetch_tracking` against saved page fixtures: a normal in-transit page → populated
  dataclass; an `rto_delivered` page → `status='rto_delivered'`; a truncated / renamed-structure
  page → `None`; a timeout → `None`.
- Cache rule: `tracking_checked_at` within 30 min → no HTTP call made (mock asserts zero requests).
- `_render_order`: live data present → the four extra lines rendered; `None` → only fallback text;
  `tracking` reveal off → no tracking lines at all (existing behavior); `delivered` order → no live
  fetch.
- Ownership: existing order-tracking ownership tests stay green.

---

## 10. Registry / doc updates (post-REVIEW, `doc-updater`)

- `docs/memory/api_registry.md` — new job `delivery_confirm`; webhook `fulfillments/update`
  delivered-branch behavior change.
- `docs/memory/component_registry.md` — `pending_delivery_confirmations`, `fulfillments` new
  columns, `ad2ship` adapter, `Fulfillment` model new fields, `order_tracking` agent enrichment.
- `docs/FR/_pipeline_status.md` — feature row.
- `docs/architecture-plan.md` — note the deferred-confirmation pattern and that live courier
  tracking (page-parse) now exists for the Q&A path (previously "no live courier integration").
- `docs/memory/error_learnings.md` — the RTO-as-delivered root cause + the "trust `displayStatus`,
  not the webhook `shipment_status` string" rule.
