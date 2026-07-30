# Component Registry

> Reusable building blocks (services, adapters, pydantic models, FastAPI dependencies, helpers). Grep before creating anything new — never duplicate.

## Format
## [ComponentName]
- **File:** app/path/to/file.py
- **Purpose:** one line
- **Public API:** key functions/classes + signatures
- **Used in:** module names
- **Notes:** gotchas

---
<!-- entries below -->

## Settings
- **File:** backend/app/config/settings.py
- **Purpose:** pydantic-settings config; fail-fast on missing `APP_MASTER_KEY`.
- **Public API:** `Settings(BaseSettings)` — `app_master_key: str` (required, from env/.env), `database_url: str = ""`, `shop_domain: str = "thetavas.myshopify.com"`, `shopify_api_version: str = "2026-07"`, `request_timeout_seconds: float = 20.0`, `app_env: str = "dev"`, `cron_secret: str = ""` (env `CRON_SECRET`; empty disables the jobs endpoint), `admin_password: str = ""` (env `ADMIN_PASSWORD`; empty → admin login returns 503, never grants access — Rule 1 third env exception, approved 2026-07-30).
- **Used in:** deps.Container, TokenManager, ShopifyClient, scripts/smoke_shopify.
- **Notes:** Shopify API version is read ONLY from `shopify_api_version` — never hardcode in URLs. Calling `Settings()` with no args needs `# type: ignore[call-arg]` (mypy strict; value loaded from env at runtime).

## SecretVault
- **File:** backend/app/config/crypto.py
- **Purpose:** Fernet encrypt/decrypt for secrets at rest.
- **Public API:** `SecretVault(master_key: str)`; `.encrypt(plaintext: str) -> str`; `.decrypt(token: str) -> str`; `VaultError(Exception)`.
- **Used in:** ConfigService, deps.Container.
- **Notes:** invalid master key → `VaultError` at construction; decrypt of garbage → `VaultError`. Error strings never contain the plaintext.

## ConfigRepo (Protocol) + InMemoryConfigRepo + PostgresConfigRepo
- **File:** backend/app/store/base.py (Protocol), backend/app/store/memory.py (in-memory), backend/app/store/postgres.py (Postgres)
- **Purpose:** async key→value string store for config/secrets.
- **Public API:** `ConfigRepo.get(key) -> str | None`, `.set(key, value) -> None`; knowledge-override methods (Phase 3.5): `.get_knowledge_override(kind) -> str | None`, `.set_knowledge_override(kind, content) -> None`, `.get_knowledge_overrides(kinds: list[str]) -> dict[str, str | None]`, `.bump_config_int(key) -> None`; `InMemoryConfigRepo()` (dict-backed); `PostgresConfigRepo(pool: LazyPool)` (UPSERT into `app_config` on set).
- **Used in:** ConfigService, deps.Container, knowledge.KnowledgeLoader, admin.router (knowledge PUT).
- **Notes:** `core` depends on the Protocol, never the concrete repo. deps picks Postgres when `database_url` is set, else in-memory. Knowledge overrides live in a separate `knowledge_overrides(kind PK, content, updated_at)` table (survives Vercel deploys, unlike seed files); `bump_config_int` writes a decimal-int string to `app_config` (`knowledge_version` → cache invalidation), created at "1" then `+1` (Postgres uses `(value::bigint + 1)::text`).

