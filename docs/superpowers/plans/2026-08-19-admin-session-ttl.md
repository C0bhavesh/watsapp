# Admin Session TTL Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the admin panel's login session from 12 hours to 30 days, replacing two independently-maintained magic numbers with one named constant.

**Architecture:** `app/admin/router.py` gains `ADMIN_SESSION_TTL_HOURS = 24 * 30`, passed explicitly to `issue_token(..., ttl_hours=ADMIN_SESSION_TTL_HOURS)` and used for the cookie's `max_age=ADMIN_SESSION_TTL_HOURS * 3600` in `login()`. `app/admin/auth.py::issue_token`'s signature and default (`ttl_hours: int = 12`) are unchanged — only the one call site in `login()` now passes an explicit value instead of relying on the default.

**Tech Stack:** Python 3.12+, FastAPI, pytest.

## Global Constraints

- Full type hints; `mypy app` strict must stay clean.
- `ruff check .` must stay clean.
- No change to `verify_token`, `check_password`, or any cookie flag other than `max_age` (`httponly`, `samesite=strict`, `secure` derived from `app_env`, `path=/admin` all unchanged).
- No change to the `5/minute` login rate limit.
- This is an auth-surface change — per `.claude/rules/common/agents.md`, `security-reviewer` runs after `code-reviewer` (mandatory, not conditional, for this task).

---

### Task 1: Named TTL constant + regression test

**Files:**
- Modify: `backend/app/admin/router.py`
- Test: `backend/tests/admin/test_login.py`

**Interfaces:**
- Produces: `ADMIN_SESSION_TTL_HOURS: int` (module-level constant in `router.py`, value `720`), consumed by nothing outside this task (single-task plan).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_login.py`, after `test_login_cookie_not_secure_in_dev` (the last test in the file):

```python
def test_login_cookie_max_age_is_30_days(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "max-age=2592000" in set_cookie.lower()
```

`2592000` = `30 * 24 * 3600` seconds (30 days) — the exact value the test pins, independent of how the production code computes it, so the test fails if the duration is ever anything other than 30 days.

- [ ] **Step 2: Run test to verify it fails**

Run (from `backend/`): `python -m pytest tests/admin/test_login.py::test_login_cookie_max_age_is_30_days -v`
Expected: FAIL — `assert "max-age=2592000" in "...max-age=43200..."` (43200 = the current 12-hour value in seconds)

- [ ] **Step 3: Add the named constant and use it at both call sites**

In `backend/app/admin/router.py`, find the `login` function (currently ~line 222-246):

```python
@admin_router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, response: Response) -> dict[str, bool]:
    """Verify the admin password and issue a signed session cookie on success."""
    settings = get_container().settings
    if not settings.admin_password:
        _audit("login", "not_configured")
        raise HTTPException(status_code=503, detail="admin not configured")
    if not check_password(req.password, settings.admin_password):
        _audit("login", "failure")
        raise HTTPException(status_code=401, detail="invalid password")
    _audit("login", "success")
    token = issue_token(settings.app_master_key, _now())
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="strict",
        # Secure derived from server config, never a client header: prod (TLS-terminated at
        # Vercel) always forces Secure; dev/tests over http stay usable (app_env defaults to dev).
        secure=settings.app_env == "prod",
        max_age=12 * 3600,  # keep in sync with issue_token ttl_hours
        path="/admin",
    )
    return {"ok": True}
```

Add the constant immediately before the `login` function (right after the `LoginRequest` class, which sits just above `@admin_router.post("/login")`):

```python
# Owner-directed (client-decisions-all.md Q20/A, confirmed 30-day duration, 2026-08-19): a
# device already logged in should rarely need the password again. Single source of truth for
# BOTH issue_token's ttl_hours and the cookie's max_age, so the two can never independently
# drift out of sync (the prior code kept them in sync only via a comment).
ADMIN_SESSION_TTL_HOURS = 24 * 30
```

Then change the two coupled values inside `login`:

```python
    _audit("login", "success")
    token = issue_token(settings.app_master_key, _now(), ttl_hours=ADMIN_SESSION_TTL_HOURS)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="strict",
        # Secure derived from server config, never a client header: prod (TLS-terminated at
        # Vercel) always forces Secure; dev/tests over http stay usable (app_env defaults to dev).
        secure=settings.app_env == "prod",
        max_age=ADMIN_SESSION_TTL_HOURS * 3600,
        path="/admin",
    )
    return {"ok": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/admin/test_login.py::test_login_cookie_max_age_is_30_days -v`
Expected: PASS

- [ ] **Step 5: Run the full admin test suite + full backend suite**

Run: `python -m pytest tests/admin/ -v`
Expected: all pass, including the pre-existing `test_login_ok_sets_cookie_and_session_works`, `test_login_cookie_not_secure_in_dev`, etc. — none of them pinned the old 12-hour/43200-second value, so none should need changes (verify this by reading their assertions if any fail — do not weaken an assertion to make it pass without understanding why it changed).
Run: `python -m pytest`
Expected: all pass. `backend/tests/admin/test_auth.py`'s tests (`issue_token`/`verify_token` unit tests) call `issue_token` directly with no `ttl_hours` arg or with an explicit `ttl_hours=12` — these are testing `auth.py`'s own default/behavior, which this task does NOT change (only the `router.py` call site changes), so they should be unaffected.

- [ ] **Step 6: Run mypy + ruff**

Run (from `backend/`): `python -m mypy app/admin/router.py`
Run: `python -m ruff check app/admin/router.py backend/tests/admin/test_login.py`
Expected: both clean.

- [ ] **Step 7: Secrets-compliance grep**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/admin/router.py`
Expected: empty.

- [ ] **Step 8: Confirm `order_actions.py` untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_login.py
git commit -m "feat(admin): extend session TTL to 30 days (client-directed, Q20/A)"
```

---

## Self-review notes (plan author)

- **Spec coverage:** named constant replacing the two coupled magic numbers (design) ✓ Step 3; regression test pinning the exact new duration (design's testing section) ✓ Step 1; no change to `verify_token`/`check_password`/other cookie flags/rate limit (Global Constraints) ✓ Step 3 diff shows only `max_age` and the `issue_token` call changed.
- **Placeholder scan:** no TBD/TODO; every step has literal, complete code.
- **Type consistency:** `ADMIN_SESSION_TTL_HOURS` is a plain `int`, used identically at both call sites within the same function — no cross-task signature risk (single task).
- **Scope:** single task is correct — this is one cohesive, atomic change with no natural split point.

## Next steps after Task 1 is done

1. Route to `code-reviewer` (scoped to `router.py` + `test_login.py`).
2. Route to `security-reviewer` — **mandatory**, not optional, since this changes an auth session's validity window (`.claude/rules/common/agents.md`'s sensitive-surface trigger explicitly lists "auth").
3. `doc-updater`: mark client-decisions-all.md Q20 as ANSWERED (A, 30 days), update `_pipeline_status.md`'s Q20 CHECKPOINT to resolved/shipped, note the new constant in `component_registry.md`.
4. No schema/migration involved — nothing for the owner to run in Supabase.
5. Owner reviews → push after approval (never auto-push, per CLAUDE.md Rule 7).
