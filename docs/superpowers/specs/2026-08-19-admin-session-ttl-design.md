# Admin Session TTL Extension — Design

> Client-directed (Q20/A, `docs/FR/client-decisions-all.md`), owner confirmed 30-day duration. Approved 2026-08-19.

## Problem

The admin chat panel's login session (`admin_session` cookie, signed via `app/admin/auth.py::issue_token`) lasts 12 hours (`issue_token`'s `ttl_hours` default, mirrored by a manually-kept-in-sync `max_age=12*3600` on the cookie in `app/admin/router.py::login`). The owner wants a much longer session so re-login is rare on a device already in use, without weakening the password requirement itself or making the session apply across devices (impossible for a browser cookie regardless — a new device always logs in once).

## Decision

Client chose option A (extend session duration, keep the password) over B (bookmarkable long-lived link token) and C (remove the password). Owner confirmed **30 days**.

## Design

Single source of truth for the duration, replacing the two independently-maintained magic numbers (`issue_token`'s default `ttl_hours: int = 12` and `login`'s `max_age=12 * 3600` comment-linked to it) with one named constant:

`app/admin/router.py`: `ADMIN_SESSION_TTL_HOURS = 24 * 30` (720 hours = 30 days), used at both call sites — `issue_token(settings.app_master_key, _now(), ttl_hours=ADMIN_SESSION_TTL_HOURS)` and the cookie's `max_age=ADMIN_SESSION_TTL_HOURS * 3600`. This removes the "keep in sync" comment's fragility (a future duration change is a one-line edit, not two independently-drifting numbers) while changing no other behavior: `issue_token`'s `ttl_hours` parameter and default already existed and are unchanged in signature; only the call site now passes an explicit value instead of relying on the default.

No change to `verify_token`, `check_password`, the cookie's other flags (`httponly`, `samesite=strict`, `secure` derived from `app_env`, `path=/admin`), or the login rate limit (`5/minute`).

## Security consideration (why 30 days is acceptable here)

A stolen/lost device with an already-open session becomes a larger window of risk (30 days vs 12 hours) — this is the direct trade-off the client accepted in exchange for convenience. Mitigating factors already in place and unchanged by this task: the cookie is `httponly` (unreadable by injected JS/XSS), `samesite=strict` (never sent cross-site), `secure` in production (TLS-only), and scoped to `path=/admin` only. The token itself is HMAC-signed with `app_master_key` (not the display password), so a compromised session token still can't be used to derive or change the login password. No new attack surface is introduced — the only change is a longer validity window on the same signed-token mechanism already in production.

## Testing

Existing `auth.py`/`router.py` login/session tests (`backend/tests/admin/`) already exercise `issue_token`/`verify_token`/the cookie at the default 12-hour boundary — these need updating to assert the new 30-day (`720`-hour) value instead. No new test *behavior* is needed beyond confirming the constant is threaded through both call sites consistently (a regression test asserting `login`'s response cookie `max_age` matches `ADMIN_SESSION_TTL_HOURS * 3600` closes the exact "two numbers can drift" risk this design removes).

## Out of scope

Options B (bookmarkable magic-link token) and C (no password) — not chosen. No change to per-device behavior (a new browser/device still requires the password once, as before — inherent to cookie-based auth, not something this task can or should change).
