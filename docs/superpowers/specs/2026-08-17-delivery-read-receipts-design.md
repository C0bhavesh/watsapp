# Delivery / Read Receipts (sub-project 1c) — Design

**Status:** Approved by owner (2026-08-17), via conversational brainstorming.

## Problem

The admin chat page currently shows a template-sent message as either delivered-to-Meta-successfully or not (`status`: queued/sent/suppressed/failed/undeliverable) — but WhatsApp itself separately reports whether a successfully-sent message was actually **delivered to the customer's phone** and whether the **customer read it**, via a distinct part of the webhook payload (`value.statuses`, alongside `value.messages`) that this codebase currently ignores entirely. The owner wants WhatsApp-style tick marks (1 grey / 2 grey / 2 blue) on the chat page, matching the real WhatsApp app, for BOTH template sends and the AI's free-text replies.

## What already exists (verified by reading the code, not assumed)

- `outbound_messages.template_wamid` (schema.sql:39) is already populated on every successful template send via `mark_outbound_sent(row.id, result.wamid)` (`app/jobs/outbox_drain.py:294`) — the correlation key for template-send status updates already exists, no new capture needed for that path.
- `outbound_messages.delivery_status` (schema.sql:40) already exists as a column but is **never read or written by any app code** (confirmed via full-codebase grep) — this feature is its first consumer.
- `messages` (the AI-chat table, schema.sql:105-112) has NO `wamid`/`delivery_status` columns today — these must be added (additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, matching this file's own established idiom, e.g. schema.sql:94, 207-208, 216-217).
- The webhook body Meta POSTs to `/webhook/whatsapp` (`app/channels/whatsapp.py::receive_webhook`) already carries `value.statuses` in the same envelope structure as `value.messages` — `app/channels/whatsapp_inbound.py::extract_events` only ever reads `value.get("messages")`, so status events are silently dropped today, not merely unparsed.
- `WhatsAppSendError`/`SendResult.wamid` (`app/channels/whatsapp_sender.py`) is already returned by every send call (`send_text`, `send_template`, `send_buttons`) — the AI-reply path (`core/conversation.py:213`, `result = await send_text(...)`) already has the wamid in hand, it's just never persisted anywhere today.
- `ConversationStore.append_message(conversation_id, role, content) -> None` (`app/store/base.py:292`) is called from `persist_turn` (`app/core/memory.py:28-32`) BEFORE the actual `send_text()` call in `core/conversation.py` (persist happens at line 205, send at line 213) — the assistant message row must be written first (send_mode gating happens between persist and send), so its wamid can only be attached in a follow-up write after send succeeds, not at persist time.
- `schema.sql` is applied manually against the production database (no code path in the repo executes it) — the same manual-application pattern already used for today's data-repair SQL.

## Design

### 1. Schema additions (owner-run, manual — same pattern as today's repair SQL)

```sql
ALTER TABLE messages ADD COLUMN IF NOT EXISTS wamid text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery_status text;
```

`outbound_messages.delivery_status` needs no schema change — it already exists, unused.

### 2. Capturing wamid for AI replies

`ConversationStore.append_message`'s return type widens from `None` to `int` (the new row's id) — a non-breaking widening since every current caller already ignores the return value. `persist_turn` (`core/memory.py`) returns the assistant message's new id. `core/conversation.py`'s `_run_turn`, after `send_text()` succeeds (result.ok), calls a new store method `set_message_wamid(message_id: int, wamid: str) -> None` to attach it — matching this codebase's established "narrow the shared method, give the side effect its own explicit verb" pattern from today's earlier `touch()` fix. Both `postgres.py` and `memory.py` implement all new/changed methods identically, per this project's dual-store-implementation convention.

### 3. Parsing Meta's status events

New dataclass `InboundStatus` (`app/channels/whatsapp_inbound.py`): `wamid: str`, `status: str` (one of `"sent"|"delivered"|"read"|"failed"`), `timestamp: str`. New function `extract_statuses(payload, expected_phone_number_id) -> list[InboundStatus]`, mirroring `extract_events`'s exact structure (same tenant guard, same attacker-typed-payload defensive parsing, same "skip unparseable, never raise" discipline) but walking `value.get("statuses")` instead of `value.get("messages")`.

### 4. Applying a status update

New function `apply_delivery_status(c: Container, status: InboundStatus) -> None` (new module, or added to an existing one — implementer's call, likely `app/core/delivery_status.py` given it's used from the webhook handler and doesn't belong in `channels/` per this project's layering rules). Looks up the wamid in `outbound_messages.template_wamid` first; if not found, tries `messages.wamid`; if found in neither, no-ops (a status update for a message this app didn't send, or sent before this feature existed — never an error). Applies an ordering guard before writing: the rank order is `sent(0) < delivered(1) < read(2)`, with `failed` as a distinct terminal state that always overwrites (WhatsApp's own "this definitively did not go through" signal always wins). A new status only overwrites the stored one if it ranks higher, or if the stored value is `NULL`. This makes reprocessing the exact same status idempotent (equal rank never triggers a write) and out-of-order delivery safe (a late "delivered" arriving after "read" is already recorded is silently dropped).

New store methods (both `postgres.py`/`memory.py`, added to whichever store protocol/class already owns the underlying table):
- `IngestStore.apply_outbound_delivery_status(wamid: str, status: str) -> bool` (returns whether a matching row was found, so the caller can try the other table next).
- `ConversationStore.apply_message_delivery_status(wamid: str, status: str) -> bool` (same, for `messages`).

