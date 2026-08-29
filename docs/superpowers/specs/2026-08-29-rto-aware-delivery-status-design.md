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

Five more orders from the same day, cross-checked against their ad2ship track pages:

| Order | Shopify `displayStatus` | Shopify last event | ad2ship badge | ad2ship history top | Truth |
|---|---|---|---|---|---|
| tavas3908 | `ATTEMPTED_DELIVERY` | `ATTEMPTED_DELIVERY` | `rto_delivered` | RETURN ACCEPTED / Rto Delivered (Surat, origin) | **RTO** |
| tavas3674 | `DELIVERED` | `DELIVERED` | `rto_delivered` | RETURN ACCEPTED / Rto Delivered (Surat, origin) | **RTO** |
| tavas3813 | `DELIVERED` | `DELIVERED` | `rto_delivered` | RETURN ACCEPTED / Rto Delivered (Surat, origin) | **RTO** |
| tavas4464 | `DELIVERED` | `DELIVERED` | `delivered` | DELIVERED TO CONSIGNEE - CODE VERIFIED (Mumbai) | delivered |

### What this rules out — including the first design's own premise

- **Shopify `displayStatus` is NOT a reliable RTO discriminator.** tavas3674 and tavas3813 are RTOs
  that Shopify shows as `displayStatus == "DELIVERED"` with `DELIVERED` as the latest event —
  indistinguishable from tavas4464, a genuine delivery. tavas3908 was only caught because Delhivery
  happened to push a later `ATTEMPTED_DELIVERY` scan; that was luck, not signal. A rule of
  "`displayStatus == DELIVERED` and it is the latest event" would send `order_delivered` for
  tavas3674 and tavas3813.
- **Free-text / keyword matching is impossible.** Every Shopify `FulfillmentEvent.message` is
  `null`; Delhivery/ad2ship pushes zero text into Shopify.
- **Attempt count / `IN_TRANSIT` re-entry is not a signal.** Genuine deliveries in the sample also
  had 2–3 failed attempts and re-entered `IN_TRANSIT`.
- **`deliveredAt` is not a signal.** Set on the RTOs too.
- Shopify enums (`shipment_status`, `FulfillmentDisplayStatus`, `FulfillmentEventStatus`) have **no
  RTO value** — the RTO scan can only land as `DELIVERED` / `ATTEMPTED_DELIVERY`.

### The reliable signal — ad2ship

Only ad2ship's own track page cleanly separates the two:

| | Genuine delivery | RTO |
|---|---|---|
| `status-badge` class | `delivered` | `rto_delivered` / `rto_in_transit` |
| history top status | `DELIVERED TO CONSIGNEE …` | `RETURN ACCEPTED - DL-RTO-RD-AC` |
| history top remark | `Delivered` | `Rto Delivered` / `Rto In Transit` |
| location | customer's city | origin hub (e.g. `Surat_Laldarwaja_R`) |

So the deferred re-check keys off **ad2ship**, with Shopify `displayStatus` kept only as a
degraded fallback when ad2ship cannot be read (owner decision D5). The re-check is still deferred
(not synchronous) because ad2ship can lag the Shopify webhook by minutes, and a `pending` row that
retries is the clean way to absorb that.

---

## 2. Scope

Two related changes, one spec, two build phases. The **ad2ship adapter is shared** and is built
first (Phase A depends on it); the new `fulfillments.shipment_status` column is shared too.

**Phase A — Delivered-notification fix.** `order_delivered` is confirmed by a deferred re-check that
reads **ad2ship** (Shopify `displayStatus` only as a fallback), instead of firing off the raw
webhook string.

**Phase B — Live tracking Q&A.** The same ad2ship adapter feeds the order-tracking agent, which
relays live status / location / expected date for a shipped, non-delivered order when the store's
`tracking` reveal is on.

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
| D5 | When the re-check **cannot read ad2ship** (unreachable / unparseable): fall back to the Shopify rule — send `order_delivered` only if `displayStatus == "DELIVERED"` **and** the `DELIVERED` event is the latest event; otherwise leave the row `pending` for a later retry. This fallback is leaky (misses tavas3674-type RTOs) but strictly better than blindly sending. |

**Residual risk accepted (D2 + D5):** ad2ship normally shows the terminal `rto_*` / `delivered`
badge at the same time Shopify fires the `delivered` webhook (same scan source), so the 2h wait is
ample in the common case. The exposure is a *simultaneous* ad2ship outage — then the D5 Shopify
fallback governs, and a tavas3674-shaped RTO (Shopify says `DELIVERED`, latest event) would send a
wrong `order_delivered`. Accepted; a second re-check at +12h would shrink the window further, not
built now.

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