## IngestStore (Protocol) + InMemoryIngestStore + PostgresIngestStore + dataclasses
- **File:** backend/app/store/base.py (Protocol + dataclasses), backend/app/store/memory.py (in-memory), backend/app/store/postgres.py (Postgres)
- **Purpose:** ADR-001 atomic order ingest — dedupe (processed_webhooks) + mapping upsert (order_mappings) + outbox row (outbound_messages) in ONE transaction.
- **Public API:** `IngestStore.ingest_order_created(webhook_id, topic, mapping: MappingUpsert, outbound: OutboundDraft | None) -> IngestResult`; read-only views (Phase 3.5): `.recent_mappings(limit) -> list[MappingView]`, `.recent_outbound(limit) -> list[OutboundView]`. Frozen dataclasses: `MappingUpsert(order_gid, order_name, order_number_int, phone_e164, customer_name, email, language, financial_status_at_create, is_cod)`; `OutboundDraft(dedupe_key, kind, phone_e164, payload_json)`; `IngestResult(duplicate, queued)`; `MappingView(order_gid, order_name, phone_e164, status, is_cod, created_at)`; `OutboundView(dedupe_key, state, kind, phone_e164, attempts, last_error_code, created_at)`. In-memory exposes `.webhooks/.mappings/.outbound` for assertions.
- **Used in:** channels.shopify_webhook, deps.Container, admin.router (mappings/outbox views).
- **Notes:** duplicate `(webhook_id, topic)` → `duplicate=True, queued=False` (short-circuit). `dedupe_key` UNIQUE (`order_created:{order_gid}`) = one push per order ever; re-seen dedupe_key → `queued=False`. `outbound=None` (ineligible/backfill) maps without queueing. Postgres detects rowcount via command-tag `.endswith("0")`.

## LazyPool (asyncpg)
- **File:** backend/app/store/pg_factory.py
- **Purpose:** asyncpg connection pool created on FIRST `acquire()`, never at import (serverless cold-start rule).
- **Public API:** `LazyPool(dsn: str)`; `async with pool.acquire() as conn:`; `async close()`. Double-checked `asyncio.Lock` guards single pool creation. `create_pool(min_size=0, max_size=5)`.
- **Used in:** PostgresConfigRepo, PostgresIngestStore, deps.Container.
- **Notes:** asyncpg has no py.typed marker — `[[tool.mypy.overrides]] module="asyncpg.*" ignore_missing_imports=true` in pyproject.toml.

## normalize_phone (E.164)
- **File:** backend/app/core/phone.py
- **Purpose:** normalize any phone/wa_id to E.164 with `+91` default for bare 10-digit Indian numbers.
- **Public API:** `normalize_phone(raw: str | None) -> str | None`.
- **Used in:** channels.shopify_orders (parser). core layer, no external deps.
- **Notes:** strips non-digits, drops leading `00`; 10-digit → `+91…`; 11-digit `0…` → `+91…`; 11–15 digits → `+…`; else None.

## Shopify order webhook parsing (IncomingOrder)
- **File:** backend/app/channels/shopify_orders.py
- **Purpose:** pure parse of an `orders/create` payload + language + push eligibility.
- **Public API:** `IncomingOrder(gid, name, order_number, email, phone_e164, customer_name, tags, gateways, created_at, locale)` with `.is_cod()`; `parse_order_created(payload) -> IncomingOrder | None` (None if gid/name missing); `choose_language(locale, default="en") -> str` (supported `{en,hi,gu}`, first 2 letters); `is_eligible_for_push(order, now, push_policy, staleness_hours) -> bool`.
- **Used in:** channels.shopify_webhook.
- **Notes:** GID taken from `admin_graphql_api_id` (never reconstructed — F20). Phone chain: order→customer→shipping→billing, each normalized. COD = gateway contains "cash on delivery" OR tag == "cod". Eligibility: created_at required + within staleness window; `cod_only` → COD only; `all`/`all_prepaid_no_buttons` → all.

## Shopify webhook HMAC verifier (base64)
- **File:** backend/app/channels/shopify_signature.py
- **Purpose:** verify Shopify webhook signatures.
- **Public API:** `verify_shopify_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool`.
- **Used in:** channels.shopify_webhook.
- **Notes:** Shopify = **base64**(HMAC-SHA256(raw body, client secret)); constant-time compare. Distinct from Meta (hex). Missing/empty header → False.

