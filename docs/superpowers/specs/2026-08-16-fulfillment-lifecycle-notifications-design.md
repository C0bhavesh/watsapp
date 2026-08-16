# Fulfillment Lifecycle Notifications (order_shipped / order_delivered) — Design

**Status:** Approved by owner (2026-08-16), via conversational brainstorming (not the visual companion).

## Problem

The owner's WABA has two more Meta-approved templates that aren't wired into the app yet:
`order_shipped` (courier + tracking link, sent when an order ships) and `order_delivered` (sent
once it arrives). Today, `fulfillments/create`/`fulfillments/update` webhooks are received and
mirrored (tracking data stored) but nothing is ever sent to the customer — this closes that gap.

This was explicitly scoped OUT of the order-type-routing/templated-replies feature (2026-08-15)
as an independent subsystem, per the owner's own decision to sequence them separately.

## What already exists (verified by reading the code and a real production payload, not assumed)

- `fulfillments/create`/`fulfillments/update` are already subscribed Shopify webhook topics
  (`app/shopify/subscriptions.py::REQUIRED_TOPICS`), routed in `shopify_webhook.py` to
  `fulfillment_from_webhook_payload` → `_mirror_fulfillment` (pure data sync today, no send).
- **A real fulfillments/update payload from this store** (captured 2026-08-16, order tavas4119)
  confirms `shipment_status: "delivered"` IS populated for this store's actual courier
  (Delhivery Surface) — resolving the open question of whether Shopify's shipment-status tracking
  is usable at all for this store's couriers. The same payload also confirms `tracking_company`
  and `tracking_number`/`tracking_url` are present. It carries NO order name (only the
  fulfillment's own sub-name, e.g. `"tavas4119.1"`) and NO customer object (only
  `destination.first_name`/`last_name`, the shipping address name) — so template params must NOT
  be sourced from this payload directly; see "Data sourcing" below.
- `app/store/base.py::IngestStore.get_mirrored_order(gid) -> Order | None` already exists (from
  the 2026-08-12 mirror-first-reads feature) and returns the full `Order` (name, customer,
  phone fields) from the `orders`/`customers`/`order_items` mirror, already populated by
  `orders/create`/`orders/updated` webhooks independent of this feature.
- `app/jobs/outbox_drain.py::send_inline_outbound(c, outbound_id)` is already generic — it claims
  ANY row by id via `claim_outbound_by_id` and calls `send_one_outbound`, which is NOT generic
  today: `_TemplatePayload`/`_parse_payload` hardcode exactly the 6 named keys
  `cod_confirmation`/`prepaid_order` need. This is the one piece of already-shipped code this
  feature must widen.
- `app/store/base.py::IngestStore.enqueue_outbound(draft: OutboundDraft) -> bool` already exists
  (used by `jobs/reminders.py`) — a generic "insert with `ON CONFLICT (dedupe_key) DO NOTHING`"
  primitive, the same exactly-once mechanism `order_created:{gid}`/`order_reminder:{gid}` already
  rely on.
- `order_shipped` and `order_delivered` are both Meta-approved in `en` only (checked live during
  planning for the previous feature, re-confirmed unchanged) — same language-pin situation as
  every other template this app sends.
- `app/channels/whatsapp_sender.py::send_template` already supports positional `Sequence[str]`
  body params (added 2026-08-15/16, Task 1 of the previous feature) — `order_shipped` (4
  positional: name, order number, courier, tracking link) and `order_delivered` (2 positional:
  name, order number) both fit this without further sender changes.

## Design

### 1. Generalized outbox payload

Today's `_TemplatePayload`/`_parse_payload` (`outbox_drain.py`) require exactly
`template, language, customer_name, order_id, product_name, product_color, product_size,
product_amount` (+ optional `image_url`), and `send_one_outbound` builds a hardcoded named-params
dict from those 6 fields plus a hardcoded Confirm/Cancel button pair (already made conditional on
`template == "cod_confirmation"` in the prior feature).

Replace with a generic envelope stored in `payload_json`:
```json
{"template": "...", "language": "...", "body_params": {...} | [...], "image_url": "...?", "buttons": [...]?}
```
`body_params` is either a JSON object (named params, e.g. `cod_confirmation`/`prepaid_order`) or a
JSON array (positional params, e.g. `cod_confirmmsg`-style or the new `order_shipped`/
`order_delivered`) — mirroring `send_template`'s own existing `Mapping | Sequence` duality, so
`_parse_payload` just needs to preserve whichever JSON type it finds rather than coercing to one
shape. `buttons` becomes an explicit field (a list of `order:confirm:{gid}`/`order:cancel:{gid}`-
style payload strings, or absent/empty) instead of being derived from a `template ==
"cod_confirmation"` string check — this generalizes correctly since fulfillment notifications
never have buttons, without hardcoding template-name comparisons for button logic going forward.