Both implement the same ordering-guard logic in one place — implementer's call whether that's a shared pure function (`_should_apply(current, new) -> bool`) called from both, or duplicated with a matching-behavior test in each store (this project already has isolated dual-store logic in several places; follow whichever existing precedent looks cleanest once the implementer is in the code).

### 5. Wiring into the webhook handler

`app/channels/whatsapp.py::receive_webhook`, after the existing `events = extract_events(...)` block and its processing loop: also call `statuses = extract_statuses(payload, expected_phone_number_id=cfg.phone_number_id)`, and for each, call `apply_delivery_status(c, status)`. No new idempotency table is needed — the ordering-guard UPDATE is naturally idempotent on reprocessing, unlike the message-processing path which needs `record_if_new` because re-running `run_turn`/`dispatch_button` would have real side effects (an LLM call, a mutation); applying the same delivery-status rank twice is a no-op by construction. Errors in status processing must never fail the webhook's 200 ack (same discipline already applied to `dispatch_button`'s try/except at `whatsapp.py:141-146`) — wrap the status-processing loop the same way.

### 6. Admin API

`get_conversation_thread()` (`app/admin/router.py`): `template_sent` entries gain a `"delivery_status"` field (from `outbound_messages.delivery_status`, alongside the existing `"status"` field which stays as-is — `status` is the SEND-ATTEMPT outcome, `delivery_status` is Meta's post-send confirmation, two different dimensions). `ai_reply` entries — which currently carry no `status`/`delivery_status` at all — gain `"delivery_status"` (from `messages.delivery_status`) but still no `status` field (an AI reply's "did we even successfully call the WhatsApp API" outcome isn't currently tracked/exposed anywhere in the admin view and stays out of scope here — only the post-send delivery/read confirmation is being added).

### 7. Frontend

`renderBubble()` (`chats.js`): for any entry where a tick makes sense — `template_sent` with `status === "sent"`, or any `ai_reply` (which has no `status` field to gate on, since AI replies aren't currently modeled with a send-attempt-outcome state) — render ticks to the right of the timestamp based on `delivery_status`:
- `null`/absent → 1 grey tick (✓, sent but not yet confirmed delivered)
- `"delivered"` → 2 grey ticks (✓✓)
- `"read"` → 2 blue ticks (✓✓ in blue)
- `"failed"` → a red exclamation mark, replacing the ticks entirely (matches real WhatsApp's own visual convention for this case)

`template_sent` entries whose `status` is `suppressed`/`failed`/`undeliverable`/`queued` keep their EXISTING red-text-label treatment (already shipped) — no ticks render for these, since the message never reached a state where Meta could report delivery/read on it.

## Out of scope (YAGNI)

- A send-attempt-outcome (`status`) field for AI replies — only delivery/read tracking is added for that path, not a redesign of how AI-reply failures are surfaced.
- Group/broadcast message status fan-out — this app is 1:1 messaging only, Meta's `statuses` array is always single-recipient here.
- Historical backfill of delivery/read status for messages sent before this feature ships — those simply show 1 grey tick forever (no status info was ever captured for them), same as the "no info" default state.
- Any change to `core/order_actions.py` — untouched, this is a read-only status-tracking feature layered on top of the existing send paths, not a mutation-path change.
- Any change to the button-tap (`send_buttons`) path's delivery tracking — button-tap PROMPTS (the Confirm/Cancel messages) aren't rendered as their own bubble type in the admin thread view today (only the resulting `button_tap` action is), so there's no bubble to put ticks on for that path.

## Testing

- `extract_statuses`: parses a well-formed `statuses` array into `InboundStatus` objects; tenant guard rejects a mismatched `phone_number_id` identically to `extract_events`; malformed/type-confused entries are skipped, never raised.
- `apply_delivery_status`: a wamid found in `outbound_messages` updates that row and does not touch `messages`; a wamid found in `messages` updates that row and does not touch `outbound_messages`; a wamid found in neither is a silent no-op; ordering guard — `sent→delivered→read` applies each step, `read→delivered` (out-of-order) is rejected, `delivered→failed` and `read→failed` both apply (failed always wins), re-applying the identical status twice is idempotent (second call is a no-op, verified by unchanged `updated_at` or an explicit "not applied" return).
- Webhook integration: a delivery confirmed via `receive_webhook` end-to-end updates the correct row; a status-processing exception is swallowed and the webhook still 200s (matching the existing `dispatch_button` exception-swallowing test pattern).
- `append_message`'s widened return type: existing callers unaffected; `persist_turn` returns the assistant message's real id.
- Admin API: `template_sent`/`ai_reply` entries carry `delivery_status`; absent/null when no status has ever been reported.
- Frontend smoke tests (existing convention — served-JS substring assertions): tick-rendering logic present, red-exclamation-for-failed present, existing red-text-label paths unchanged for suppressed/failed/undeliverable `status` values.

## Global constraints (already binding, restated for this feature)

- Admin-only surface unaffected — no auth changes; this feature only touches the WhatsApp webhook (already HMAC-verified, already tenant-guarded) and the existing admin thread endpoint (already `require_admin`-gated).
- Webhook integrity: this feature adds NO new idempotency requirement beyond what's already true (status application is idempotent by construction via the ordering guard) — the existing `record_if_new`/message-id dedup for the `messages` path is untouched, unmodified, and this feature does not weaken it.
- `core/order_actions.py` untouched.
- No new secrets, no new external API calls (all data arrives via the existing, already-verified webhook).
- Given this touches webhook-parsing code (a CLAUDE.md-designated sensitive surface), a `security-reviewer` pass is required after `code-reviewer`, in addition to the standard review, before this deploys.