`rto` = ad2ship reports an `rto_*` badge / "Rto Delivered" history, **or** (fallback path only) a
Shopify `DELIVERED` event exists but is not the latest event.

Writers:
- **Phase A sweep job** — authoritative for `delivered` vs `rto` at the due time, primarily from
  the ad2ship badge, with the Shopify `displayStatus` fallback (D5).
- **Phase B ad2ship fetch** — refines `shipment_status` for in-flight orders and fills the
  `tracking_*` columns. Monotonic: it only advances along the progression above and never
  overwrites a terminal value (once `delivered`/`failure`/`rto`, it stays).

---

## 5. Phase A — Delivered-notification fix

> **Prerequisite:** the ad2ship adapter (§6.1) is built **before** this phase — the sweep job's
> primary signal is `ad2ship.fetch_tracking`.

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
  (batch cap consistent with the other jobs, e.g. `_RECONCILE_LIMIT`-scale).
- Load the fulfillment's AWB from the mirror (`fulfillments.tracking_number` for `fulfillment_gid`).
- **Primary — ad2ship** (`ad2ship.fetch_tracking(http, awb)`):
  - `t.status == "delivered"` → **send** `order_delivered` (see send path below), row `state='sent'`,
    `fulfillments.shipment_status='delivered'`.
  - `t.status` starts with `rto` (`rto_delivered`, `rto_in_transit`) → row `state='rto'`,
    `fulfillments.shipment_status='rto'`, **no send.**
  - `t.status` non-terminal (`in_transit`, `out_for_delivery`, `attempted_delivery`, `unknown`) →
    ad2ship has not settled; **do not send**, fall through to the fallback below (and if that is
    also inconclusive, leave the row `pending`).
  - `t is None` (unreachable / unparseable) → fall through to the fallback.
- **Fallback — Shopify (D5)**, only when ad2ship was `None` or non-terminal: GraphQL-read the
  fulfillment (see 5.3) and evaluate `fulfillment_is_genuinely_delivered(f)`:
  - `True` → **send**, row `state='sent'`, `shipment_status='delivered'`.
  - `False` → leave row `pending` (retried next run). Do **not** mark `rto` from the fallback — the
    Shopify signal is not trusted enough to assert RTO, only to withhold the send.
  - Shopify also errors → leave row `pending`, log exception type + location only (mirrors
    `reconcile_cancels`).
- **Abandon horizon** — `now() > due_at + 7 days` and still `pending` → `state='abandoned'`, no send.
- Return `{"swept", "sent", "rto", "pending", "abandoned", "errors"}`.

**Send path:** `_enqueue_and_send_fulfillment_notification(c, order_gid=…, dedupe_key=
f"fulfillment_delivered:{fulfillment_gid}", phone=<row.phone_e164>, template="order_delivered",
body_params=[name, order.name])` — the exact call the webhook used to make inline, now made from the
job. `name` / `order.name` come from `c.ingest.get_mirrored_order(order_gid)`; a mirror miss logs and
leaves the row `pending`.

Idempotency: the `outbound_messages` `dedupe_key UNIQUE` on `fulfillment_delivered:{gid}` is the real
guard — double-processing a row cannot double-send.

### 5.3 GraphQL read extension (fallback path only) — `backend/app/shopify/client.py`

`FULFILLMENT_FIELDS` (`:88-91`) gains `displayStatus` and
`events(first: 250, sortKey: HAPPENED_AT) { nodes { status happenedAt } }`.
`_fulfillments_from_node` maps them onto the `Fulfillment` model (new fields
`display_status: str | None = None`, `events: tuple[FulfillmentEvent, ...] = ()` where
`FulfillmentEvent` is a frozen `(status: str, happened_at: str)`). All new fields are defaulted so
existing callers and the mirror read stay backward-compatible. This read already runs isolated from
`ORDER_FIELDS` (the `read_fulfillments`-scope safety split), so no blast radius on `get_order` /
Confirm / Cancel.

Helper `fulfillment_is_genuinely_delivered(f: Fulfillment) -> bool` lives in `app/core/` (pure,
unit-tested): `True` iff `f.display_status == "DELIVERED"` **and** `f.events` is non-empty **and**
the `happened_at` of the (first) `DELIVERED` event equals `max(e.happened_at for e in f.events)`
(ISO-8601 strings sort correctly). Empty events or a non-`DELIVERED` display status → `False`.

### 5.4 Scheduling

Owner adds one cron entry to the deployed `vercel.json`:

```json
{ "path": "/internal/jobs/delivery_confirm", "schedule": "*/15 * * * *" }
```

Same mechanism as `send_reminders`. Net effect: `order_delivered` lands 2h–2h15m after the delivered
scan for a clean delivery. **This is a deploy-config change — owner applies it, not Claude.**