## Meta webhook HMAC verifier (hex)
- **File:** backend/app/channels/whatsapp_signature.py
- **Purpose:** verify Meta WhatsApp Cloud API webhook signatures.
- **Public API:** `verify_meta_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool`.
- **Used in:** channels.whatsapp (Phase 3+).
- **Notes:** Meta = **hex**(HMAC-SHA256(raw body, app secret)) with `sha256=` prefix; constant-time compare. Distinct from Shopify (base64). Non-ASCII header bytes → False (fail-closed). Missing/empty header → False.

## WhatsAppConfig (Fernet-split secrets/plain)
- **File:** backend/app/channels/whatsapp_config.py
- **Purpose:** load WhatsApp Bot API credentials from ConfigService (secrets encrypted, plain values unencrypted).
- **Public API:** frozen dataclass `WhatsAppConfig(access_token, app_secret, verify_token, phone_number_id, waba_id, api_version)`; module tuples `WHATSAPP_SECRET_FIELDS = ("access_token", "app_secret", "verify_token")`, `WHATSAPP_PLAIN_FIELDS = ("phone_number_id", "waba_id", "api_version")`; `async load_whatsapp_config(config: ConfigService) -> WhatsAppConfig | None`.
- **Used in:** deps.Container, channels.whatsapp (Phase 3+).
- **Notes:** returns `None` if any of the 6 keys is unset; all 6 must be present for a valid config. Secrets are fetched via `config.get_secret(f"whatsapp:{field}")`, plain via `config.get_plain(f"whatsapp:{field}")`. No hardcoded `api_version` default (ADR-005: operational values are config, not code).

## WhatsApp inbound event parser
- **File:** backend/app/channels/whatsapp_inbound.py
- **Purpose:** parse a Meta webhook envelope into a typed inbound event.
- **Public API:** frozen dataclasses `InboundText(message_id, wa_id, text, timestamp)`, `InboundInteractive(message_id, wa_id, button_id, button_title, timestamp)`, `InboundButton(message_id, wa_id, payload, button_text, context_message_id, timestamp)`; `InboundEvent = InboundText | InboundInteractive | InboundButton`; `extract_event(payload: dict) -> InboundEvent | None`.
- **Used in:** channels.whatsapp (POST receive).
- **Notes:** F4 — template quick-reply taps arrive as `type:"button"` (→ `InboundButton`), NOT `type:"interactive"` (→ `InboundInteractive`, for buttons WE sent via send_buttons). Every field type-coerced (`isinstance` guards); status callbacks / unknown types / malformed / type-confused payloads → `None`, never an exception.

## WhatsApp sender (send_text / send_template / send_buttons)
- **File:** backend/app/channels/whatsapp_sender.py
- **Purpose:** outbound Meta Graph message sends.
- **Public API:** `SendResult(ok, status_code, wamid, error)` (frozen); `WhatsAppSendError(Exception)`; `async send_text(http, cfg, to, body, timeout=20.0) -> SendResult`; `async send_template(http, cfg, to, template_name, language, body_params, button_payloads=(), timeout=20.0) -> SendResult`; `async send_buttons(http, cfg, to, body_text, buttons, timeout=20.0) -> SendResult` (`buttons: Sequence[tuple[id, title]]`).
- **Used in:** scripts/smoke_whatsapp; Phase 4/5 (outbox drain, confirm/cancel).
- **Notes:** POST to `graph.facebook.com/{api_version}/{phone_number_id}/messages`, bearer `cfg.access_token`. HTTP >=400 → `SendResult(ok=False, ...)` (not raised); transport/timeout error → `WhatsAppSendError`. `send_buttons`: 1-3 buttons, title <=20 chars else `ValueError`. `send_template` quick-reply buttons carry an explicit per-button `payload` (F4) as `type:"button"` components indexed `"0".."2"`.

