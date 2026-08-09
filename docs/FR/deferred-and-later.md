# Deferred & "Later" Work — consolidated list

> Created 2026-08-10 (owner asleep; requested "list out the other changes we can do later and
> save"). Single place for everything intentionally NOT done yet. Nothing here blocks the bot
> working end to end — it is hardening, polish, config, and future scope. Grouped by type.

## A. Owner config / setup to go live (NOT code — you do these in the panel/dashboards)

1. **Enter Shopify credentials** — `client_id` + `client_secret` in the admin panel (still "not
   configured"). Needed for order-status + product lookups + the confirm/cancel mutations.
2. **Sort the admin password** — currently riding a browser session; set a strong `ADMIN_PASSWORD`
   in Vercel + redeploy so future logins are clean.
3. **Point Meta's webhook** at the live URL — WhatsApp → Configuration → Webhook:
   Callback `https://thetavas-bot.vercel.app/webhook/whatsapp`, verify token
   `thetavas-vhw9TTsKGXL_8sYW-FmGNA`, subscribe to **messages**.
4. **Set the kill switch** — `send_mode` = `allowlist` + add your own number for testing (it is
   `off` = nothing sends). Move to `live` at cutover.
5. **Schedule the cron jobs** (Vercel Project → Cron, or the dashboard) to hit
   `/internal/jobs/outbox_drain` and `/internal/jobs/reconcile_cancels` (and the existing
   `ensure_subscription`, `retention_purge`) with the `X-Cron-Secret`. Without a schedule, queued
   order-confirmation messages won't send and cancels won't finalize their tag.
6. **Set the function `maxDuration`** (Vercel Project → Functions) comfortably above the outbox
   drain worst case (Rule 5 — deploy config, owner does it).
7. **Rotate the secrets shared in this chat** before real customers — regenerate the Meta access
   token + app secret in Meta, reset the Supabase DB password — then update them (panel/env). A
   chat transcript is not a secrets vault.

## B. Client decisions still open (you are the client — just answer)

- **Q6** — no-match fallback: share support contact only (recommended) vs also alert staff.
- **Q13** — order tags after switch: clean new + old "…by wati" during transition (recommended)
  vs keep old vs new-only.
- **Allowlist during pilot** — the inbound *conversation* path (Phase 4) still resolves + sends a
  non-allowlisted sender's own order to the LLM (reply suppressed). Decide strict isolation vs
  shadow-observe. NOTE: the *button* path (Phase 5) now fully honors shadow/allowlist after the
  review fix, so this only concerns the free-text conversation.

## C. Pre-deploy security hardening (before real customer traffic)

- **Postgres-backed login rate limiting** (round-3 decision) — the current `slowapi` limiter is
  in-process and ineffective on serverless; back it with Postgres (or Upstash) + escalating
  lockout, keyed on the forwarded client IP.
- **Session revocation / logout** — the 12h admin cookie can only be invalidated by rotating
  `APP_MASTER_KEY` (which would destroy all encrypted creds); add a separate signing secret +
  `session_epoch` + `POST /admin/logout`.
- **`public_base_url` host pinning** — it's the Shopify webhook callback; pin/allowlist so a
  compromised panel can't redirect order PII.
- (Audit logging + DPDP erasure/retention — already DONE, by the DPDP/audit workstream.)

## D. Phase 4 (conversation) cleanups — LOW, from the Phase 4 review

- **Dead `email` reveal toggle** — `reveal_fields` accepts "email" but no agent renders it; drop
  it from the allowed set or wire it (ties to Q5 reveal scope).
- **Unbounded message append during handoff** — a paused (handed-off) conversation appends every
  inbound with no per-conversation cap; add a size guard if abuse appears.

## E. Phase 5 (order push) follow-ups — LOW/hardening, from the Phase 5 review

- **Atomic outbox claim** — `claim_queued_outbound` is a plain SELECT (no `FOR UPDATE SKIP
  LOCKED`); safe for the single-instance cron at 100–500 orders/day, but add a claim-time
  `UPDATE ... RETURNING` to a `sending` state before running overlapping/multi-instance drains.
- **Duplicate-send window** — a function kill between a successful Meta send and the DB
  `mark_outbound_sent` re-sends the template next drain (accepted outbox tradeoff, ADR-001);
  mitigated by right-sizing `maxDuration` / `_CLAIM_LIMIT` (see A.6).
- **Self-invoke after webhook** — drain latency is currently the cron cadence; a fire-and-forget
  self-invoke after the 200 would make the push near-instant.
- **Literal-YES free-text cancel** — v1 is button-only; the `pending_actions` TTL fallback for a
  typed "YES" is deferred.
- **Proactive shipped/cancelled push topics** — `orders/fulfilled` / `orders/cancelled` templates
  (each a paid message) — future client decision.
- **`_is_dispatched` shared helper** — the cancel-eligibility rule lives in both
  `core/order_actions.py` and `agents/order_tracking.py`; factor into one helper so they can't
  drift (kept separate now to avoid touching two owners' files).

## F. Note to verify

- **Vertex model id** `vertex_ai/gemini-3.5-flash` (carried from the cafe) — verified live: the
  provider now activates. If a newer Gemini model is preferred, it's a one-line registry change.
