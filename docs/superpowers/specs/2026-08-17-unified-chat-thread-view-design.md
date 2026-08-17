# Unified Chat Thread View (Admin Panel) — Design

**Status:** Approved by owner (2026-08-17), via conversational brainstorming (not the visual companion).

## Problem

A number registered on WhatsApp Business Platform (Cloud API) cannot be logged into the WhatsApp
mobile app or added to Meta Business Suite's Inbox — that's a platform limitation, not a bug, and
it means there is currently no way to see what's been sent/received on this number anywhere except
the admin panel's flat `/admin/outbox` table (delivery status only, no message content) and the
recipient's own phone. The owner wants a real WhatsApp-style chat view in the admin panel instead.

This is the first of three sub-projects (owner-confirmed sequencing, 2026-08-17): this one (a
read-only unified thread per customer), then manual send (free text + template picker, reusing the
existing AI pause/resume mechanism), then tags (Shopify order tags + new custom conversation
labels). Only this first sub-project is designed here.

## What already exists (verified by reading the code and schema, not assumed)

- Three SEPARATE, unconnected data sources currently hold what would need to appear in one thread:
  1. `conversations`/`messages` tables — the free-text AI chat. `conversations.user_id` is the raw
     Meta `wa_id` (NO `+` prefix, e.g. `"919664290413"`), indexed
     `(user_id, last_active_at)` — already exactly what a thread-list-by-recency query needs.
     `messages.role`/`content`/`created_at` per conversation, written only by
     `app/core/conversation.py::persist_turn` (via `ConversationStore.append_message`).
  2. `outbound_messages` table — every template send this app has made (`cod_confirmation`,
     `prepaid_order`, `cod_confirmmsg`, `cod_cancel`, `order_shipped`, `order_delivered`).
     `phone_e164` here IS E.164 (`+91...`) — a DIFFERENT format from `conversations.user_id`.
     `payload_json` (the actual template name + params sent) already exists per row; `state`,
     `created_at`/`updated_at`, `template_wamid` also present. Nothing here is currently rendered
     as message content anywhere — `GET /admin/outbox` only exposes `OutboundView`'s flat fields
     (`dedupe_key`, `state`, `kind`, `phone_e164`, `attempts`, `last_error_code`, `created_at` — no
     `payload_json`).
  3. `order_actions` table — the Confirm/Cancel button-tap audit trail (`app/core/order_actions.py`).
     `actor_wa_id` here uses the SAME raw format as `conversations.user_id` (both are `event.wa_id`
     verbatim, no `+`) — no normalization needed between these two, only between either of them and
     `outbound_messages.phone_e164`.
- `app/core/phone.py::normalize_phone` already exists and is already used throughout the codebase
  (`order_resolver.py`, `reconcile.py`, the Q19/fulfillment-notifications work) for exactly this
  kind of format reconciliation — no new normalization logic needs inventing.