## Deterministic multilingual reply copy
- **File:** backend/app/channels/copy.py
- **Purpose:** fixed system reply strings (never LLM-generated) in en/hi/hinglish/gu.
- **Public API:** `SUPPORTED_LANGUAGES = ("en", "hi", "hinglish", "gu")`; `copy_for(key: str, language: str) -> str`.
- **Used in:** Phase 4 (conversation deterministic replies).
- **Notes:** keys `order_confirmed`, `cancel_confirm_prompt`, `order_cancelled`, `order_not_found`, `refusal_other_order`, `error_fallback`. Unsupported language → English fallback; unknown key → `KeyError` (call sites internal, not user input). No emojis (CLAUDE.md). Wording is an OPEN owner/client review item before Phase 4 wires it in.

## MessageStore (Protocol) + InMemoryMessageStore + PostgresMessageStore
- **File:** backend/app/store/base.py (Protocol), backend/app/store/memory.py (in-memory), backend/app/store/postgres.py (Postgres)
- **Purpose:** dedupe authority for inbound Meta message ids (`processed_messages`) — the Meta-side sibling of `processed_webhooks`.
- **Public API:** `MessageStore.record_if_new(message_id: str) -> bool` (`True` iff newly recorded); `InMemoryMessageStore()` (inspectable `.seen: set[str]`); `PostgresMessageStore(pool: LazyPool)`.
- **Used in:** channels.whatsapp (POST receive), deps.Container.
- **Notes:** Postgres: `INSERT ... ON CONFLICT DO NOTHING`, rowcount via `_rows_affected(tag) > 0` (the existing helper, not `.endswith`). deps picks Postgres when `database_url` set, else in-memory. `processed_messages(message_id PK, received_at)` table pre-exists (schema.sql).

## WhatsApp webhook router (GET verify + POST receive)
- **File:** backend/app/channels/whatsapp.py
- **Purpose:** Meta webhook edge — GET subscribe verification + POST receive (HMAC, dedupe, typed dispatch stub).
- **Public API:** `router` — `GET /webhook/whatsapp`, `POST /webhook/whatsapp`.
- **Used in:** main.app.
- **Notes:** GET: `hub.mode==subscribe` + ASCII-safe constant-time `hub.verify_token` compare → echoes `hub.challenge`, else 403. POST: 403 on bad/unconfigured HMAC; 413 if body > 1 MiB; foreign `phone_number_id` / status callback / unparseable / non-dict / unknown-type → 200 `{"ok": true, "ignored": true}`; replay → 200 `{"ok": true, "duplicate": true}`; fresh event → 200 `{"ok": true, "duplicate": false, "event_type": <ClassName>}`. Phase 3 = pipe only: no engine/mutation dispatch — the `event_type` echo is the seam Phase 4/5 attach to.

## Subscription self-heal (ensure_subscription)
- **File:** backend/app/shopify/subscriptions.py
- **Purpose:** ensure the ORDERS_CREATE webhook subscription points at our callback URL AND is bound to the current Shopify API version.
- **Public API:** `async ensure_subscription(client: ShopifyClient, callback_url: str) -> str` → `"ok" | "created" | "updated"`.
- **Used in:** jobs.router (`ensure_subscription` job).
- **Notes:** F20 version-aware — a sub is `"ok"` ONLY when callbackUrl matches AND `apiVersion.handle == client.api_version`; a drifted URL **or** a stale API version → `webhookSubscriptionUpdate` (`"updated"`); none → `webhookSubscriptionCreate` (format JSON) → `"created"`. Both create and update pass `apiVersion: $apiVersion` (read via the `ShopifyClient.api_version` accessor, never the private settings attr). `_LIST_QUERY` requests `apiVersion { handle }` per node. userErrors → `ShopifyGraphQLError`.

## ConfigService
- **File:** backend/app/config/service.py
- **Purpose:** plain + encrypted config access over a ConfigRepo + SecretVault.
- **Public API:** `ConfigService(repo: ConfigRepo, vault: SecretVault)`; async `get_plain/set_plain(key[, value])`, `get_secret/set_secret(key[, value])` (secrets encrypted at rest).
- **Used in:** TokenManager, deps.Container, scripts/smoke_shopify.
- **Notes:** secret keys in use: `shopify:client_id`, `shopify:client_secret`, `shopify:access_token`. Plain key: `shopify:token_expires_at`.

