# Error Learnings

> Append an entry whenever a non-obvious issue is encountered and solved, or when the human corrects a mistake. Agents read this before starting new work to avoid repeating known mistakes.

## Format
## [YYYY-MM-DD] Short Title
**Mistake/Issue:** what went wrong or was non-obvious
**Correct:** what should be done instead
**Pattern:** the general rule going forward

---
<!-- entries below -->

## [2026-07-28] Shopify orders are NOT searchable by phone
**Mistake/Issue:** `orders(query:"phone:...")` silently ignores the phone filter — it looks like it works but returns unfiltered results.
**Correct:** maintain our own `order_mappings` (phone_e164 → order_gid) filled from the `orders/create` webhook; fallback: `customers(query:"phone:...")` → `orders(query:"customer_id:...")`.
**Pattern:** verify every Shopify query filter against shopify.dev's supported-filter list before designing around it.

## [2026-07-28] Two different webhook HMAC schemes in one app
**Mistake/Issue:** Meta and Shopify both send HMAC-SHA256 signatures but encode them differently — copying the cafe verifier for Shopify would silently reject every webhook.
**Correct:** Meta `X-Hub-Signature-256` = **hex**; Shopify `X-Shopify-Hmac-Sha256` = **base64**. Two verifiers, both on the RAW body, constant-time compare.
**Pattern:** never reuse a signature verifier across providers without checking the encoding.

## [2026-07-28] Template quick-reply taps are message type "button", NOT interactive.button_reply
**Mistake/Issue:** A tap on a TEMPLATE quick-reply button arrives as `type:"button"` with `button.{text,payload}` + `context.id`; the cafe inbound parser only handles `type:"interactive"` (`interactive.button_reply`) and returns `None` for unknown types — so copying it wholesale makes every Confirm/Cancel tap fail SILENTLY (found by architecture review F4, before any code was written).
**Correct:** add an `InboundButton` variant to the typed union; template sends must attach an explicit per-button `payload` (≤128 chars, e.g. `order:confirm:{gid}`) or Meta returns only the language-dependent button text.
**Pattern:** when copying a channel parser across a message CLASS (template vs interactive), re-verify the inbound webhook shape against the provider's reference — not just the outbound payload.

## [2026-07-28] Vercel Python has no reliable "process after response" — use a durable outbox
**Mistake/Issue:** The plan said "ack Shopify 2xx <5s, process after" — but FastAPI BackgroundTasks on Vercel's buffered Python runtime run INSIDE the request cycle (delaying the response), and post-response work can be killed on instance reclaim. Combined with webhook-id idempotency, a lost send is never retried: customer silently gets no message (review F1).
**Correct:** webhook handler = one short DB transaction (dedupe insert + mapping upsert + `outbound_messages` queue row) → 200. A separate cron/self-invoked drain sends from the outbox with retries; `dedupe_key` UNIQUE makes "one push per order ever" a DB invariant.
**Pattern:** on serverless, anything that must survive the request needs a durable queue/table — never in-process background work.

## [2026-07-28] Cafe project is pip-installed editable and hijacks our `app.*` imports
**Mistake/Issue:** The dev machine has the cafe reference project (`beyond_loaf_agent`, `D:\ai_whatsapp_agent\backend`) installed as an editable package. Its `_EditableFinder` on `sys.meta_path` maps the top-level `app` package to the cafe dir. Because it runs AFTER the standard `PathFinder`, `import app` resolves to OUR `backend/app` — BUT any `app.<child>` submodule that does NOT yet exist in our package falls through to the cafe project (e.g. `import app.deps` pulled in `D:\ai_whatsapp_agent\backend\app\deps.py` and blew up on `from app.config.crypto import Crypto`). This produced a confusing RED (cafe ImportError instead of a clean `ModuleNotFoundError`).
**Correct:** it resolves itself as soon as our `app/<module>.py` exists (PathFinder finds ours first). No action needed if you create the module before importing it. If ever isolation is required: `pip uninstall beyond-loaf-agent` in this env, or run tests with `PYTHONPATH` scoping — but do NOT modify the shared env as part of feature work.
**Pattern:** a confusing cross-project ImportError under a same-named top-level package usually means an editable install is shadowing missing submodules — check `site-packages/__editable__*.pth` + `*_finder.py` before suspecting your own code.

