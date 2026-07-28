# Security Review — Phase 1 backend (2026-07-28)

> Reviewed commits `2a4f0f3..ae94609`. Verdict: **PASS WITH NOTES**. Findings verified with
> mock-transport PoCs, not asserted from reading. Fixes dispatched same day (see pipeline).

## Verified clean (the primary surface)

- Secret-pattern grep over the working tree **and full git history**: EMPTY. No `.env` tracked;
  `.gitignore` covers `.env`, `.env.*`, `*.pem`. Docs contain placeholders only.
- `SecretVault`: invalid master key → `VaultError` without echoing the key; junk ciphertext →
  `VaultError("decryption failed")` with no `InvalidToken`/`binascii` leakage.
- `client_id`, `client_secret`, access token all persisted via Fernet (`set_secret`); only
  `token_expires_at` is plaintext (correct).
- Zero `logging`/`logger` calls in `app/` — no log-leak surface. Only `print()`s are in the
  dev-only smoke script.
- `TokenGrantError` messages carry status code only; `_graphql` errors never include the token.
- The `ae94609` order-name guard is correct and also rejects unicode-digit bypasses.
- Tests use synthetic values only; per-test `Fernet.generate_key()`.
- No CORS middleware (correct default posture).

## HIGH (fixed 2026-07-28)

**H1 — Search-operator injection in `find_customer_orders_by_phone`** (`client.py:123-137`).
`phone_e164` interpolated unguarded; PoC sent `phone:+91… OR email:victim@example.com`. The
sibling `find_order_by_name` was hardened in `ae94609`; this one was missed. Sits directly on
the Phase 3 ownership path → a broadened query resolves a DIFFERENT customer, whose orders are
then treated as the sender's (PII disclosure + wrongful cancellation).
Fix: E.164 regex guard + numeric-only customer id validation, both returning `[]` with no HTTP.

**H2 — `AuthorizedOrder` had no runtime invariant** (`models.py:38-43`). ADR-004's
"structurally impossible" was enforced only by a docstring; PoC forged an `AuthorizedOrder` for
a victim order and `cancel_order` accepted it. Fix: `__post_init__` asserting `verified_phone`
matches one of the order's phones. (Ripples fixed: mutation tests + smoke script fixtures.)

## MEDIUM (fixed)

- **M1** OpenAPI `/docs`, `/redoc`, `/openapi.json` publicly served (verified 200); harmless now
  but would auto-publish webhook paths + button payload shapes in Phases 2–3. Fix: disabled when
  `app_env == "prod"` (first real consumer of `app_env`).
- **M2** Smoke script printed full `Order` objects (email + 3 phone fields) — DPDP-relevant once
  `read_customers` is granted. Fix: counts and order names only.
- **M3** Smoke script fired real `tagsAdd`/`orderCancel` at the production store, safe only
  because `gid://shopify/Order/1` happens not to exist. Fix: gated behind `SMOKE_ALLOW_MUTATIONS=1`.

## LOW (fixed)

- **L1** Token print emitted `shpat_` + 4 real chars ("masking" that wasn't). Fix: length only.
- **L2** Corrupt `token_expires_at` → uncaught `ValueError` inside the lock = **permanent**
  token-acquisition outage on the <5s webhook path. Fix: parse guarded, falls through to grant.
- **L4** ACCESS_DENIED detection keyed on the English substring of a vendor message. Fix: match
  `extensions.code == "ACCESS_DENIED"` with the substring as fallback.
- **INFO-1** `httpx.AsyncClient` relied on implicit `follow_redirects=False` (the only thing
  preventing token replay to a redirect target). Fix: set explicitly.

## Deferred to Phase 2/3 planning (tracked, not fixed now)

- **L3** GraphQL `errors` are dropped when partial `data` is present — an order can return null
  phones due to a *scope* error rather than genuine absence, degrading the ownership check input.
  Fails closed (no phone → no match) but hides misconfiguration. Surface errors alongside data.
- **INFO** `ConfigService` has no key classification — nothing prevents `set_plain` storing a
  secret in plaintext. Add a `SECRET_KEYS` frozenset asserted in both setters + a masked accessor
  for the Phase 2 admin UI.
- **INFO** `database_url=""`/`app_env="dev"` defaults mean a missing prod env var silently yields
  dev posture → make `database_url` required in Phase 2, `app_env` a `Literal`.
- **INFO** Add ruff `"S"` (flake8-bandit) before the Phase 2/3 HMAC + admin-auth work.
- **INFO** No lockfile (all `>=` floors; every Vercel build re-resolves) and no `.vercelignore`
  (tests/ + scripts/ ship in the bundle, including the live-mutation smoke script).
- Token grant frame locals hold `client_id`/`client_secret` — adopt the cafe project's `_redact()`
  before Phase 2 adds logging/APM (Sentry-style capture-with-locals would ship the secret).
