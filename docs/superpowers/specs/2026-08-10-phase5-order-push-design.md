# Phase 5 — Order Push + Confirm/Cancel Automation — Design Spec

> Created 2026-08-10. This is the **outbound** half of the product — the WATI replacement.
> Built under owner-delegated authority (owner asleep, granted free will to complete + push).
> Design is NOT new: it consolidates the already **owner-approved** architecture
> (`docs/architecture-plan.md` v1.1 Level 2 Flow A/B) and the **ACCEPTED** ADR-001 (durable
> outbox), ADR-002 (send-policy kill switch), ADR-004 (structural mutation safety), ADR-005
> (config-driven decisions). No new architecture is invented; decisions below use the
> already-recorded client answers (round 1–3).

## Goal

When a Shopify order is created, WhatsApp the customer a **Confirm/Cancel** template; a
**Confirm** tap tags the order, a **Cancel** tap double-checks then cancels it — cutting COD
losses. This replaces WATI's outbound flow. Inbound conversation (Phase 4) is already live.

## What already exists (build ON these, do not rebuild)

- **Outbox row**: `outbound_messages(id, dedupe_key UNIQUE, state, kind, phone_e164, payload_json,
  template_wamid, delivery_status, attempts, last_error_code, ...)`. Phase 2 already QUEUES a row
  per order (`dedupe_key='order_created:{gid}'`, `payload_json={template, language, customer_name,
  order_name, amount}`). **Nothing drains it yet.**
- **Sender**: `send_template(...)` (with `button_payloads`) and `send_buttons(...)` (interactive
  reply buttons) — `app/channels/whatsapp_sender.py`. `SendResult(ok, status_code, wamid, error)`.
- **Inbound parser**: `InboundButton(payload, ...)` = template quick-reply tap; `InboundInteractive(
  button_id, ...)` = interactive reply-button tap — `app/channels/whatsapp_inbound.py`.
- **Shopify mutations**: `ShopifyClient.add_tags(auth, tags)`, `cancel_order(auth) -> CancelRequested`
  — both require an `AuthorizedOrder` (ADR-004). `get_order(gid)` for live re-fetch. `Order` has
  `is_cancelled()`, `fulfillment_status`, `tags`.
- **Ownership**: `AuthorizedOrder(order, verified_phone)` raises unless the phone matches the order
  (ADR-004). Only `core/order_resolver` issues it. `resolve_by_phone`/`resolve_by_order_name` exist.
- **Copy**: `app/channels/copy.py` — deterministic 4-language strings (confirm/cancel/error…).
- **Config**: `AdminControls` (`send_mode`, `allowlist_phones`, `push_policy`, `tags:{pending,
  confirmed,cancelled}`, `default_language`) via `load_controls`. `WhatsAppConfig` via
  `load_whatsapp_config`. Container has `shopify`, `ingest`, `config`, `http`, `messages`.
- **Jobs**: `app/jobs/router.py` registers `ensure_subscription`, `retention_purge` behind
  `X-Cron-Secret`. Add `outbox_drain` the same way.
- **Webhook**: `app/channels/whatsapp.py` verifies HMAC + dedupes (`MessageStore.record_if_new`) +
  routes `InboundText` → `run_turn`. Button events currently fall through (marked "Phase 5").

## Non-negotiable safety invariants (CLAUDE.md + ADRs)

1. **The LLM is NOT in the mutation path.** Button taps dispatch **deterministically** — no agent,
   no Gemini. (Free-text "cancel" stays in Phase 4, which only ever *sends a button*.)
2. **Ownership before acting** (ADR-004, Rule 3): every button tap re-fetches the order live and
   constructs an `AuthorizedOrder` (phone of the tapper must match the order) BEFORE any tag/cancel.
   A non-owner or unknown gid → generic refusal, no mutation, no order detail leaked.