`shopify_webhook.py`'s existing `cod_confirmation`/`prepaid_order` construction is updated to
write this new shape (still the same underlying data, just enveloped generically) — this is the
one behavioral no-op migration required so the OLD payload construction and the NEW fulfillment
payload construction both write the same envelope `_parse_payload` reads. Legacy pre-migration
rows already queued (if any) fail to parse under the new required-keys check and are marked
`undeliverable`, exactly the same graceful-degradation precedent already used when
`order_confirmation_cod` was retired in favor of `cod_confirmation` (Q19) — acceptable because
`send_mode` stays `allowlist` and there is no real customer backlog at risk.

### 2. Shipped/delivered detection

In `shopify_webhook.py`'s existing `fulfillments/create`/`fulfillments/update` branch, after
`_mirror_fulfillment` succeeds (mirroring stays first and authoritative — a notification is a
side effect of already-correct data, never the other way around):

- **Shipped**: the parsed `Fulfillment` has both `tracking_company` and (`tracking_number` OR
  `tracking_url`) non-empty. Checked on every `fulfillments/create` AND `fulfillments/update`
  event (tracking can be attached after initial creation) — the dedupe key, not an in-code
  "have I seen this before" flag, is what prevents a duplicate send on repeat events.
- **Delivered**: the RAW webhook payload's `shipment_status == "delivered"` (case-sensitive exact
  match against Shopify's own enum value, confirmed live). This is a NEW field read directly from
  the raw payload dict in `shopify_webhook.py` (not added to the `Fulfillment` dataclass itself —
  it's a one-shot trigger check, not stored tracking data the rest of the app needs, so it doesn't
  belong in the mirror's `Fulfillment` model; `fulfillment_from_webhook_payload` stays unchanged).

Both checks run unconditionally on every fulfillment webhook (cheap string checks, no extra
Shopify calls) — whichever condition is newly true (per the dedupe key, "newly true" simply means
"not already enqueued") triggers its own independent enqueue+inline-send.

### 3. Idempotency (dedupe keys)

Two new dedupe-key families, enqueued via the existing `enqueue_outbound`:
- `fulfillment_shipped:{fulfillment_gid}`
- `fulfillment_delivered:{fulfillment_gid}`

`fulfillment_gid` (not `order_gid`) is the key's identity — a split-shipment order (multiple
fulfillments) gets its own shipped/delivered pair per fulfillment, which is correct (each shipment
is its own physical parcel with its own tracking). The existing `ON CONFLICT (dedupe_key) DO
NOTHING` on `enqueue_outbound` is the entire idempotency mechanism — no new flag/column, no new
state machine, matching exactly how `order_created:{gid}`/`order_reminder:{gid}` already work.

### 4. Data sourcing

Template params come from `c.ingest.get_mirrored_order(order_gid)` — NOT from the fulfillment
webhook payload itself (confirmed above it lacks a usable order name or customer object). Reuses
`customer_display_name(order)` (existing, from the prior feature) for the name param and
`order.name`/`order.best_phone()` for the rest — the exact same helpers `order_actions.py` and
`reconcile.py` already use, so this introduces no new data-shaping logic, only a new call site.

If `get_mirrored_order` returns `None` (mirror write failed earlier, or this fulfillment belongs
to an order this bot never saw `orders/create` for — e.g. a very old order), the notification is
skipped entirely — logged (gid only, no PII) and the webhook still acks 200 normally. This matches
this codebase's established "a mirror miss degrades gracefully, never blocks the ack" posture
(`_mirror_order`/`_mirror_fulfillment` already follow this).

`order.best_phone()` (not a `push_policy`/`is_eligible_for_push` check) gates who gets notified —
`push_policy` (`cod_only`/`all`/etc.) governs the INITIAL order-confirmation push eligibility only;
shipped/delivered are a separate lifecycle stage and fire for any mirrored order with a phone,
regardless of whether its original confirmation push was sent. `send_mode`/allowlist (via
`send_decision`, evaluated the same way every other send in this app already is) remains the one
gate that actually controls real-world delivery.

### 5. Send mechanism

