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

## [2026-07-28] pydantic-settings `Settings()` no-arg call trips mypy strict
**Mistake/Issue:** `Settings()` (values loaded from env/.env at runtime) fails mypy strict with `Missing named argument "app_master_key" [call-arg]`, because the required field has no default and the pydantic mypy plugin isn't enabled.
**Correct:** annotate the call site with `# type: ignore[call-arg]` (the plan already does this in `test_settings.py` for `Settings(_env_file=None)`). Used in `app/deps.py` and `scripts/smoke_shopify.py`.
**Pattern:** required-without-default BaseSettings fields are "required" to mypy even though env supplies them — expect a `call-arg` ignore at every no-arg `Settings()` construction.