## [2026-07-28] A docstring is not an invariant — `AuthorizedOrder` (ADR-004) forged in a PoC
**Mistake/Issue:** `AuthorizedOrder` only *documented* "verified_phone matches the order" — security review forged one for a victim order and `cancel_order` accepted it; the mutation-safety gate was enforcement-in-name-only.
**Correct:** add `__post_init__` that raises `ValueError` unless `verified_phone` is truthy and equals one of the order's phones; fix ripple fixtures (tests/smoke) to build genuinely consistent orders instead of relaxing the check.
**Pattern:** a type that asserts an invariant must validate it at construction — if the guarantee lives only in a docstring/convention, treat it as unenforced.

## [2026-07-28] pydantic-settings `Settings()` no-arg call trips mypy strict
**Mistake/Issue:** `Settings()` (values loaded from env/.env at runtime) fails mypy strict with `Missing named argument "app_master_key" [call-arg]`, because the required field has no default and the pydantic mypy plugin isn't enabled.
**Correct:** annotate the call site with `# type: ignore[call-arg]` (the plan already does this in `test_settings.py` for `Settings(_env_file=None)`). Used in `app/deps.py` and `scripts/smoke_shopify.py`.
**Pattern:** required-without-default BaseSettings fields are "required" to mypy even though env supplies them — expect a `call-arg` ignore at every no-arg `Settings()` construction.

## [2026-07-28] asyncpg ships no py.typed — mypy strict rejects `import asyncpg` in app code
**Mistake/Issue:** asyncpg (0.31.0 here) has no `py.typed` marker, so `mypy --strict` flags `import-untyped` on `import asyncpg` in `app/store/pg_factory.py` and `postgres.py`, breaking the `mypy app` gate.
**Correct:** add a per-module override to `backend/pyproject.toml`: `[[tool.mypy.overrides]]` `module = "asyncpg.*"`, `ignore_missing_imports = true`. Added alongside the dependency in Task 2.
**Pattern:** when adding an untyped third-party dep under a `strict` mypy config, expect to add an `ignore_missing_imports` override for it — the dep being installed is not enough.

## [2026-07-28] Plan-verbatim code can exceed the repo's ruff line-length; lint tests too, not just app/
**Mistake/Issue:** Several lines copied verbatim from the Phase 2 plan (a docstring, a ternary, and inline test comments) exceeded ruff `line-length = 100`, and one plan test imported `get_container` unused (F401). Linting only the per-task `app/` files (as the compliance step lists) let the test-file violations reach a later whole-project `ruff check .`.
**Correct:** behaviour-preserving reflow (split the line / move the comment above the statement) and drop the unused import. Run `ruff check .` (whole project, incl. `tests/`) per task, not just on the app files touched.
**Pattern:** a plan's inline code is not guaranteed lint-clean against this repo's config — run the full-project linter each task, and treat test files as first-class lint targets.

## [2026-07-28] hmac.compare_digest raises TypeError on non-ASCII str — compare bytes for header values
**Mistake/Issue:** `hmac.compare_digest(a, b)` with two `str` raises `TypeError` if either contains a non-ASCII char. Starlette decodes HTTP headers latin-1, so an attacker header byte `\xe9` reaches the verifier as a non-ASCII str → 500 on the Shopify HMAC check and the cron `X-Cron-Secret` check.
**Correct:** encode both sides to bytes before comparing — `candidate.encode("ascii")` inside `try/except UnicodeEncodeError: return False` (fail closed), then `compare_digest(expected.encode("ascii"), provided)`.
**Pattern:** any secret/signature compare on a value that originated in an HTTP header must be a bytes compare guarded against non-ASCII input — never `compare_digest` on two raw strs.