Both notifications enqueue via `enqueue_outbound`, then attempt an inline send via the ALREADY
GENERIC `send_inline_outbound(c, outbound_id)` — same ADR-001 pattern as order confirmation,
necessary because there is no working scheduler today (the backstop drain is manual-only). Two
enqueue+inline-send attempts can happen in the same webhook invocation (shipped AND delivered
both newly true in one event — rare but possible, e.g. a courier reports the full journey in one
update) — each is independent and bounded by its own short timeout, so worst case is two bounded
sends stacked on the same ack budget. Neither notification has an image header (unlike
`cod_confirmation`), so there is no extra product-image fetch stacking on top the way Q19's inline
path had — the exact timeout budget (single vs. combined, and its numeric value) is a planning-
time detail to ground against the current `_INLINE_SEND_TIMEOUT_SECONDS` constant and Shopify's
5s ack ceiling, not decided here.

`OutboundDraft.kind` (a free-text label, e.g. `"order_confirmation"` today) is written but never
read/branched on anywhere in the app — confirmed by a repo-wide check — so this feature can freely
use its own descriptive values (e.g. `"fulfillment_shipped"`/`"fulfillment_delivered"`) with zero
functional impact; it exists purely for future human/admin-panel legibility.

## Out of scope (YAGNI)

- Any change to `order_confirmation`/`prepaid_order`/`cod_confirmmsg`/`cod_cancel`'s own sending
  logic beyond the payload-envelope migration in section 1 — their template names, languages,
  timing, and content are untouched.
- A live courier-tracking integration (a2ship or otherwise) — already decisively closed by Q10
  ("no live courier integration... send the Shopify tracking link"). This feature only reacts to
  Shopify's own webhook-delivered `shipment_status`, never polls a courier API.
- Any change to `delivered_at` capture (Q18 storage groundwork, already shipped) — that field is
  populated via the live GraphQL path for Q&A purposes; this feature's "delivered" TRIGGER is
  entirely independent (webhook `shipment_status`, not the `Fulfillment.delivered_at` field).
- A backfill/replay for fulfillments that already shipped/delivered before this feature existed —
  no historical shipped/delivered notifications are sent for already-fulfilled orders; only new
  webhook events after deploy trigger a send.
- Multi-language `order_shipped`/`order_delivered` — both pinned to `en`, same as every other
  template, until/unless hi/gu versions are separately approved in Meta (a client question, not
  built here).

## Testing

- `outbox_drain.py`'s generalized `_parse_payload`: a named-`dict` `body_params` payload parses
  identically to today (regression proof); a positional-`list` `body_params` payload (new) parses
  correctly with no `parameter_name` keys downstream; a `buttons` field present/absent both work;
  a legacy pre-migration row (missing `body_params`, old 6-flat-key shape) is marked
  `undeliverable`, not silently mis-parsed.
- `shopify_webhook.py`'s fulfillment branch: tracking-first-attached on `fulfillments/create`
  enqueues+sends `order_shipped` with the correct 4 positional params; tracking added later via
  `fulfillments/update` (not present on the original `create`) also triggers it; a REPEAT event
  with tracking already present does NOT re-enqueue (dedupe key already exists); `shipment_status:
  "delivered"` triggers `order_delivered`; a non-`"delivered"` status does not; both conditions
  newly true in the same event enqueue+send both, independently.
- Mirror-miss case: a fulfillment webhook for an order with no mirror row skips the notification
  entirely, still acks 200, logs gid only.
- `send_mode`/allowlist gating: a suppressed decision doesn't send (mirrors every other send path's
  existing test pattern in this codebase).
- Split-shipment case: two different `fulfillment_gid`s on the same `order_gid` each get their own
  independent shipped/delivered dedupe keys and sends.

## Global constraints (already binding, restated for this feature)

- Critical Rule 2 (LLM never mutates) — untouched; this feature adds no new Shopify write paths,
  only reads (mirror lookup) and sends.
- Critical Rule 3 (always re-fetch live before acting) — not applicable here; this is a read-only
  notification path, no mutation gate to preserve. The mirror-vs-live distinction from the
  2026-08-12 feature (mutation path stays live-only) is unaffected — this feature never touches
  `resolve_by_gid`/`order_actions.py`.
- `send_mode` kill switch — every send goes through the same `send_decision`-gated path the rest
  of the app already uses; no new bypass.
- No new secrets, no new admin-panel config, no schema/migration changes (dedupe keys are just
  new string patterns in the existing `outbound_messages.dedupe_key` column; `body_params`'s
  generalized JSON shape needs no schema change since `payload_json` is already a free-form text
  column).
