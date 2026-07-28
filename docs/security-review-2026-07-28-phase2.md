# Security Review — Phase 2 (webhook + outbox + Postgres), 2026-07-28

> Commits `b99e5f0..4fc59e7`. Verdict: **FAIL** (3 HIGH), all small and localized; fixes
> dispatched same day. First phase with a PUBLIC internet-facing surface, reviewed as the
> primary threat boundary. All findings verified with working PoCs against the ASGI app.

## What the reviewer confirmed is SOUND

The cryptographic and authorization core held up under active attack:
- HMAC is base64 over the **raw body** with a constant-time compare, executed **before** any
  other logic. Hex (Meta-scheme) and urlsafe-base64 forgeries rejected; missing/empty/
  whitespace signatures rejected.
- Jobs endpoint is genuinely fail-closed (503 when unset) and leaks **no job-existence oracle**
  (auth is checked before job lookup: unauthenticated unknown job → 403, not 404).
- Idempotency's double-send protection **survives a forged-header replay**: because
  `dedupe_key` derives from the *signed* body (`order_created:{gid}`), a forged
  `X-Shopify-Webhook-Id` bypasses the dedupe table but still cannot queue a second message.
- Zero dynamic SQL — every asyncpg call is `$n`-parameterized. No injection, no auth bypass,
  no secret leakage, no PII disclosure found.
- Phase 1 fixes all still hold (AuthorizedOrder invariant, injection guards, follow_redirects,
  prod-docs-off); no secrets in the repo or in any of the 28 commits.

## HIGH — the gate failures (all fixed 2026-07-28)

**H1 — Non-ASCII header 500s the HMAC verifier** (`shopify_signature.py:12`).
`hmac.compare_digest(str, str)` raises `TypeError` on non-ASCII; Starlette decodes headers as
latin-1, so `X-Shopify-Hmac-Sha256: \xe9` reaches it. PoC: **500**. An unhandled exception in
the security-critical verifier, reachable by anyone who knows the URL. Fix: compare **bytes**.

**H2 — Same defect on the cron endpoint** (`jobs/router.py:36`). PoC: **500**. Same fix.

**H3 — Type-confusion 500s on SIGNED payloads → subscription-deletion risk**
(`shopify_orders.py`, `phone.py`). Four confirmed crashes: numeric `phone`, scalar `customer`,
non-list `payment_gateway_names`, non-str `customer_locale`. These fire on **Shopify's own
deliveries**, and a 500 makes Shopify retry the same poison payload for 48h, counting toward
the **19-consecutive-failure threshold that deletes the subscription**. Any Shopify payload-shape
change would silently kill the integration. Fix: defensive type coercion for every payload read.

## MEDIUM (fixed in the same pass)

- `json.loads` **RecursionError** escapes the `except ValueError` (3000-deep JSON → 500).
- **No body-size cap** — an 8 MB body buffered pre-auth (Vercel's 4.5 MB cap is host-specific).
- **VaultError → 500 on every webhook** if `APP_MASTER_KEY` is rotated/corrupt → 19 failures →
  subscription deleted. Fix: fail closed with 403.
- **`X-Shopify-Shop-Domain` never validated** — the client secret is per-*app*, not per-store; a
  second store installing the app could poison mappings and queue templates to numbers it owns.
- **No field-length caps** — a 200k-char `name` was stored and copied into `payload_json`
  (Meta template params cap at 1024 → guaranteed downstream send failure + DB bloat).
- **Unvalidated types into asyncpg text params** (`email`, `financial_status`) — masked entirely
  by the in-memory store in tests; would `DataError` → 500 → poison-retry loop on Postgres.

## LOW (fixed)

- asyncpg command tag parsed by suffix (`"INSERT 0 10".endswith("0")` is True) — also raised
  independently by the code reviewer.
- **`statement_cache_size=0` missing — imminent Supabase issue:** against Supabase's
  transaction-mode pooler (port 6543), asyncpg prepared statements intermittently raise
  `prepared statement "__asyncpg_stmt_N__" already exists` → 500s on the webhook.
- `cron_secret` had no minimum length; `GET` accepted for a state-changing job.
- `LazyPool.close()` mutated `_pool` outside the lock.

## Deferred (scheduled, not fixed in this pass)

- Pre-auth DB round-trip + no rate limiting: an unauthenticated flood exhausts the `max_size=5`
  pool and starves genuine deliveries (which then fail → subscription-deletion pressure).
  Fix: in-process TTL cache for the secret + short-circuit on malformed header + rate limit.
- Idempotency keyed on **unsigned** headers — derive from the signed body (hash/gid+topic) with
  `webhook_id` secondary; plus the >30d `processed_webhooks` retention job.
- `public_base_url` unvalidated: once the Phase 3 admin UI can write config, an attacker with
  config-write could repoint Shopify's order deliveries (full PII) at their own host. Require
  https + domain allowlist.
- `app_env` defaults to `dev` → a missing `APP_ENV=prod` silently publishes `/docs`.
- **DPDP:** `outbound_messages.payload_json` stores a *second copy* of customer name/order/amount;
  `email` is collected but unused in Phase 2. Minimize, or purge payload on successful send.
- **No logging anywhere in `app/`** — good for the no-secrets rule, but unauthenticated 500s and
  rejected signatures leave no trace, and there is no order-action audit trail. Add redacting
  structured logging before Phase 3.
- `apply_schema.py` is safe (only `CREATE ... IF NOT EXISTS`, no DROP/TRUNCATE/ALTER) but prints
  no target and has no confirmation; app uses one DSN with no least-privilege role.