## [2026-07-28] Webhook payload fields must be type-coerced — a 500 on a signed delivery deletes the subscription
**Mistake/Issue:** JSON fields were read with the wrong shape assumed (`payload.get("phone")` an int → `normalize_phone` TypeError; `customer` a str → `.get` AttributeError; `payment_gateway_names` an int → not iterable; unvalidated `email`/`financial_status` into asyncpg text params → DataError). Each is a 500 on a signature-valid delivery, and Shopify deletes a webhook subscription after 19 consecutive failures — so one poison-but-signed payload can silently unhook order ingestion.
**Correct:** coerce every field read with `_s`/`_d`/`_seq` helpers (str/dict/list-or-empty) before use, guard `normalize_phone`/`choose_language` against non-str, and route optional DB fields through the parser so a bad value becomes `None`, not a crash.
**Pattern:** on a signed webhook, treat every payload field as attacker-typed — a 500 is not a transient error, it burns the provider's retry budget and can tear down the integration.

## [2026-07-28] Empty stored secret ≠ absent secret — they hit different fail-closed branches
**Mistake/Issue:** To cover the untested "client secret not configured → 403" path I first set the raw config value to `""` (`config_repo.set("shopify:client_secret", "")`). That does NOT exercise the `not secret` short-circuit — `ConfigService.get_secret` sees a non-None raw value and calls `vault.decrypt("")`, which raises `VaultError` (Fernet `InvalidToken` on empty input). So it lands on the SAME branch as the corrupt-secret test, adding no new coverage.
**Correct:** to hit the genuine "unset" branch (`get_secret` returns `None`), rebuild a fresh container WITHOUT seeding the key (`reset_container(); get_container()`), not an empty stored value.
**Pattern:** with encrypt-at-rest config, an empty stored value routes through decrypt (→ VaultError), while a truly-absent key returns None — they take different code paths even though both fail closed to 403. Choose the fixture that targets the branch you mean to cover.

## [2026-07-28] No project venv on this machine — use the codex-runtimes Python interpreter
**Mistake/Issue:** `python`/`python3`/`py` on PATH all resolve to the Windows Store alias stub in `WindowsApps` (no real interpreter, no `pip`), and there is NO `.venv`/`venv` in the repo or the worktree — so `pytest`/`ruff`/`mypy` cannot run via the obvious commands. This blocks all TDD until a real interpreter is found.
**Correct:** the working interpreter is `C:\Users\cbbha\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` (Python 3.12.13) — it already has every project dep installed (fastapi, pydantic, httpx, cryptography, asyncpg, pytest, pytest-asyncio, ruff, mypy). Run everything as `"$PY" -m pytest|ruff|mypy` from `backend/`. It resolves `import app` to the worktree's `backend/app` correctly (no cafe-editable-install shadowing observed).
**Pattern:** on this Windows machine, when the PATH `python` is a WindowsApps stub and there's no repo venv, reach for the codex-runtimes bundled interpreter before trying to bootstrap a new venv.

## [2026-07-28] Plan code can lag the repo — PostgresMessageStore uses the existing `_rows_affected` helper, not `.endswith("0")`
**Mistake/Issue:** the Phase 3 plan's `PostgresMessageStore.record_if_new` returned `not result.endswith("0")` — but `.endswith("0")` mis-parses any command tag ending in a multi-digit count whose last char is 0 (e.g. `INSERT 0 10`), the exact fragility flagged as a Phase 2 code-review follow-up. `app/store/postgres.py` already ships `_rows_affected(tag)` (parses the numeric suffix) used by `PostgresIngestStore`.
**Correct:** implement `return _rows_affected(result) > 0`, reusing the existing helper, rather than copying the plan's `.endswith` verbatim.
**Pattern:** when a plan's code duplicates logic that already exists in the repo as a hardened helper, port to the helper — the plan is a snapshot and may predate the repo's fixes.
