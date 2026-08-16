# Order-Type Routing + Templated Confirm/Cancel Replies — Design

**Status:** Approved by owner (2026-08-15), via conversational brainstorming (not the visual companion).

## Problem

The owner connected a new WhatsApp number and got 7 templates approved: `cod_confirmation`,
`prepaid_order`, `cod_confirmmsg`, `cod_cancel`, `order_shipped`, `order_delivered`,
`hello_world`. Only `cod_confirmation` is wired into the app today (Q19, 2026-08-15) — every
other approved template is unused. The owner specified how the first four should map onto the
existing order lifecycle:

- New COD order → `cod_confirmation` (already shipped)
- Customer taps Confirm → `cod_confirmmsg`
- Customer taps Cancel (and it actually goes through) → `cod_cancel`
- New prepaid order → `prepaid_order`

This also surfaces and fixes a real bug: `shopify_webhook.py`'s `TEMPLATE_NAME` is hardcoded to
`cod_confirmation` for every eligible order regardless of payment method, so a prepaid customer
today gets asked to "confirm" a Cash-on-Delivery order they already paid for, with a Cancel
button that (per the Q1 decision, 2026-07-29) was already flagged to the owner as a risk of
accidental cancellations on paid orders.

`order_shipped`/`order_delivered` (fulfillment lifecycle notifications) are explicitly OUT OF
SCOPE for this spec — a separate, independent design, built next.

## What already exists (verified by reading the code, not assumed)

- `IncomingOrder.is_cod()` (`app/channels/shopify_orders.py`) already classifies an order as COD
  vs. prepaid from the webhook payload (payment gateway name / `cod` tag).
- `shopify_webhook.py`'s `orders/create` branch builds an `OutboundDraft` with a `payload_json`
  carrying `template`, `language`, `customer_name`, `order_id`, `product_name`, `product_color`,
  `product_size`, `product_amount`, optional `image_url` — the exact same 6 named fields
  `cod_confirmation` and `prepaid_order` both require (confirmed by reading both templates'
  approved component definitions from the Graph API — identical BODY/HEADER shape, `prepaid_order`
  has no BUTTONS component).
- `outbox_drain.py::send_one_outbound` unconditionally builds
  `buttons = [f"order:confirm:{gid}", f"order:cancel:{gid}"]` for every row and passes them to
  `send_template`.
- `order_actions.py::_handle_confirm` sends the plain-text `confirm_success`/`already_confirmed`
  replies (`copy_for`) via `_safe_send_text`, a best-effort inline `await` (swallows
  `WhatsAppSendError`, never raises) — the established pattern for every reply in this file.
