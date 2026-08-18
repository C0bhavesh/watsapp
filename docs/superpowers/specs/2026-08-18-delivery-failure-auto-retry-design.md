# Delivery-Failure Auto-Retry (sub-project 1e) — Design

**Status:** Approved by owner (2026-08-18), via conversational brainstorming. Design was revised twice mid-session after two real architectural corrections (documented below) — both caught before any code was written.

## Problem

The delivery/read-receipts feature (shipped 2026-08-18) shows a red "!" when WhatsApp reports a message failed to actually reach the customer's phone — but does nothing about it. The owner wants the app to automatically retry a failed delivery up to 3 times, and be alerted if all retries are exhausted.

## Corrections made during brainstorming (recorded for future reference)

1. **Retry timing cannot be "wait 5-10 minutes between attempts"** — this codebase has no working scheduler (`docs/memory/error_learnings.md`, ADR-001's amendment: the external cron that used to drive `outbox_drain` stopped working, and the fix was to move to a bounded inline `await` inside the triggering request, not a background job). There is nothing that could wake up minutes later to fire a delayed retry.
2. **A "delivery failed" report is not something learned synchronously when sending** — it arrives as its own independent, later WhatsApp status webhook (see `docs/superpowers/specs/2026-08-17-delivery-read-receipts-design.md`). This means retries do NOT need any artificial delay/sleep loop at all: resending gets a fresh wamid, and *that* attempt's own eventual delivery-failure report (if any) arrives as its own future webhook event, which is what naturally triggers the next retry. The retry mechanism is entirely event-driven, needs zero new scheduling infrastructure, and never blocks a request waiting for anything.

## What already exists (verified by reading the code, not assumed)

- `apply_delivery_status()` (`app/core/apply_status.py`) already runs whenever a status webhook reports `failed` for a wamid, after looking it up in `outbound_messages` (template sends) or `messages` (AI replies) — this is the exact hook point for triggering a retry.
- `outbound_messages`/`messages` both already store everything needed to resend: `payload_json` (template name/language/body_params/buttons/image_url, via the existing `_parse_payload` helper in `app/jobs/outbox_drain.py`) for template rows; `content` for AI-reply rows (recipient resolved via the row's `conversation_id` → `conversations.user_id`).
- `AdminControls.owner_alert_number` (`app/admin/controls.py:67`) and the existing "alert the owner via WhatsApp, degrade silently if unset, never raise" pattern (`core/conversation.py::_alert_owner`, private to that module) is the established precedent to mirror for the retry-exhaustion alert — not reused directly (it's private and hardcoded to the handoff-alert wording), but the same shape.
- `app/channels/whatsapp.py::receive_webhook` already loads `WhatsAppConfig` (`load_whatsapp_config`) but NOT `AdminControls` — this feature's wiring needs to also load `AdminControls` (`load_controls`) to reach `owner_alert_number` and the `send_mode`/`allowlist_phones` kill-switch gating a resend must respect (a resend is an outbound send like any other — `send_decision(controls.send_mode, controls.allowlist_phones, phone)` applies unchanged).
- `TURN_TIMEOUT_SECONDS = 55.0` (`core/conversation.py:50`) is this codebase's existing per-webhook-request time budget — retries fit inside this without needing their own budget math, since each retry is a single, non-blocking, immediate resend (no sleep), not a loop.

## Design

### 1. Data model

Add `retry_count int NOT NULL DEFAULT 0` to both `outbound_messages` and `messages` (additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, another owner-run manual migration alongside — or combined with, if not yet run — the delivery-receipts feature's own pending migration).

### 2. Trigger: inside `apply_delivery_status()`, when a `failed` status is actually applied

Immediately after `apply_delivery_status()` determines the incoming status is `"failed"` and the ordering guard actually applied it (i.e., this genuinely is a new failure, not a duplicate/out-of-order report already superseded), read the row's current `retry_count`:

- **`retry_count < 3`:** attempt one resend right now, using the row's exact original content (template payload / AI-reply text) to the same recipient, gated through the SAME `send_decision(controls.send_mode, controls.allowlist_phones, phone)` check every other outbound send uses. **`retry_count` increments by 1 in every case below** — it counts "a retry attempt was used," not "a retry succeeded." On a successful resend (got a wamid): increment `retry_count`, update the row's wamid to the NEW one, and reset `delivery_status` to `NULL` (a fresh send awaiting its own confirmation — the OLD wamid's `"failed"` history is superseded, not separately preserved). The next time WhatsApp reports on THIS new wamid (delivered/read/failed), it flows through the exact same `apply_delivery_status()` path — if it's another `failed`, this same logic fires again with `retry_count` now at 1, then 2, then 3.
- **A resend attempt itself fails to send, or is suppressed by `send_decision`** (not a later delivery failure — the send call itself errors out immediately, or the kill-switch blocks it, e.g. `send_mode` off/allowlist-miss): there is no new wamid, so no future webhook will ever report on it — nothing would ever trigger a further retry. Still increment `retry_count` (a retry attempt was used, even though it didn't produce a trackable send), then treat this the same as retries being exhausted: stop, go straight to the alert (below), regardless of what the incremented count actually reached.
- **`retry_count >= 3`** (this failure report is for the 4th total delivery failure — original send + 3 resends, all eventually reported failed): stop retrying. `delivery_status` stays `"failed"`. Send the owner alert (below).

### 3. Owner alert on exhaustion

A new small alert function (mirroring `_alert_owner`'s shape — reads `owner_alert_number`, degrades silently if unset, `send_text`, never raises, logs a warning on send failure) with its own message distinct from the handoff alert, e.g.: *"Thetavas bot: a message to {customer_phone} failed to deliver after 3 retries and was not sent. You may want to follow up another way."* Fires once, at the moment the 3rd retry's own failure is reported (or a resend attempt itself couldn't be sent) — not repeated on any further failure reports for the same already-exhausted row.

## Out of scope (YAGNI)

- Any delay/spacing between retries — explicitly not needed once the design correctly recognized retries are triggered by independent future webhook events, not a loop this app controls the timing of.
- Retrying a `suppressed`/`undeliverable`/send-attempt-failed row (the EXISTING `state` field on `outbound_messages`, distinct from `delivery_status`) — this feature only reacts to WhatsApp's own post-send delivery-failure reports, not send-attempt failures (`send_one_outbound`'s existing `bump_outbound_attempt` retry mechanism already covers that, unrelated and untouched).
- A UI control to manually trigger a retry, or to view retry history/count on the admin chat page — not requested, not built here.
- Fixing/setting up a working Vercel Cron scheduler — the owner explicitly chose the no-new-infrastructure path; revisiting the scheduler is a separate future decision if ever needed for something that genuinely requires waiting.
- Any change to `core/order_actions.py` — untouched, this is a send/notification-path feature only.

## Testing

- `apply_delivery_status()`: a `failed` report with `retry_count < 3` triggers exactly one resend attempt, increments `retry_count`, and (on success) resets `delivery_status` to `NULL` with the new wamid; a `failed` report with `retry_count >= 3` does NOT resend, fires the owner alert instead.
- The resend respects `send_mode`/`allowlist_phones` gating identically to every other outbound send (a suppressed resend still counts as a "used" retry attempt, going straight to alert-on-exhaustion logic, since a suppressed send will never get a wamid to hang a future retry off of — same handling as a synchronous send failure).
- A resend attempt that itself fails to send (not a later delivery-failure report) immediately triggers the owner alert without waiting for a 3rd count.
- The owner alert fires exactly once per exhausted row (not repeated), degrades silently when `owner_alert_number` is unset, and never raises even if the alert send itself fails.
- Both `outbound_messages` and `messages` code paths are covered identically (dual code path, mirroring this project's existing convention for the two delivery-tracking tables).

## Global constraints (already binding, restated for this feature)

- `core/order_actions.py` untouched.
- No schema/migration auto-applied by any code path — `retry_count` columns are owner-run manual DDL, same pattern as the delivery-receipts feature's own migration.
- Resends go through the exact same `send_mode`/`allowlist_phones` kill-switch gating as every other outbound send — no bypass.
- This touches webhook-processing code (`apply_delivery_status`, called from `/webhook/whatsapp`) — a `security-reviewer` pass is required after `code-reviewer`, same as the delivery-receipts feature.