## Shopify models + normalize_order_name
- **File:** backend/app/shopify/models.py
- **Purpose:** frozen domain dataclasses + pure derivations.
- **Public API:** `Money(amount, currency)`; `Order(...)` with `.best_phone()`, `.is_cod()`, `.is_cancelled()`; `AuthorizedOrder(order, verified_phone)`; `CancelRequested(job_id)`; `normalize_order_name(raw, prefix="tavas") -> str`.
- **Used in:** ShopifyClient (reads/mutations), core.order_resolver (Phase 2+).
- **Notes:** phone chain = order.phone → shipping_phone → billing_phone. COD = gateway contains "cash on delivery" (case-insensitive) OR tag == "cod". `normalize_order_name` maps `tavas3733`/`#tavas3733`/`3733`/`#3733` → `tavas3733`. `AuthorizedOrder` is the ADR-004 mutation gate — only core.order_resolver should construct it in production. Phase 1 security fix: `AuthorizedOrder.__post_init__` raises `ValueError` unless `verified_phone` is truthy AND matches one of the order's phones — the invariant is now enforced at construction, not just documented.

## Shopify error taxonomy
- **File:** backend/app/shopify/errors.py
- **Purpose:** typed Shopify-layer failures.
- **Public API:** `ShopifyError` (base); `ShopifyAuthError`, `ShopifyThrottled`, `ShopifyUnavailable`, `TokenGrantError`; `ShopifyGraphQLError(messages: list[str], codes: tuple[str, ...] = ())` with `.messages` and `.codes`.
- **Used in:** TokenManager, ShopifyClient, shopify.subscriptions.
- **Notes:** error messages never include secrets/tokens. `codes` is the optional second positional (Phase 1 security fix); callers passing only `messages` (e.g. subscriptions `_raise_on_user_errors`) still work.

## TokenManager (ADR-003)
- **File:** backend/app/shopify/token_manager.py
- **Purpose:** client-credentials access token with store persistence, 1h refresh margin, single-flight.
- **Public API:** `TokenManager(http, config, settings, now=time.time)`; async `get_token() -> str`, `force_refresh() -> str`. `REFRESH_MARGIN_SECONDS = 3600`.
- **Used in:** ShopifyClient, deps.Container, scripts/smoke_shopify.
- **Notes:** caches in-memory + persists token (secret) & expiry (plain) to ConfigService; asyncio.Lock double-checked → one grant under concurrency. Grant failures raise `TokenGrantError` without leaking client_secret.

## ShopifyClient (5 ops)
- **File:** backend/app/shopify/client.py
- **Purpose:** Admin GraphQL client — transport + 3 reads + 2 mutations.
- **Public API:** `ShopifyClient(http, tokens, settings)`; `@property api_version -> str` (read-only accessor for `settings.shopify_api_version`; used by subscriptions self-heal instead of reaching into the private attr); async `get_order(gid) -> Order | None`, `find_order_by_name(raw_name) -> Order | None`, `find_customer_orders_by_phone(phone_e164) -> list[Order]`, `add_tags(auth: AuthorizedOrder, tags) -> None`, `cancel_order(auth: AuthorizedOrder, *, reason="CUSTOMER", restock=True) -> CancelRequested`. Module: `ORDER_FIELDS`, `_order_from_node`.
- **Used in:** deps.Container, scripts/smoke_shopify, core (Phase 2+).
- **Notes:** URL uses `settings.shopify_api_version` (2026-07). HTTP 401 → force_refresh + retry once → `ShopifyAuthError`. THROTTLED → `ShopifyThrottled`; errors+null data → `ShopifyGraphQLError`; partial data+errors → returns data. Mutations accept ONLY `AuthorizedOrder` (ADR-004); userErrors raise `ShopifyGraphQLError`. Direct order-by-phone is NOT supported by Shopify — customer→orders fallback used, returns `[]` on access-denied (read_customers scope may be absent).