- `app/core/conversation.py`'s pause mechanism (`ConversationStore.pause_until`/`get_paused_until`)
  is TIME-BOUNDED (`pause_until(conversation_id, now + HANDOFF_PAUSE_WINDOW)`), set only when the
  AI itself decides to hand off — there is currently NO admin-panel exposure of this at all (not in
  `admin/router.py`'s endpoint list). Out of scope for THIS sub-project (belongs to the "manual
  send" sub-project, which will reuse this exact mechanism rather than inventing a new one), but
  confirmed here as a real existing capability the next sub-project builds on.
- The admin panel is plain static HTML/JS (`app/admin/static/{index.html,admin.js}`), served via
  `StaticFiles` in `app/main.py`. Existing pattern: a small `api(method, path, body)` fetch helper,
  a generic `fillTable(tableId, rows, cols)` renderer for flat tables (used by the existing
  `/admin/mappings`/`/admin/outbox` views in `loadViews()`), no framework, no build step.

## Design

### 1. A new merged-timeline endpoint

`GET /admin/conversations/{wa_id}` (admin-authenticated, matching every other `/admin/*` route)
queries all three sources for one customer and returns them as one time-ordered list:

- `messages` filtered by that `wa_id`'s `conversation_id`, via a NEW genuinely read-only lookup —
  NOT `ConversationStore.get_or_create`, which CREATES a conversation row on a miss (a side effect
  that must never fire just because staff viewed a thread — e.g. a customer who only ever received
  an `order_shipped` notification and never chatted with the AI has no conversation row at all, and
  opening their thread in the admin panel must not silently create a phantom empty one). The new
  method degrades to "no AI-chat entries for this thread" on a miss, never creates anything →
  entries typed `customer_message` (role `user`) or `ai_reply` (role `assistant`).
- `outbound_messages` filtered by `phone_e164 = normalize_phone(wa_id)` → entries typed
  `template_sent`, carrying the template name + parsed `body_params` from `payload_json` (not a
  reconstructed full message — see section 2) plus `state` (so a failed/suppressed send is visible,
  not just successful ones).
- `order_actions` filtered by `actor_wa_id = wa_id` (no normalization needed, same format) →
  entries typed `button_tap`, carrying `action` + `result`.

Each entry is normalized to a common shape: `{type: str, timestamp: str, text: str, meta:
dict[str, object]}` (`meta` carries type-specific extra fields — e.g. `template_sent`'s `meta`
includes the raw `body_params` dict for anyone who wants the full detail beyond the summary
`text`). The three query results are merged and sorted by `timestamp` in Python (no new SQL join
needed — the three tables have no foreign key relationship to join on directly; `wa_id`/`phone_e164`
is the only shared key, and it's already being used as the filter, not a join condition).

`GET /admin/conversations` (no id) lists threads for a sidebar: `SELECT * FROM conversations ORDER
BY last_active_at DESC LIMIT N` (already the exact index that exists), returning `wa_id`,
`last_active_at`, and a short preview (the most recent entry's `text`, truncated) per thread.

### 2. Template sends and button taps are summarized, not reconstructed

A `template_sent` entry's `text` is built from data already on the row —
`f"{template} → {', '.join(body_params.values() if dict else body_params)}"` (e.g. `"order_shipped
→ Suman B, tavas4119, Delhivery, https://track/..."`) — never the full approved message wording.
Rationale: Meta owns the actual approved copy for each template; hardcoding a reconstruction of it
in this codebase would drift the moment a template's wording is edited in Meta's Template Manager,
silently showing staff something the customer didn't actually receive. A `button_tap` entry's
`text` is `f"Tapped {action} → {result}"` (e.g. `"Tapped confirm → ok"`), reading directly off the
existing `order_actions.action`/`result` columns — no new interpretation logic.

### 3. Phone-format normalization + frontend

Normalization happens ONCE, server-side, inside the new endpoint (`normalize_phone(wa_id)` before
querying `outbound_messages`) — the frontend never sees or handles format differences. If
`normalize_phone` returns `None` (an unparseable `wa_id` — shouldn't happen for a real Meta-sourced
`wa_id`, but degrade gracefully rather than crash), the `outbound_messages` portion of the merge is
simply skipped for that thread (logged, not raised) — matching this codebase's established
graceful-degradation posture rather than failing the whole endpoint over one source.

Frontend: a new "Chats" section added to `app/admin/static/index.html` (thread-list sidebar) +
`app/admin/static/admin.js` gains `loadChatList()` (calls `GET /admin/conversations`, renders the
sidebar) and `loadChatThread(waId)` (calls `GET /admin/conversations/{wa_id}` on a sidebar click,
renders bubbles) — plain DOM manipulation matching the existing `fillTable`/`api` pattern, no new
framework or build step. Customer messages (`customer_message`) render left-aligned; everything
this app originated (`ai_reply`, `template_sent`, `button_tap`) renders right-aligned, each visibly
labeled by type (so staff can tell an AI reply from a template send from a button-tap event at a
glance) since they're visually on the same side but semantically different.

### Out of scope (YAGNI, deferred to later sub-projects or explicitly not built)

- Sending anything from this view — read-only. The next sub-project (manual send) builds on top of
  this thread view once it exists.
- Any tag display (Shopify or custom) — the third sub-project.
- Exposing/using `pause_until`/`get_paused_until`/AI on-off toggling from the admin panel — exists
  in the AI engine already, stays untouched here; the next sub-project reuses it.
- Real-time/live updates (websockets, polling) — the thread view loads on demand (sidebar click),
  matching the existing admin panel's manual-refresh pattern (`views-refresh` button); no
  auto-refresh in this sub-project.
- Pagination/infinite scroll — a fixed reasonable limit (e.g. most recent 100 entries per thread,
  most recent 50 threads in the sidebar) is enough for v1 given this store's order volume (100-500
  orders/day); revisit if it becomes a real constraint.
- Any change to how `messages`/`outbound_messages`/`order_actions` are WRITTEN — this sub-project
  is entirely a new READ path over already-existing, already-populated data.

## Testing

- The new read-only conversation lookup (section 1): a hit returns the right messages; a miss
  (no conversation row for that `wa_id`) returns empty AI-chat entries AND does not create a row
  (assert the conversation table's row count is unchanged after the call); a thread with entries
  from only 1 or 2 of the 3 sources (not all three) still merges correctly.
- Merge/sort correctness: entries from all 3 sources interleaved by timestamp, not grouped by
  source — a test seeding an AI message, then a template send, then a button tap (in that
  chronological order) must return them in that exact order, not source-grouped.
- Phone-format reconciliation: a thread where `conversations.user_id` (no `+`) and
  `outbound_messages.phone_e164` (`+91...`) refer to the same real number must merge correctly —
  proving the normalization actually connects them, not just that each query runs.
- `normalize_phone` returning `None`: the `outbound_messages` portion degrades to empty for that
  thread rather than raising; the other two sources still return normally.
- `GET /admin/conversations` (list): ordered by `last_active_at` descending, matching the existing
  index; requires admin auth (401/403 without a valid session, same as every other `/admin/*`
  route — reuse the existing `require_admin` dependency, no new auth logic).
- Template-summary rendering: a `body_params` dict (named) and a `body_params` list (positional,
  per the fulfillment-notifications feature) both render into a readable `text` string without
  crashing on either shape.

## Global constraints (already binding, restated for this feature)

- Admin-only surface — every new route requires `Depends(require_admin)`, matching every existing
  `/admin/*` endpoint exactly. No new auth mechanism.
- No new secrets, no schema/migration changes (all three source tables already exist and are
  already populated) — this is a pure read-time aggregation.
- No mutation of any kind — this sub-project touches core/order_actions.py's data only as a READ
  (querying `order_actions`), never writes to it or any mutation path.
