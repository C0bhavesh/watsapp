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
- **Public API:** `Settings(BaseSettings)` — `app_master_key: str` (required, from env/.env), `database_url: str = ""`, `shop_domain: str = "thetavas.myshopify.com"`, `shopify_api_version: str = "2026-07"`, `request_timeout_seconds: float = 20.0`, `app_env: str = "dev"`.
- **Used in:** deps.Container, TokenManager, ShopifyClient, scripts/smoke_shopify.
- **Notes:** Shopify API version is read ONLY from `shopify_api_version` — never hardcode in URLs. Calling `Settings()` with no args needs `# type: ignore[call-arg]` (mypy strict; value loaded from env at runtime).

## SecretVault
- **File:** backend/app/config/crypto.py
- **Purpose:** Fernet encrypt/decrypt for secrets at rest.
- **Public API:** `SecretVault(master_key: str)`; `.encrypt(plaintext: str) -> str`; `.decrypt(token: str) -> str`; `VaultError(Exception)`.
- **Used in:** ConfigService, deps.Container.
- **Notes:** invalid master key → `VaultError` at construction; decrypt of garbage → `VaultError`. Error strings never contain the plaintext.

## ConfigRepo (Protocol) + InMemoryConfigRepo
- **File:** backend/app/store/base.py (Protocol), backend/app/store/memory.py (impl)
- **Purpose:** async key→value string store for config/secrets. Postgres impl arrives Phase 2.
- **Public API:** `ConfigRepo.get(key) -> str | None`, `.set(key, value) -> None`; `InMemoryConfigRepo()` implements it (dict-backed).
- **Used in:** ConfigService, deps.Container.
- **Notes:** `core` depends on the Protocol, never the concrete repo.

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
- **Notes:** phone chain = order.phone → shipping_phone → billing_phone. COD = gateway contains "cash on delivery" (case-insensitive) OR tag == "cod". `normalize_order_name` maps `tavas3733`/`#tavas3733`/`3733`/`#3733` → `tavas3733`. `AuthorizedOrder` is the ADR-004 mutation gate — only core.order_resolver should construct it in production.

## Shopify error taxonomy
- **File:** backend/app/shopify/errors.py
- **Purpose:** typed Shopify-layer failures.
- **Public API:** `ShopifyError` (base); `ShopifyAuthError`, `ShopifyThrottled`, `ShopifyUnavailable`, `TokenGrantError`; `ShopifyGraphQLError(messages: list[str])` with `.messages`.
- **Used in:** TokenManager, ShopifyClient.
- **Notes:** error messages never include secrets/tokens.

## TokenManager (ADR-003)
- **File:** backend/app/shopify/token_manager.py
- **Purpose:** client-credentials access token with store persistence, 1h refresh margin, single-flight.
- **Public API:** `TokenManager(http, config, settings, now=time.time)`; async `get_token() -> str`, `force_refresh() -> str`. `REFRESH_MARGIN_SECONDS = 3600`.
- **Used in:** ShopifyClient, deps.Container, scripts/smoke_shopify.
- **Notes:** caches in-memory + persists token (secret) & expiry (plain) to ConfigService; asyncio.Lock double-checked → one grant under concurrency. Grant failures raise `TokenGrantError` without leaking client_secret.

## ShopifyClient (5 ops)
- **File:** backend/app/shopify/client.py
- **Purpose:** Admin GraphQL client — transport + 3 reads + 2 mutations.
- **Public API:** `ShopifyClient(http, tokens, settings)`; async `get_order(gid) -> Order | None`, `find_order_by_name(raw_name) -> Order | None`, `find_customer_orders_by_phone(phone_e164) -> list[Order]`, `add_tags(auth: AuthorizedOrder, tags) -> None`, `cancel_order(auth: AuthorizedOrder, *, reason="CUSTOMER", restock=True) -> CancelRequested`. Module: `ORDER_FIELDS`, `_order_from_node`.
- **Used in:** deps.Container, scripts/smoke_shopify, core (Phase 2+).
- **Notes:** URL uses `settings.shopify_api_version` (2026-07). HTTP 401 → force_refresh + retry once → `ShopifyAuthError`. THROTTLED → `ShopifyThrottled`; errors+null data → `ShopifyGraphQLError`; partial data+errors → returns data. Mutations accept ONLY `AuthorizedOrder` (ADR-004); userErrors raise `ShopifyGraphQLError`. Direct order-by-phone is NOT supported by Shopify — customer→orders fallback used, returns `[]` on access-denied (read_customers scope may be absent).

## Container / deps (composition root)
- **File:** backend/app/deps.py
- **Purpose:** singleton wiring of the whole Shopify layer.
- **Public API:** `Container` dataclass (settings, vault, config_repo, config, http, tokens, shopify); `get_container() -> Container` (module singleton); `reset_container() -> None` (tests).
- **Used in:** FastAPI app / routes (Phase 2+), tests.
- **Notes:** uses `InMemoryConfigRepo` in Phase 1 — swap to Postgres when `database_url` is set (Phase 2).

## FastAPI app + GET /health
- **File:** backend/app/main.py (app), backend/api/index.py (Vercel ASGI entrypoint)
- **Purpose:** deployable FastAPI app; liveness probe.
- **Public API:** `app = FastAPI(...)`; `GET /health` → `{"status": "ok", "service": "thetavas-order-bot"}`.
- **Used in:** Vercel deploy (`vercel.json`, region bom1).
- **Notes:** entrypoint `api/index.py` re-exports `app`; all routes → `api/index.py`.