---

## 6. The ad2ship adapter (shared, built first) + Phase B — Live tracking Q&A

§6.1 is the shared adapter both phases use. §6.2–6.3 are Phase B (the Q&A enrichment).

### 6.1 New adapter — `backend/app/shopify/ad2ship.py`  *(shared)*

Placed under `app/shopify/` for proximity to the tracking/fulfillment code where the AWB and
`trackingInfo` already live. It is a standalone HTTP adapter with **no Shopify coupling** — a single
`fetch_tracking` function behind which the page-parse detail is fully contained (a future ad2ship API
backing would replace the internals without touching callers). Consumed by the Phase A sweep job and
the Phase B agent enrichment.

```python
@dataclass(frozen=True)
class Ad2shipTracking:
    status: str            # NORMALIZED, = the `status-badge` CSS class verbatim, lowercased.
                           # Observed: pickup_scheduled | picked_up | in_transit |
                           # out_for_delivery | undelivered | ndr | delivered |
                           # rto_in_transit | rto_delivered | cancelled | unknown
    status_label: str      # badge text as rendered, e.g. "RTO Delivered", "Delivered"
    current_city: str | None    # state in parens from the latest history location, e.g. "Gujarat"
    current_hub: str | None     # hub token from the latest history location, e.g. "Surat_Laldarwaja_R"
    last_scan: str | None       # latest .h-status strong text, e.g. "RETURN ACCEPTED - DL-RTO-RD-AC"
    last_scan_remark: str | None # latest .h-remarks, e.g. "Rto Delivered"
    last_scan_at: str | None    # latest history timestamp text as shown
    expected_date: str | None   # a .date-box whose label matches /expected|estimated/i, else None

    def is_delivered_to_customer(self) -> bool:
        return self.status == "delivered"

    def is_rto(self) -> bool:
        return self.status.startswith("rto")

    def is_terminal(self) -> bool:
        return self.is_delivered_to_customer() or self.is_rto() or self.status == "cancelled"

async def fetch_tracking(
    http: httpx.AsyncClient, awb: str, *, timeout: float = 4.0
) -> Ad2shipTracking | None
```

- `GET https://ad2ship.com/track-order/{awb}`, browser-like `User-Agent`, `follow_redirects=False`.
- Parse hooks (from real page fixtures, see §9):
  - `status` ← `re` match on `class="status-badge <token>"`, lowercased. Unrecognised /
    missing → `"unknown"`.
  - `status_label` ← text content of that badge element.
  - `last_scan` / `last_scan_remark` / `last_scan_at` ← the **first** `.history-list .history-item`:
    `.h-status strong`, `.h-remarks`, and the item's time/date node.
  - `current_hub` / `current_city` ← the first `.history-item .h-location` text, split on the
    trailing `"(<State>)"` — hub = the part before, city = the parenthesised state. Either may be
    `None`.
  - `expected_date` ← a `.date-box` (`<span>LABEL</span><strong>VALUE</strong>`) whose LABEL
    matches `/expected|estimated/i`. The common `"Delivered Date"` box is **not** an expected date.
- Parser is stdlib `re` — `backend/pyproject.toml` has **no HTML-parsing dependency** and this
  spec adds none. The hooks above are single-attribute / single-class anchors that `re` handles
  cleanly; fixtures lock the behaviour.
- Returns `None` on **any** failure: timeout, non-200, HTML present but the `status-badge` node
  absent, parse exception. Never raises. Logs exception **type only** — never the AWB or URL
  (matches the tracking-number no-log rule, `error_learnings.md` 2026-08-13).
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
| Sweep `ad2ship.fetch_tracking` | returns `None` → sweep falls through to the Shopify fallback (D5) |
| Sweep Shopify GraphQL read (fallback) | row stays `pending`, retried next run; `errors` counter++ |
| Sweep `order_delivered` send | existing `send_inline_outbound` isolation — row not marked `sent`, re-attempted next run |
| Agent `ad2ship.fetch_tracking` | returns `None` → agent uses existing tracking-link fallback text |

No new code path can 500 a webhook or block the Shopify `<5s` ack (the webhook change only writes one
row). All logging is PII-free: exception type + code location, never `str(exc)`, never the tracking
number / URL / phone — matching `_log_notify_failure` and the 2026-08-13 error-learning.

`send_mode` kill switch, allowlist, and shadow mode apply unchanged — the sweep job sends through the
same `send_inline_outbound` path as every other outbound.

---

## 8. Edge cases

- **Split shipments** — one `pending_delivery_confirmations` row and one `shipment_status` per
  fulfillment gid; `order_delivered` already dedupes per fulfillment gid.
- **AWB missing from the mirror row** — sweep cannot query ad2ship; falls straight to the Shopify
  fallback (D5); agent skips live enrichment and uses fallback text.