## Container / deps (composition root)
- **File:** backend/app/deps.py
- **Purpose:** singleton wiring of the whole Shopify layer.
- **Public API:** `Container` dataclass (settings, vault, config_repo, config, http, tokens, shopify, ingest, messages); `get_container() -> Container` (module singleton); `reset_container() -> None` (tests).
- **Used in:** FastAPI app / routes, tests.
- **Notes:** when `settings.database_url` is set → `PostgresConfigRepo` + `PostgresIngestStore` + `PostgresMessageStore` over ONE shared `LazyPool` (no connection made at build time — LazyPool connects on first acquire); else the three in-memory impls. `http = AsyncClient(follow_redirects=False)`.

## FastAPI app + GET /health + routers
- **File:** backend/app/main.py (app), backend/api/index.py (Vercel ASGI entrypoint)
- **Purpose:** deployable FastAPI app; liveness probe; mounts the Phase 2 routers.
- **Public API:** `app = FastAPI(...)`; `GET /health` → `{"status": "ok", "service": "thetavas-order-bot"}`. Includes `shopify_webhook_router`, `whatsapp_router`, `jobs_router`, and `admin_router`; registers slowapi (`app.state.limiter` + `RateLimitExceeded` handler), `AdminBodyCapMiddleware`, and mounts the static admin panel at `/admin/ui` (`StaticFiles(..., html=True)`).
- **Used in:** Vercel deploy (`vercel.json`, region bom1).
- **Notes:** entrypoint `api/index.py` re-exports `app`; all routes → `api/index.py`. Phase 1 security fix: OpenAPI/docs/redoc are DISABLED in prod — `_docs_enabled()` reads `Settings().app_env` (docs off when `app_env == "prod"`, default on otherwise / on missing key). Preserve this construction when adding routers. `AdminBodyCapMiddleware` must be added at module import (before first request) — it 413s oversized `/admin` requests by Content-Length before FastAPI parses the body.

## Jobs dispatcher (internal, authenticated)
- **File:** backend/app/jobs/router.py
- **Purpose:** single authenticated cron/self-invoke endpoint running a named-job registry.
- **Public API:** `router` — `GET|POST /internal/jobs/{name}`; `JOBS: dict[str, JobFn]` registry; `JobFn = Callable[[Container], Awaitable[dict[str, Any]]]`. Registered: `ensure_subscription` (reads config `public_base_url`, calls subscriptions.ensure_subscription against `{base}/webhooks/shopify`).
- **Used in:** main.app, Vercel cron (future).
- **Notes:** `settings.cron_secret` empty → 503 (never an open endpoint, F11); header `X-Cron-Secret` constant-time compared → 403 on mismatch/missing; unknown job → 404; `ensure_subscription` with no `public_base_url` → 200 `{"error": "public_base_url not configured"}`. A job raising any `ShopifyError` (base class) → 502 `{"job": name, "error": "job failed"}` — exception text is NEVER echoed (may carry vendor detail); non-`ShopifyError` exceptions still propagate as raw 500.

## Rate limiter (slowapi)
- **File:** backend/app/ratelimit.py
- **Purpose:** shared slowapi `Limiter` used to rate-limit the admin login.
- **Public API:** `limiter: Limiter = Limiter(key_func=get_remote_address)`.
- **Used in:** admin.router (`@limiter.limit("5/minute")` on POST /admin/login), main.app (`app.state.limiter` + `RateLimitExceeded` → 429 handler). `limiter.reset()` clears counters (tests).
- **Notes:** NEW dep `slowapi>=0.1.9` (untyped → `[[tool.mypy.overrides]] module="slowapi.*"`). Keyed by remote address; per-process in-memory storage (fine for one Vercel instance; not distributed).

