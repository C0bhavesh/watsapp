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
- **Public API:** `Settings(BaseSettings)` — `app_master_key: str` (required, from env/.env), `database_url: str = ""`, `shop_domain: str = "thetavas.myshopify.com"`, `shopify_api_version: str = "2026-07"`, `request_timeout_seconds: float = 20.0`, `app_env: str = "dev"`, `cron_secret: str = ""` (env `CRON_SECRET`; empty disables the jobs endpoint).
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
- **Public API:** `ConfigRepo.get(key) -> str | None`, `.set(key, value) -> None`; `InMemoryConfigRepo()` (dict-backed); `PostgresConfigRepo(pool: LazyPool)` (UPSERT into `app_config` on set).
- **Used in:** ConfigService, deps.Container.
- **Notes:** `core` depends on the Protocol, never the concrete repo. deps picks Postgres when `database_url` is set, else in-memory.

## IngestStore (Protocol) + InMemoryIngestStore + PostgresIngestStore + dataclasses
- **File:** backend/app/store/base.py (Protocol + dataclasses), backend/app/store/memory.py (in-memory), backend/app/store/postgres.py (Postgres)
- **Purpose:** ADR-001 atomic order ingest — dedupe (processed_webhooks) + mapping upsert (order_mappings) + outbox row (outbound_messages) in ONE transaction.
- **Public API:** `IngestStore.ingest_order_created(webhook_id, topic, mapping: MappingUpsert, outbound: OutboundDraft | None) -> IngestResult`. Frozen dataclasses: `MappingUpsert(order_gid, order_name, order_number_int, phone_e164, customer_name, email, language, financial_status_at_create, is_cod)`; `OutboundDraft(dedupe_key, kind, phone_e164, payload_json)`; `IngestResult(duplicate, queued)`. In-memory exposes `.webhooks/.mappings/.outbound` for assertions.
- **Used in:** channels.shopify_webhook, deps.Container.
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
- **Notes:** returns `None` if any of the 6 keys is unset; all 6 must be present for a valid config. Secrets are fetched via `config.get_secret(f"whatsapp:{field}")`, plain via `config.get_plain(f"whatsapp:{field}")`.

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
- **Public API:** `Container` dataclass (settings, vault, config_repo, config, http, tokens, shopify, ingest); `get_container() -> Container` (module singleton); `reset_container() -> None` (tests).
- **Used in:** FastAPI app / routes, tests.
- **Notes:** when `settings.database_url` is set → `PostgresConfigRepo` + `PostgresIngestStore` over ONE shared `LazyPool` (no connection made at build time — LazyPool connects on first acquire); else `InMemoryConfigRepo` + `InMemoryIngestStore`. `http = AsyncClient(follow_redirects=False)`.

## FastAPI app + GET /health + routers
- **File:** backend/app/main.py (app), backend/api/index.py (Vercel ASGI entrypoint)
- **Purpose:** deployable FastAPI app; liveness probe; mounts the Phase 2 routers.
- **Public API:** `app = FastAPI(...)`; `GET /health` → `{"status": "ok", "service": "thetavas-order-bot"}`. Includes `shopify_webhook_router` and `jobs_router`.
- **Used in:** Vercel deploy (`vercel.json`, region bom1).
- **Notes:** entrypoint `api/index.py` re-exports `app`; all routes → `api/index.py`. Phase 1 security fix: OpenAPI/docs/redoc are DISABLED in prod — `_docs_enabled()` reads `Settings().app_env` (docs off when `app_env == "prod"`, default on otherwise / on missing key). Preserve this construction when adding routers.

## Jobs dispatcher (internal, authenticated)
- **File:** backend/app/jobs/router.py
- **Purpose:** single authenticated cron/self-invoke endpoint running a named-job registry.
- **Public API:** `router` — `GET|POST /internal/jobs/{name}`; `JOBS: dict[str, JobFn]` registry; `JobFn = Callable[[Container], Awaitable[dict[str, Any]]]`. Registered: `ensure_subscription` (reads config `public_base_url`, calls subscriptions.ensure_subscription against `{base}/webhooks/shopify`).
- **Used in:** main.app, Vercel cron (future).
- **Notes:** `settings.cron_secret` empty → 503 (never an open endpoint, F11); header `X-Cron-Secret` constant-time compared → 403 on mismatch/missing; unknown job → 404; `ensure_subscription` with no `public_base_url` → 200 `{"error": "public_base_url not configured"}`. A job raising any `ShopifyError` (base class) → 502 `{"job": name, "error": "job failed"}` — exception text is NEVER echoed (may carry vendor detail); non-`ShopifyError` exceptions still propagate as raw 500.