- `order_actions.py`'s cancel flow is two-phase: the first tap only asks "are you sure?"; the
  confirm tap calls `c.shopify.cancel_order(auth)` and, if it doesn't raise, sends the plain-text
  `cancel_requested` reply — deliberately soft wording ("we have requested cancellation... we will
  confirm once it is done"), because `orderCancel` is async and Shopify's `cancelledAt` is not
  guaranteed to be set yet.
- `jobs/reconcile.py::run_reconcile_cancels` is a separate cron job that re-fetches every order
  still in `cancel_requested`, and ONLY once Shopify reports `cancelledAt` actually set, applies
  the final `cancelled` tag and advances the mapping. It does not send any WhatsApp message today.
  It already has `auth.verified_phone` (via `authorize_own_order(order)`) and
  `order.customer_locale` available — everything needed to notify, with no new Shopify calls.

## Design

### 1. Order-creation routing (COD vs. prepaid)

In `shopify_webhook.py`, replace the hardcoded `TEMPLATE_NAME` constant with a per-order choice:
`"cod_confirmation" if incoming.is_cod() else "prepaid_order"`, written into the same
`payload_json` shape as today (no new fields).

In `outbox_drain.py::send_one_outbound`, make the button attachment conditional:
`buttons = [f"order:confirm:{gid}", f"order:cancel:{gid}"] if payload.template ==
"cod_confirmation" else []`. `send_template` already accepts an empty `button_payloads` sequence
(the existing `cod_confirmation` code path already builds zero button components when none are
passed), so no sender-side change is needed. `_TemplatePayload`/`_parse_payload` need no new
fields — `payload.template` already carries the value needed for this branch. The 1-hour reminder
job (`reminders.py`) replays `payload_json` verbatim and inherits the correct template/button
behavior automatically since it re-parses the same payload.

Prepaid orders get **no automatic Shopify tag**. `financial_status: paid` already signals payment
in Shopify; the "confirmed" tag stays meaningful specifically as "the customer confirmed via
WhatsApp," which structurally cannot happen for a no-button template. No mapping-status change
either — a prepaid order's mapping stays at whatever ingest already sets it to today.

### 2. Confirm tap → `cod_confirmmsg`

Add a `_safe_send_template` helper to `order_actions.py`, mirroring `_safe_send_text`/
`_safe_send_buttons` exactly (best-effort inline `await send_template(...)`, catches
`WhatsAppSendError`, logs, never raises). `cod_confirmmsg` takes two positional body params
(`{{1}}` name, `{{2}}` order number) — `order.name`/mapping's `customer_name`/`auth.order`'s
own fields already available at this call site, no new data needed.

In `_handle_confirm`: after the successful `add_tags` call, and on the idempotent
"already confirmed" re-tap branch, call `_safe_send_template(..., "cod_confirmmsg", ...)` instead
of `copy_for("confirm_success"|"already_confirmed", ...)`. Both branches converge on the same
template send since the template's wording ("your order has been confirmed successfully") is
accurate for a genuine confirm and a harmless re-statement on a re-tap.

Every other reply in `order_actions.py` (`cancel_too_late`, `already_cancelled`, `not_found`,
`error_fallback`, `cancel_are_you_sure`, `cancel_kept`, `cancel_failed`, and the first-phase
`cancel_requested` "we've requested it" message) stays plain text, unchanged — no approved
template covers those states, and building/approving new ones for every edge case is out of
scope.

### 3. Cancel confirmation → `cod_cancel`, fired from `reconcile.py`

`order_actions.py`'s cancel flow is **not modified** — the two-phase ask, the provisional
`cancel_requested` tag, and the soft plain-text reply after requesting all stay exactly as they
are today. `cod_cancel`'s wording ("your order has been cancelled successfully") is a completion
claim, so it is deliberately NOT sent at request time — only once Shopify has actually confirmed
the cancellation.

In `jobs/reconcile.py::run_reconcile_cancels`, right after `add_tags(auth, controls.tags.cancelled)`
and `set_mapping_status(gid, "cancelled")` succeed (i.e., cancellation is now a confirmed fact),
add a best-effort `send_template(..., "cod_cancel", ...)` call using `auth.verified_phone` and
`choose_language(order.customer_locale, ...)`. `cod_cancel` also takes two positional params
(`{{1}}` name, `{{2}}` order number) — `order.name` for the second; for the first,
`order.customer.first_name` + `order.customer.last_name` if present, else the literal fallback
`"-"` (matching the exact missing-field fallback Q19 already established for `cod_confirmation`'s
named params, so this stays consistent rather than inventing a second convention).

The send is wrapped to swallow `WhatsAppSendError` (log and continue) the same way this job
already swallows a per-order `ShopifyError` — the tag/mapping-status write is the source of truth
and must never be rolled back or retried because a notification failed. This is a genuinely new
capability for `reconcile.py` (a job that today only mutates Shopify); no outbox/queue
involvement — a direct call, consistent with keeping this job self-contained. A notification
failure here has no automatic retry (same accepted risk profile as every other best-effort reply
send elsewhere in the codebase) — accepted explicitly by the owner during design.

**Kill-switch gating (new requirement, not already present):** `reconcile.py` sends nothing
today, so it has never needed to check `send_mode`. Since this adds its first-ever outbound send,
`run_reconcile_cancels` must call `send_decision(controls.send_mode, controls.allowlist_phones,
auth.verified_phone)` before sending `cod_cancel`, and suppress (skip the send, proceed with the
tag/status write regardless) on anything other than a "send" decision — the exact same gating
`order_actions.py::dispatch_button` already applies before any of its sends. Without this, the
kill switch (`send_mode` is `allowlist` in production today) would have no effect on this new
send path, and every real customer's reconciled cancellation would get messaged regardless of the
switch. `load_whatsapp_config` is also newly needed here (this job has never loaded WhatsApp
config before, only `load_controls` for `controls.tags.cancelled`) — both loads happen once at
the top of `run_reconcile_cancels`, mirroring how `order_actions.py::dispatch_button` loads them.

### Out of scope (YAGNI)

- `order_shipped` / `order_delivered` — a separate design, built next.
- Any change to the Confirm/Cancel two-phase mutation flow itself, `resolve_by_gid`, or
  `AuthorizedOrder` — untouched; this feature only changes which reply gets sent after an
  already-correct mutation decision.
- New templates for edge-case replies (`cancel_too_late`, `already_cancelled`, etc.) — plain text
  stays, no owner request to change them.
- Retry/queue infrastructure for the `reconcile.py` notification — explicitly accepted as
  best-effort, matching the existing risk profile of every other reply send in this codebase.
- Any change to `cod_confirmation`/`prepaid_order` header-image resolution, timing budget, or URL
  validation (Q19, already shipped and reviewed) — this feature only branches which template name
  gets used, not how either one is built or sent.

## Testing

- `shopify_webhook.py`: COD order → `payload_json.template == "cod_confirmation"`; prepaid order
  → `"prepaid_order"`; both carry identical field shape otherwise.
- `outbox_drain.py`: a `prepaid_order` row sends with zero button components; a `cod_confirmation`
  row is unaffected (still gets both buttons) — regression-proof the existing behavior.
- `order_actions.py`: a Confirm tap sends `cod_confirmmsg` (not the old plain text) with the
  correct two params; an already-confirmed re-tap also sends `cod_confirmmsg`; every other
  branch's plain-text reply is unchanged (explicit regression assertions, not just "still passes").
- `reconcile.py`: an order that reconciles to `cancelled` sends `cod_cancel` with the correct
  phone/params after the tag+status write; a `WhatsAppSendError` during that send does not affect
  the job's `reconciled` count or roll back the tag/status write; an order still `pending`
  (not yet cancelled per Shopify) sends nothing, matching today's behavior; a suppressed
  `send_decision` (e.g. the reconciled order's phone is not on the allowlist) skips the send but
  still completes the tag/status write and counts toward `reconciled` — the kill switch affects
  notification only, never the mutation's own correctness.
- Confirm `git diff -- app/core/order_actions.py` is NOT empty this time (it's the deliberate
  target of section 2) but the mutation logic itself (tag/mapping writes, ownership checks,
  two-phase cancel gating) is byte-identical before/after — only the reply mechanism changes.
  `resolve_by_gid`'s call sites and `AuthorizedOrder` construction are unaffected.

## Global constraints (already binding, restated for this feature)

- Critical Rule 2 (LLM never mutates) — untouched; no new Shopify write paths, only new
  notification sends attached to existing, already-gated mutation outcomes.
- Critical Rule 3 (always re-fetch live before acting, ownership check before revealing) —
  untouched; `reconcile.py` already re-fetches live via `c.shopify.get_order(gid)` before
  deciding to notify, and only notifies the order's own `verified_phone`.
- `send_mode` kill switch — the order-creation routing (section 1) and the Confirm-tap reply
  (section 2) already flow through existing `send_decision` gates (`outbox_drain.py`'s row-level
  check; `dispatch_button`'s check before any resolve/reply). The cancel-confirmation send
  (section 3) is NEW outbound traffic from a job that has never sent anything before, so it gets
  its own explicit `send_decision` gate as part of this feature — see section 3's "Kill-switch
  gating" note. This is not optional: without it, `reconcile.py` would ignore `send_mode` entirely.
- No new secrets, no new admin-panel config, no schema/migration changes.