- **ad2ship non-terminal at due time** (`in_transit` etc. while Shopify already said delivered) —
  sweep does not send, tries the Shopify fallback, else leaves the row `pending` for the next run.
- **`pending` row never resolves** — `abandoned` after `due_at + 7 days`, no send.
- **Genuine delivery + Shopify-fallback false negative** — we withhold `order_delivered`. Safe
  direction; customer can still ask (Phase B answers).
- **Customer asks about a `delivered` / `rto` order** — no live fetch; answer from stored state.
- **Webhook replay of the `delivered` event** — `ON CONFLICT DO NOTHING` keeps the original
  `due_at` and `phone_e164`.
- **ad2ship page redesign** — `fetch_tracking` returns `None`; Phase B degrades to today's
  behaviour; Phase A falls back to the Shopify rule (D5).

---

## 9. Testing (TDD — pytest + pytest-asyncio, RED→GREEN)

### Fixtures (real pages, saved 2026-08-29) — `backend/tests/agents/fixtures/ad2ship/`
- `delivered_tavas4464.html` — badge `delivered`, "DELIVERED TO CONSIGNEE - CODE VERIFIED".
- `rto_delivered_tavas3908.html`, `rto_delivered_tavas3674.html`, `rto_delivered_tavas3813.html` —
  badge `rto_delivered`, "RETURN ACCEPTED / Rto Delivered", origin location.
- The plan also saves an **in-transit** fixture (`in_transit_<awb>.html`, capture during Task for
  §6.1) and a **garbage** fixture (`malformed.html` — valid HTML, no `status-badge` node).

### ad2ship adapter (§6.1) — built first
- `fetch_tracking` on `delivered_tavas4464.html` → `status == "delivered"`,
  `is_delivered_to_customer()` True, `current_city == "Maharashtra"`, `last_scan_remark == "Delivered"`.
- on `rto_delivered_tavas3908.html` → `status == "rto_delivered"`, `is_rto()` True,
  `current_hub` starts `"Surat_"`, `expected_date is None` (only a "Delivered Date" box present).
- on `in_transit_<awb>.html` → `status` non-terminal, `is_terminal()` False.
- on `malformed.html` → `None`. On HTTP 404 / 500 → `None`. On timeout (`httpx.TimeoutException`
  from a mock transport) → `None`. No exception propagates.

### Phase A
- Webhook: a `delivered` payload writes a `pending_delivery_confirmations` row
  (`state='pending'`, `due_at ≈ now+2h`, `phone_e164` resolved) and sends **nothing**.
- Webhook replay: second `delivered` delivery does not change `due_at` / `phone_e164`.
- `fulfillment_is_genuinely_delivered` unit cases: `display_status="DELIVERED"` & DELIVERED is
  latest → True; DELIVERED present but not latest → False; `display_status="ATTEMPTED_DELIVERY"`
  → False; empty events → False.
- `run_delivery_confirm` sweep (ad2ship mocked), fixture rows:
  (a) ad2ship `delivered` → `order_delivered` sent once, row `sent`, `shipment_status='delivered'`;
  (b) ad2ship `rto_delivered` → row `rto`, **no send**, `shipment_status='rto'`;
  (c) ad2ship `None` + Shopify `fulfillment_is_genuinely_delivered` True → sent, row `sent`;
  (d) ad2ship `None` + Shopify False → row stays `pending`, no send;
  (e) ad2ship non-terminal + Shopify False → row stays `pending`;
  (f) ad2ship `None` + Shopify GraphQL raises → row stays `pending`, `errors++`;
  (g) row past `due_at + 7d` still `pending` → `abandoned`, no send.
- Two sweep passes over the same `sent`-eligible row → exactly one `order_delivered` (dedupe key).
- `send_mode = off` → sweep marks nothing `sent`, zero Meta calls.

### Phase B
- Cache rule: `tracking_checked_at` within 30 min → agent makes **zero** HTTP calls (mock asserts).
- `_render_order`: live data present → `Current status` / `Currently at` / `Latest update` /
  `Expected delivery` lines rendered; `fetch_tracking` `None` → only the existing tracking-link
  fallback text; `tracking` reveal off → no tracking lines at all (unchanged); `shipment_status`
  terminal (`delivered`/`rto`) → no live fetch.
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
- `docs/memory/error_learnings.md` — the RTO-as-delivered root cause; **Shopify has no reliable RTO
  signal** (`shipment_status`, `displayStatus`, and event ordering all fail — tavas3674/tavas3813
  are RTOs Shopify shows as clean `DELIVERED`); the only reliable source is the ad2ship
  `status-badge` (`rto_*` vs `delivered`).