## Admin auth primitives
- **File:** backend/app/admin/auth.py
- **Purpose:** signed expiring session token + constant-time password check (cafe-verbatim, dependency-free).
- **Public API:** `issue_token(secret, now: datetime, ttl_hours=12) -> str` (format `<unix_exp>.<b64url_hmac_sha256>`; empty secret → `ValueError`); `verify_token(secret, token, now) -> bool` (False, never raises, on empty secret / malformed / bad sig / expiry); `check_password(supplied, expected) -> bool` (constant-time; empty `expected` → False, fail closed).
- **Used in:** admin.router (login issues, require_admin verifies).
- **Notes:** the session token is signed with `settings.app_master_key` (not `admin_password`) so rotating the display password doesn't need re-keying. `verify_token` fails closed on every error path.

## Admin JSON API router
- **File:** backend/app/admin/router.py
- **Purpose:** the whole `/admin/*` JSON API — auth, creds (Shopify/WhatsApp/LLM), knowledge, controls, read-only views.
- **Public API:** `admin_router: APIRouter` (prefix `/admin`); `require_admin(admin_session: str | None = Cookie())` dependency (401 unless the session cookie verifies); `AdminBodyCapMiddleware` (ASGI, 413 on oversized `/admin` bodies). Endpoints: `POST /login` (rate-limited 5/min, 503 if unset password, 401 on wrong, sets httponly `admin_session` cookie), `GET /session`, `GET|POST /shopify`, `GET|POST /whatsapp`, `GET /providers`, `GET /config`, `POST /provider`, `GET|PUT /knowledge/{kind}`, `GET|PUT /controls`, `GET /mappings`, `GET /outbox`.
- **Used in:** main.app, static panel (`/admin/ui`).
- **Notes:** every route except `/login` depends on `require_admin`. Secrets are NEVER echoed — GET status routes return `{"configured": bool}` + non-secret fields only. Creds POSTs are partial-update (blank/omitted keeps the stored value; first-time setup requires the full set). `_clean(v)` trims blank→None. LLM `POST /provider` verifies the key with the provider BEFORE persisting (verify-key-before-save). Provider verify is monkeypatchable at `app.admin.router.verify_key`.

## AdminBodyCapMiddleware
- **File:** backend/app/admin/router.py
- **Purpose:** reject oversized `/admin` requests (>1 MiB Content-Length) with 413 at the ASGI layer, before FastAPI parses the body.
- **Public API:** `AdminBodyCapMiddleware(app, max_body=1_048_576)` (pure ASGI); registered via `app.add_middleware(AdminBodyCapMiddleware)`.
- **Used in:** main.app.
- **Notes:** a router-level dependency CANNOT do this — FastAPI parses (and may 422 on) the JSON body before path dependencies run, so an oversized invalid body would 422 before any cap check. Enforcing on Content-Length at the ASGI layer matches the webhook edges' posture and returns 413 first. Only inspects `/admin` paths.

## Admin knowledge validation models
- **File:** backend/app/admin/knowledge_models.py
- **Purpose:** kind-specific Pydantic validation for knowledge PUTs; serialize to the cafe-loader-compatible stored format.
- **Public API:** `validate_and_serialize(kind, payload: dict[str, object]) -> str` (raises `pydantic.ValidationError` → router maps to 422; unknown kind → `KeyError`, guarded upstream). Models `BrandVoiceBody`, `FaqBody`/`FaqItem`, `PatternsBody`/`PatternItem`, `BusinessBody`.
- **Used in:** admin.router (PUT /knowledge/{kind}).
- **Notes:** faq/patterns store a JSON **list**, business a JSON **object**, brand_voice raw markdown — so `KnowledgeLoader` reads overrides and seeds identically. Size caps: brand_voice ≤100k chars; faq 1..200 items (q/a ≤2000); patterns 1..100 items (≤20 examples); business fields ≤2000, extra dict of strings.

