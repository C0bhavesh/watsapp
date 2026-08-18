# Admin Manual Reply (sub-project 1f) — Design

**Status:** Approved by owner (2026-08-18), via conversational brainstorming.

## Problem

The admin chat page (`backend/app/admin/static/chats.html`) is currently read-only plus a "Resume AI" control — there is no way for the store owner to type and send a free-text WhatsApp message to a customer directly from the panel, the way WhatsApp Web itself allows. This forces the owner to switch to WhatsApp Web/Desktop for any manual reply, losing the panel's order-context sidebar and delivery-status tracking.

## What already exists (verified by reading the code, not assumed)

- `core/conversation.py` already has a human-handoff pattern: when a customer explicitly asks for a person, the AI self-pauses for `HANDOFF_PAUSE_WINDOW` via `ConversationStore.pause_until(conversation_id, until)`, and the owner is alerted (`_alert_owner`). The admin panel already has a "Resume AI" button (`POST /admin/conversations/{thread_id}/resume`) that clears this pause early by setting `until = now`.
- `core/memory.py::load_history` only includes rows with `role == "user"` or `role == "assistant"` when building the AI's context window — any other role value is silently invisible to the model.
- The AI-reply send path (`conversation.py` around line 205-232) is the exact pattern to mirror: `persist_turn` (writes the row, returns its id) → `send_text(...)` → on a successful send with a `wamid`, `set_message_wamid(assistant_message_id, result.wamid)` — this is what hooks a row into the existing delivery-status tick marks (`apply_delivery_status`) and the delivery-failure auto-retry feature (`delivery_retry.py::retry_failed_message`), both of which operate generically on any `messages` row by wamid, regardless of how the row was created.
- `delivery_retry.py`'s `get_message_retry_info` query was recently hardened with `AND role = 'assistant'` (defense-in-depth, sub-project 1e's final review) — so a manual reply MUST be stored with `role = 'assistant'` to remain eligible for auto-retry, not a new role value.
- `GET /admin/conversations/{thread_id}` already returns all of a thread's messages (`entries`, chronologically sorted, each with a `timestamp` and `type`) to the frontend — the customer's most recent inbound message is already present in that payload, so the 24-hour WhatsApp customer-service-window check can be computed client-side with no new backend query.
- `require_admin` (existing dependency) and `_audit(...)` (existing audit-log helper, used by `resume_conversation`) are the established patterns for a new admin-gated mutation endpoint.

## Design

### 1. Data model

Store a manual reply in the existing `messages` table with `role = 'assistant'` (not a new role) — this keeps it in the AI's memory window (per owner's decision: the AI should stay consistent with what was already told to the customer) and keeps it eligible for the existing delivery-status/auto-retry machinery unchanged.

To let the UI distinguish "sent by the AI" from "sent by the admin" without touching the `role` column (which is load-bearing for memory/retry), add one new column:

```sql
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender text;
```

Additive, idempotent, owner-run manual migration (same pattern as every prior migration this project). `NULL`/absent means "AI" (all existing and future AI-generated rows); `'admin'` marks a manually-sent row. Nothing reads this column except the admin UI's rendering — it has zero effect on `core/memory.py`, `delivery_retry.py`, or any send-decision logic.

### 2. Backend: `POST /admin/conversations/{thread_id}/messages`

New admin-gated endpoint in `app/admin/router.py`, body `{"text": str}`. Flow:

1. Resolve `user_id` (phone) from `thread_id`, 404 if unknown (same pattern as `resume_conversation`).
2. Reject empty/whitespace-only `text` with 400.
3. `message_id = await c.conversations.append_message(thread_id, "assistant", text, sender="admin")` — `ConversationStore.append_message` gains a new optional keyword param `sender: str | None = None` (default preserves every existing call site's behavior unchanged: `NULL` in the DB, rendered as "AI" in the admin UI).
4. `result = await send_text(c.http, wa_cfg, user_id, text)` — **no `send_decision`/`send_mode` gate** (owner's explicit decision: a manual, targeted admin send is not what the kill switch is for).
5. On failure (`not result.ok` or a `WhatsAppSendError`): the row stays in `messages` with no wamid — visible in the admin UI as a message with no delivery status, since there is nothing to hang a tick mark or a retry off of without a wamid. Log a warning (existing pattern), return the failure to the admin UI so they see it immediately (this is a synchronous user-initiated action, unlike a background job — the UI should surface the failure inline, not silently swallow it).
6. On success with a wamid: `await c.conversations.set_message_wamid(message_id, result.wamid)` (existing method, existing best-effort try/except-and-warn wrapper, matching `conversation.py`'s exact pattern) — this is what makes tick marks and auto-retry "just work" with zero changes to `apply_status.py`/`delivery_retry.py`.
7. `await c.conversations.pause_until(thread_id, now + HANDOFF_PAUSE_WINDOW)` — reuses the exact same constant and mechanism as the existing human-handoff pause, so the AI stays quiet on this conversation for the same window, and the existing "Resume AI" button already works to clear it early with no changes needed there.
8. `_audit("admin_manual_reply", "success" | "failure", resource=f"thread:{thread_id}")`.

Return `{"ok": true, "wamid": ...}` on success (200) or `{"ok": false, "error": ...}` with a non-2xx status on send failure (exact status TBD in plan — likely 502, since the admin auth/request itself succeeded but the downstream WhatsApp send did not).

### 3. Frontend

- A message input + send button added to `chats.html`/`chats.js`, below the existing thread view (mirroring WhatsApp Web's layout, per the screenshots shown during brainstorming).
- **Enabled only when the customer's last message is within 24 hours.** Computed client-side: find the most recent `entries` item with `type === "customer_message"`, compare its timestamp to `Date.now()`. Outside the window, the box is disabled/greyed with a short inline note (e.g. "Outside the 24-hour reply window — send a template instead") rather than letting the send fail against Meta's API.
- On send: POST to the new endpoint, then reload the thread (same `loadThread()` refresh already used after "Resume AI") so the new message, its tick mark, and the now-active pause state all appear immediately.
- Manually-sent bubbles render with the `sender === "admin"` marker distinguished from AI replies (e.g., a small "You" label) — exact styling is a frontend-implementation detail for the plan, not a new design axis.
- On a send failure, show the error inline near the input (not just a console log) so the owner immediately knows to retry or switch to WhatsApp Web.

### 4. Behavior summary (from brainstorming decisions)

- Sending a manual message auto-pauses the AI on that conversation for `HANDOFF_PAUSE_WINDOW` (same constant/mechanism as the existing handoff pause; "Resume AI" already clears it).
- The 24-hour WhatsApp session window is enforced proactively in the UI (box disabled + explained), not discovered via a failed API call.
- Manual sends bypass `send_decision`/`send_mode` entirely — the kill switch governs automated/bot traffic, not a human's deliberate one-to-one reply.
- Manual sends get the same delivery tick marks and auto-retry-on-failure as AI replies, via the existing generic wamid-keyed machinery — no changes needed to `apply_status.py` or `delivery_retry.py`.
- The AI's memory includes manual replies (stored as `role = 'assistant'`), so a later AI-resumed reply stays consistent with what the admin already told the customer.

## Out of scope (YAGNI)

- Sending a template message from this box — the existing order-confirmation/template-push flow already covers that; this feature is specifically the free-text "type and hit send" gap.
- Any change to `core/order_actions.py` — untouched, this is a send/notification-path feature only.
- Rich media (images/attachments) in the manual-reply box — text only, matching the literal ask.
- Any new rate limiting or additional auth beyond the existing admin login — the owner is already a trusted actor via `require_admin`.

## Testing

- `POST /admin/conversations/{thread_id}/messages`: 401 without admin auth; 404 for an unknown `thread_id`; 400 for empty/whitespace text; on a successful send, the row is persisted with `role="assistant"`, `sender="admin"`, and a wamid; the conversation's `paused_until` is set to roughly `now + HANDOFF_PAUSE_WINDOW`; the response is `{"ok": true, ...}`.
- On a send failure (mocked `WhatsAppSendError` or a non-ok `SendResult`): the row is still persisted (visible in the thread) but has no wamid; `paused_until` is still set (the admin's intent to take over stands even if this particular send failed); the response reports the failure with a non-2xx status.
- Confirm no `send_decision`/`send_mode`/`allowlist_phones` check gates this path (a dedicated test with `send_mode="off"` still successfully sends, unlike every other outbound path in this codebase).
- Confirm a manual reply is included when `core/memory.py::load_history` builds context for a later AI turn on the same conversation (role="assistant" is picked up exactly like an AI-generated row).
- Confirm `delivery_retry.py::retry_failed_message` successfully resends a manually-sent row that later gets reported `failed` (it must pass the `role = 'assistant'` filter added in `get_message_retry_info`).
- Frontend smoke tests (existing style, markup/JS-substring assertions): the message box and send handler exist on the page; the 24h-window disable logic is present in the JS.

## Global constraints (already binding, restated for this feature)

- `core/order_actions.py` untouched.
- No schema/migration auto-applied by any code path — `messages.sender` is owner-run manual DDL, same pattern as every prior migration.
- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- This touches an outbound-send path — a `security-reviewer` pass is required after `code-reviewer`, same as every other feature that sends real WhatsApp messages.