3. **Always re-fetch live state** before acting (Rule 5.4): never act on the outbox snapshot.
4. **Two-phase cancel** (ADR-004 #3, Q4): Cancel tap → re-fetch → send "Are you sure?" reply buttons
   → only the **confirm** tap calls `orderCancel`. Provisional tag on request; final `cancelled`
   tag only after a re-fetch confirms `cancelledAt`.
5. **Cancel-before-dispatch only** (round-3 Q9): refuse to cancel an order whose `fulfillment_status`
   shows it is fulfilled/dispatched → reply "already shipped, contact support" (handoff), no mutation.
6. **Idempotency**: Meta `message_id` dedupe (`MessageStore`, already at the webhook) + re-fetch
   state check (already-confirmed/already-cancelled → a "that's already done" reply, never a second
   mutation). Every mutation is recorded in `order_actions` (audit trail).
7. **Kill switch** (ADR-002): the outbox drain enforces `send_mode` in ONE place —
   `off` = don't send (rows wait), `shadow` = record would-be as `suppressed`, `allowlist` = send
   only to allowlisted phones (else `suppressed`), `live` = send. Default `off`.
8. **Never crash a signed webhook** (Rule 5.5): a button tap that errors must still ack 200 and
   degrade to a safe reply / retry — never a 5xx.
9. **No secrets** logged/echoed; Meta error bodies already redacted by `_safe_error`.

## Button payload vocabulary (minted by us; deterministic)

- Template (Phase 2/drain mints these on the `order_confirmation_cod` quick-reply buttons):
  `order:confirm:{gid}` · `order:cancel:{gid}`
- Two-phase cancel reply buttons (minted by the Cancel handler via `send_buttons`):
  `order:cancel:confirm:{gid}` · `order:cancel:abort:{gid}`

Parsing is exact-prefix + a gid that must round-trip through `resolve_by_gid` (re-fetch +
ownership); a malformed/foreign payload → ignored with a safe generic reply, never a 5xx.

## Flow A — Order push (outbox drain job)

```
cron → GET/POST /internal/jobs/outbox_drain (X-Cron-Secret)
  load_controls(); if send_mode == "off" → return {drained: 0, reason: "send_mode off"}
  claim N queued outbound rows (state='queued', oldest first, attempt cap)
  for each row:
    gid = gid_from_dedupe_key(row.dedupe_key)          # 'order_created:{gid}' → gid
    policy = send_policy(send_mode, allowlist, row.phone)   # send | suppress
    if policy == suppress: mark_suppressed(row); continue
    cfg = load_whatsapp_config(); params from payload_json (name, order_name, amount)
    r = send_template(order_confirmation_cod, language, params,
                      button_payloads=[order:confirm:{gid}, order:cancel:{gid}])
    r.ok           → mark_sent(row, r.wamid); set_mapping_status(gid, 'template_sent')
    retryable 4xx  → bump_attempts(row, code); (state stays 'queued' until attempt cap → 'failed')
    undeliverable  → mark_undeliverable(row, code)   # e.g. 131026
```
`dedupe_key UNIQUE` = exactly one push per order ever (ADR-001). Self-invoke-after-webhook is
NOT added now (cron cadence is enough for v1); noted as a latency follow-up.

## Flow B — Button taps (deterministic dispatch, no LLM)

New module `app/core/order_actions.py` — `dispatch_button(c, event) -> None`. Called from
`whatsapp.py` for `InboundButton`/`InboundInteractive` (gated on `send_mode != 'off'`, same as
`run_turn`; a paused/handed-off conversation still processes a button — a deterministic tap is
not a "conversation"). Every branch re-fetches + ownership-checks via a NEW
`resolve_by_gid(shopify, wa_id, gid) -> AuthorizedOrder | None`.

| Tap (payload/button_id) | Deterministic handling |
|---|---|
| `order:confirm:{gid}` | resolve_by_gid → None: generic refusal. cancelled: "already cancelled". already-confirmed (tag present): "already confirmed". else: `add_tags(confirmed_tags)` → `order_actions` → reply confirm-success (copy, order language) → mapping `confirmed`. |
| `order:cancel:{gid}` | resolve_by_gid → None: refusal. cancelled: "already cancelled". **fulfilled/dispatched**: "already shipped, contact support" (handoff copy), NO mutation. else: `send_buttons("Are you sure?", [Yes→order:cancel:confirm:{gid}, No→order:cancel:abort:{gid}])`. NO mutation yet. |
| `order:cancel:confirm:{gid}` | resolve_by_gid → None: refusal. cancelled: "already cancelled". fulfilled now: "already shipped…". else: `cancel_order(auth)` → provisional tag `cancel_requested_tags` (e.g. `bot-cancel-requested`) → `order_actions` → reply cancel-requested (copy) → mapping `cancel_requested`. Final `cancelled` tag applied by the reconciliation step below. |
| `order:cancel:abort:{gid}` | reply "kept your order" (copy). No mutation. |
| unknown/foreign payload | ignored → safe generic reply. |

**Two-phase final tag**: `orderCancel` is async (returns a job). The `outbox_drain` job (or a
small `reconcile_cancels` step folded into it) re-fetches orders in state `cancel_requested`; when
`cancelledAt` is set it adds the final `cancelled_tags` and sets mapping `cancelled`. Keeps a
false `cancelled` tag from ever being written before Shopify actually cancels (ADR-004 #3).

## Data / store additions (additive; base + in-memory + Postgres)

`IngestStore` (or a sibling `OutboxStore`) gains — implemented in memory + Postgres:
- `claim_queued_outbound(limit) -> list[OutboundClaim]` (id, dedupe_key, phone_e164, payload_json, attempts) — `state='queued'`, oldest first. (Simple `SELECT ... ORDER BY created_at LIMIT` + per-row state transition; single-instance cron, low volume 100–500/day, so no `FOR UPDATE SKIP LOCKED` needed for v1 — noted.)
- `mark_outbound_sent(id, wamid)` · `mark_outbound_suppressed(id)` · `mark_outbound_undeliverable(id, code)` · `bump_outbound_attempt(id, code, max_attempts) -> 'queued'|'failed'`.
- `set_mapping_status(order_gid, status)` — drives the `order_mappings.status` enum.
- `record_order_action(order_gid, action, actor_wa_id, source_wamid, result, user_errors_json)` — audit row in `order_actions`.
- `orders_awaiting_cancel_reconcile(limit) -> list[gid]` — mappings in `cancel_requested`.
No schema change needed (all tables exist); if a helper column is missing, add it idempotently to
`schema.sql`.

## Config used (ADR-005; already present)

`send_mode`, `allowlist_phones`, `push_policy` (eligibility already applied at ingest),
`tags.{confirmed,cancelled}` (+ a `cancel_requested`/pending provisional tag — add to the tags
model if absent, default `["bot-cancel-requested"]`), `default_language`. Template name
`order_confirmation_cod`; language = order `customer_locale` → `default_language`.

## Testing (TDD, mocks; Postgres gated on TEST_DATABASE_URL)

Drain: send/suppress per each send_mode; allowlist gating; retryable vs undeliverable Meta codes;
dedupe/exactly-once; gid parsed from dedupe_key; mapping status transitions. Buttons: ownership
refusal (non-owner gid → no mutation, no leak); confirm tags + idempotent re-tap; cancel is
two-phase (first tap sends buttons, never cancels); confirm-cancel calls `orderCancel` +
provisional tag; abort no-ops; fulfilled order refuses cancel; already-cancelled/confirmed replies;
attacker-typed/foreign payloads never 5xx; audit rows written. Whatsapp webhook wiring: button
events dispatch, still ack 200 on handler error.

## Out of scope (deferred — see the "Phase 5 deferred / later" list in `_pipeline_status.md`)

Self-invoke-after-webhook latency optimization; `FOR UPDATE SKIP LOCKED` multi-instance drain;
literal-YES free-text cancel fallback (`pending_actions` TTL row) — buttons only for v1;
proactive shipped/cancelled push topics. These do not block the confirm/cancel flow working.

## Review gates

Sensitive surface (irreversible `orderCancel`, order mutations, send path): `developer` (TDD) →
`code-reviewer` → `security-reviewer` (mandatory) → fix → re-verify → push.