## Admin operational controls
- **File:** backend/app/admin/controls.py
- **Purpose:** ADR-002 (send-mode kill switch) + ADR-005 (client decisions as config) document; each field persists as its own plain `app_config` key so runtime readers keep their existing keys.
- **Public API:** `AdminControls` (Pydantic) + `TagLists`; `async load_controls(config) -> AdminControls` (stored-or-default, never crashes on corrupt values); `async save_controls(config, controls) -> None`; `REVEAL_ALLOWED`.
- **Used in:** admin.router (GET|PUT /controls); the individual keys are read by webhook eligibility / jobs / future outbox drain.
- **Notes:** validated: `send_mode` ∈ off/shadow/allowlist/live; `push_policy` ∈ cod_only/all/all_prepaid_no_buttons; `default_language` ∈ en/hi/gu; `reveal_fields` ⊆ {order_number,email,status}; `allowlist_phones`/`owner_alert_number` E.164; `public_base_url` https-or-empty (trailing slash stripped); `push_staleness_hours` 1..168. EXISTING keys reused unchanged: `push_policy`, `push_staleness_hours`, `public_base_url`. New keys: `send_mode`, `allowlist_phones`, `reveal_fields`, `tags`, `default_language`, `owner_alert_number`.

## KnowledgeLoader + Thetavas seeds
- **File:** backend/app/knowledge/loader.py (+ backend/app/knowledge/seeds/{brand_voice.md,faq.json,business.json,patterns.json})
- **Purpose:** override-else-seed reader for the store's voice + policy knowledge (cafe pattern, our names).
- **Public API:** `KnowledgeLoader(repo: ConfigRepo, seeds_dir: Path)` with `get(kind) -> str`, `knowledge_version() -> str` (config `knowledge_version` or "0"), `assemble_all() -> dict[str, str]`; module `KINDS = ("brand_voice","faq","business","patterns")`, `SEEDS_DIR: Path`.
- **Used in:** admin.router (knowledge GET); Phase 4 engine (prompt assembly) will consume it.
- **Notes:** DB override (from `knowledge_overrides`) wins; else the packaged seed FILE. Seeds are Thetavas defaults (no emojis, plain text, no menu — we don't sell in chat); business support fields are blank for the store team to fill from the panel. `knowledge_version` bumps on every PUT (Phase 4 cache invalidation).

## Providers layer (LLM behind LLMProvider)
- **File:** backend/app/providers/{base.py,registry.py,litellm_provider.py,verify.py}
- **Purpose:** minimal Phase 3.5 LLM provider abstraction — enough to verify an API key before saving; full engine use is Phase 4.
- **Public API:** base: `LLMProvider` Protocol (`complete(model, messages, api_key, timeout, *, extra_params=None) -> CompletionResult`), `Message`, `CompletionResult`, `ProviderError(.kind)`, `ProviderErrorKind(StrEnum: AUTH/RATE_LIMIT/NOT_FOUND/TIMEOUT/UNKNOWN)`. registry: `ProviderInfo(key,label,default_model,accept_on_rate_limit=False)`, `PROVIDERS` (gemini only), `get_provider(key)`, `list_providers()`. litellm_provider: `LiteLLMProvider` (lazy `import litellm` inside `complete`, api_key redacted from errors). verify: `VerifyResult(ok,error,kind)`, `async verify_key(provider, model, api_key, timeout=15.0) -> VerifyResult` (never raises, never leaks the key).
- **Used in:** admin.router (POST /provider verify-before-save).
- **Notes:** NEW dep `litellm>=1.89,<2` (untyped → `[[tool.mypy.overrides]] module="litellm.*"`), imported LAZILY so the webhook cold path never pays its import cost (rule F10). `ProviderErrorKind` is a `StrEnum` (repo ruff UP042; behaviour-equivalent to the cafe `(str, Enum)`). v1 ships Gemini only; the registry shape already supports Vertex/env-auth providers for Phase 4 (YAGNI now).
