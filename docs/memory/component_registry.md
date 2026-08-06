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
- **Public API:** `Settings(BaseSettings)` — `app_master_key: str` (required, from env/.env), `database_url: str = ""`, `shop_domain: str = "thetavas.myshopify.com"`, `shopify_api_version: str = "2026-07"`, `request_timeout_seconds: float = 20.0`, `app_env: str = "dev"`, `cron_secret: str = ""` (env `CRON_SECRET`; empty disables the jobs endpoint), `admin_password: str = ""` (env `ADMIN_PASSWORD`; empty → admin login returns 503, never grants access — Rule 1 third env exception, approved 2026-07-30), `vertex_credentials_json: str = ""` (env `VERTEX_CREDENTIALS_JSON` — service-account JSON, secret, env-only, never returned to any UI), `vertex_project: str = ""` (env `VERTEX_PROJECT`), `vertex_location: str = "us-central1"` (env `VERTEX_LOCATION`).
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
- **Public API:** `IngestStore.ingest_order_created(webhook_id, topic, mapping: MappingUpsert, outbound: OutboundDraft | None) -> IngestResult`; read-only views (Phase 3.5): `.recent_mappings(limit) -> list[MappingView]`, `.recent_outbound(limit) -> list[OutboundView]`; DPDP erasure/retention (2026-08-06): `.delete_by_phone(phone_e164) -> DeletionResult`, `.purge_older_than(cutoff: datetime) -> DeletionResult`. Frozen dataclasses: `MappingUpsert(order_gid, order_name, order_number_int, phone_e164, customer_name, email, language, financial_status_at_create, is_cod)`; `OutboundDraft(dedupe_key, kind, phone_e164, payload_json)`; `IngestResult(duplicate, queued)`; `MappingView(order_gid, order_name, phone_e164, status, is_cod, created_at)`; `OutboundView(dedupe_key, state, kind, phone_e164, attempts, last_error_code, created_at)`; `DeletionResult(order_mappings, outbound_messages, conversations, messages, pending_actions, order_actions)` (per-table row counts; `processed_messages` intentionally NOT a field — see below). In-memory exposes `.webhooks/.mappings/.outbound` for assertions.
- **Used in:** channels.shopify_webhook, deps.Container, admin.router (mappings/outbox views + POST /erasure), jobs.retention (purge job).
- **Notes:** duplicate `(webhook_id, topic)` → `duplicate=True, queued=False` (short-circuit). `dedupe_key` UNIQUE (`order_created:{order_gid}`) = one push per order ever; re-seen dedupe_key → `queued=False`. `outbound=None` (ineligible/backfill) maps without queueing. Postgres detects rowcount via `_rows_affected(tag)`. **DPDP (2026-08-06):** `delete_by_phone` removes rows keyed to one phone from order_mappings + outbound_messages (both by `phone_e164`) + conversations/messages (by `user_id`, children-before-parents for FK) + **pending_actions (by `wa_id`) + order_actions (by `actor_wa_id`)** in ONE Postgres transaction; `purge_older_than` (the AUTOMATIC age-based job) deletes rows older than `cutoff` ONLY from the client-approved-for-deletion tables: pending_actions/order_actions on `created_at`, conversations/messages on `last_active_at`, **and blanket-ages `processed_messages` on `received_at`**. conversations/messages have NO writer yet (Phase 4) but are cleaned defensively. **⚠ Q15 conflict fix (2026-08-06):** `purge_older_than` NO LONGER touches `order_mappings`/`outbound_messages` — the client decided customer/order data is kept INDEFINITELY (round 3, client-decisions-all.md Q15), so the age-based job excludes those two tables entirely (its `DeletionResult` always reports `order_mappings=0, outbound_messages=0`). Only `delete_by_phone` (right-to-erasure-on-request) may remove order/outbound rows, and only for a specific number. Docstrings on both impls + `retention.py` + `DeletionResult` state this so a future editor does not re-add the DELETEs; a regression test asserts the age-based purge never even queries those tables. **Security-review completeness fix (2026-08-06):** pending_actions/order_actions/processed_messages were previously MISSED (incomplete erasure). `processed_messages` has NO `phone_e164` column — its message_id is a Meta wamid that embeds the sender number in cleartext base64, so it cannot be phone-scoped without decoding wamids (out of scope, documented as a known residual in `delete_by_phone`'s docstring + `_pipeline_status.md` deferred note); it is only aged out blindly by `purge_older_than`, hence NOT a `DeletionResult` field. **In-memory** models only mappings/outbound → `delete_by_phone` prunes those two dicts, all other counts (conversations, messages, pending_actions, order_actions) stay 0; in-memory `purge_older_than` is a documented no-op (in-memory rows carry no timestamp — the real age filter is Postgres-only).

## LazyPool (asyncpg)
- **File:** backend/app/store/pg_factory.py
- **Purpose:** asyncpg connection pool created on FIRST `acquire()`, never at import (serverless cold-start rule).
- **Public API:** `LazyPool(dsn: str)`; `async with pool.acquire() as conn:`; `async close()`. Double-checked `asyncio.Lock` guards single pool creation. `create_pool(min_size=0, max_size=5, statement_cache_size=0)`.
- **Used in:** PostgresConfigRepo, PostgresIngestStore, deps.Container.
- **Notes:** asyncpg has no py.typed marker — `[[tool.mypy.overrides]] module="asyncpg.*" ignore_missing_imports=true` in pyproject.toml. **Supabase-host IPv4 pin (Vercel getaddrinfo EBUSY workaround, 2026-08-04):** when the DSN hostname contains `supabase.com`, `_get_pool` calls `_create_supabase_pool(host)` — it synchronously resolves IPv4 (`socket.getaddrinfo(host, None, family=AF_INET, type=SOCK_STREAM)[0][4][0]`) and passes `host=<ipv4>` + `ssl="require"` to `create_pool` (asyncpg kwarg overrides the DSN host; `ssl=require` encrypts without hostname verification since we connect by IP). This dodges Vercel's Python runtime raising `OSError [Errno 16] EBUSY` inside asyncio's threaded dual-stack (AF_UNSPEC) getaddrinfo. On resolution failure (`except OSError`) it falls back to the plain `create_pool(dsn, ...)` — never hard-fails worse than before. **Non-supabase DSNs are UNCHANGED** (no host override, no `ssl` kwarg) so local/other plain non-SSL Postgres still works. `statement_cache_size=0` kept on every path (transaction pooler breaks prepared statements). Wiring is unit-tested with mocks (`tests/store/test_lazypool_ipv4.py`); the bug itself only reproduces on the Vercel runtime.

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

## ConversationStore (Protocol) + InMemoryConversationStore + PostgresConversationStore + core.memory
- **File:** backend/app/store/base.py (Protocol + `StoredMessage`), backend/app/store/memory.py (in-memory), backend/app/store/postgres.py (Postgres), backend/app/core/memory.py (`load_history`/`persist_turn`)
- **Purpose:** windowed chat history + pause/handoff state per WhatsApp sender, backing the Phase 4 conversation engine.
- **Public API:** `StoredMessage(role, content, created_at: str | None)` (frozen); `ConversationStore.get_or_create(user_id) -> int`, `.recent_messages(conversation_id, limit) -> list[StoredMessage]`, `.append_message(conversation_id, role, content) -> None`, `.pause_until(conversation_id, until: datetime) -> None`, `.get_paused_until(conversation_id) -> datetime | None`, `.mark_handoff_attempted(conversation_id, at: datetime) -> None`, `.get_handoff_attempted_at(conversation_id) -> datetime | None`; `InMemoryConversationStore()`; `PostgresConversationStore(pool: LazyPool)`; `app.core.memory.DEFAULT_WINDOW = 8`, `async load_history(store, wa_id, window=DEFAULT_WINDOW) -> tuple[int, list[Message]]` (creates the conversation on first contact, replays only user/assistant turns as provider `Message`s), `async persist_turn(store, conversation_id, user_text, assistant_reply) -> None`.
- **Used in:** Phase 4 conversation engine (Tasks 12/14 — customer_support agent handoff logic, webhook wiring); wired into `deps.Container.conversations` (Task 13, picks Postgres/in-memory the same way `ingest`/`messages` do); `core.memory` depends only on the `ConversationStore` Protocol (no concrete store import), per `core` layering rule.
- **Notes:** `paused_until` = a human has already taken over; `handoff_attempted_at` (new column, this task) = one AI handoff attempt already used in the current window — deliberately a separate real column, not derived from message content, since "one attempt then immediate handoff" (client decision) needs both states to persist independently across messages. `conversations.user_id` holds the WhatsApp id; `get_or_create` picks the most-recently-active row per user and bumps `last_active_at` (no separate "close conversation" concept yet — a single ever-growing window per user, capped only by `limit`/`window` on read). `core.memory` deliberately has NO rolling-summary logic — only windowed history (last N turns), per task scope. In-memory `recent_messages` slices `list[-limit:]`; Postgres orders `DESC LIMIT` then reverses in Python to get chronological order.

## WhatsApp webhook router (GET verify + POST receive + Phase 4 conversation pipeline)
- **File:** backend/app/channels/whatsapp.py
- **Purpose:** Meta webhook edge — GET subscribe verification + POST receive (HMAC, dedupe) + (Task 14) wires every fresh `InboundText` into the router + 5-agent pipeline and sends the reply.
- **Public API:** `router` — `GET /webhook/whatsapp`, `POST /webhook/whatsapp`. Internal: `async _handle_text_event(c: Container, event: InboundText) -> None`, `async _run_agent(context: AgentContext, intent: Intent, c: Container, conversation_id: int, now: datetime) -> AgentReply`.
- **Used in:** main.app.
- **Notes:** GET: `hub.mode==subscribe` + ASCII-safe constant-time `hub.verify_token` compare → echoes `hub.challenge`, else 403. POST: 403 on bad/unconfigured HMAC; 413 if body > 1 MiB; foreign `phone_number_id` / status callback / unparseable / non-dict / unknown-type → 200 `{"ok": true, "ignored": true}`; response shape (unchanged since Phase 3) is `{"ok": true, "processed": N, "duplicate": N, "results": [{"message_id", "duplicate", "event_type"}, ...]}` (one aggregate ack per delivery, since Meta may batch several messages). **Task 14:** for every fresh (non-duplicate) `InboundText`, `_handle_text_event` now runs before the response is built — `InboundButton`/`InboundInteractive` are completely untouched (still just recorded/echoed; Phase 5 attaches their deterministic button-tap dispatch at the same seam). `_handle_text_event` order of checks (all inside one broad `try/except Exception: logger.exception(...)` so a downstream failure never breaks the webhook's 200 ack for an already-deduped message): (1) `load_controls`; `send_mode == "off"` returns immediately — no `load_whatsapp_config`, no conversation store touch, no Shopify call, no LLM call, nothing runs. (2) `load_whatsapp_config`; `None` (not configured) also returns immediately. (3) `load_history` (creates the conversation on first contact) then `get_paused_until` — if paused (a human has taken over), `append_message(..., "user", event.text)` is called so the human sees the message, then returns immediately, still before any Shopify/LLM call. (4) only past the pause gate: `resolve_by_phone` (Shopify, ownership-checked) + `count_orders_by_phone` (VIP threshold) + `active_llm` (falls back to `copy_for("error_fallback", "en")` if no provider is active) + `classify_intent` (router) + the matched specialist agent (`_run_agent` dispatches on `Intent` to `order_tracking`/`product_search`/`policy`/`recommendations`/`customer_support`, the last one taking `conversations`/`conversation_id`/`now` for its pause/handoff side effects) + `strip_markdown` on the reply. (5) `persist_turn` ALWAYS runs (both user + assistant turns), even in `shadow` mode or when `allowlist` will suppress the send — a shadow/allowlist-skipped reply is still a real conversation turn for history purposes. (6) send gate: `shadow` → return before `send_text`; `allowlist` → only sends if `normalize_phone(event.wa_id)` is in `controls.allowlist_phones`; `live` (and any other value) → always `send_text`. `phone` (E.164, may be `None` for an unparseable wa_id) is reused for VIP lookup, `AgentContext.phone_e164` (falls back to raw `wa_id` if unparseable), and the allowlist check.

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
- **Purpose:** singleton wiring of the whole Shopify layer + the Phase 4 conversation engine's provider resolution.
- **Public API:** `Container` dataclass (settings, vault, config_repo, config, http, tokens, shopify, ingest, messages, `conversations: ConversationStore`); `get_container() -> Container` (module singleton); `reset_container() -> None` (tests); `build_provider(settings: Settings) -> LiteLLMProvider` (constructs the LLM verifier with a `VertexConfig` from `settings.vertex_*`); `async active_llm(settings: Settings, config: ConfigService) -> tuple[LLMProvider, str, str, dict[str, object] | None] | None` (Task 13).
- **Used in:** FastAPI app / routes, tests, admin.router (POST /provider verifier); Phase 4 conversation engine (router/agents call `active_llm` to get a ready-to-call provider).
- **Notes:** when `settings.database_url` is set → `PostgresConfigRepo` + `PostgresIngestStore` + `PostgresMessageStore` + `PostgresConversationStore` over ONE shared `LazyPool` (no connection made at build time — LazyPool connects on first acquire); else the four in-memory impls. `http = AsyncClient(follow_redirects=False)`. `build_provider` runs at call time (never at import) — `LiteLLMProvider` still imports litellm lazily inside `complete`, so nothing here touches the webhook cold path; it wires `VertexConfig(vertex_credentials_json or None, vertex_project or None, vertex_location)` so verification uses env creds. `active_llm` mirrors `admin.router`'s own resolution of `llm:active_provider`/`llm:api_key:{provider}` (GET /config, POST /provider): reads the active provider key via `config.get_plain`, looks it up in the provider registry, and for `auth_kind="env"` (Vertex) returns `(provider, default_model, "", request_params)` with no stored key required; for `auth_kind="api_key"` it requires `config.get_secret(f"llm:api_key:{active}")` to be present, else returns `None`. Returns `None` if nothing is active yet or an unknown provider key is stored. Built via `build_provider(settings)`, so every call constructs a fresh `LiteLLMProvider` (cheap — litellm import stays lazy inside `complete`).

## FastAPI app + GET /health + routers
- **File:** backend/app/main.py (app), backend/api/index.py (Vercel ASGI entrypoint)
- **Purpose:** deployable FastAPI app; liveness probe; mounts the Phase 2 routers.
- **Public API:** `app = FastAPI(...)`; `GET /health` → `{"status": "ok", "service": "thetavas-order-bot"}`. Includes `shopify_webhook_router`, `whatsapp_router`, `jobs_router`, and `admin_router`; registers slowapi (`app.state.limiter` + `RateLimitExceeded` handler), a global `RequestValidationError` handler (`_validation_handler`), `AdminBodyCapMiddleware` + `AdminSecurityHeadersMiddleware`, and mounts the static admin panel at `/admin/ui` (`StaticFiles(..., html=True)`).
- **Used in:** Vercel deploy (`vercel.json`, region bom1).
- **Notes:** entrypoint `api/index.py` re-exports `app`; all routes → `api/index.py`. Phase 1 security fix: OpenAPI/docs/redoc are DISABLED in prod — `_docs_enabled()` reads `Settings().app_env` (docs off when `app_env == "prod"`, default on otherwise / on missing key). Preserve this construction when adding routers. `AdminBodyCapMiddleware` must be added at module import (before first request) — it 413s oversized `/admin` requests by Content-Length before FastAPI parses the body. **Security-review fix (2026-07-30):** global `_validation_handler` for `RequestValidationError` returns 422 with each error dict stripped of `input`/`url`/`ctx` — the default handler echoes `input` verbatim, leaking over-length creds (api_key/client_secret/access_token/password) into the body. NOTE: FastAPI's `RequestValidationError.errors()` takes NO kwargs (unlike pydantic's `ValidationError.errors(include_input=...)`) — filter the dicts by hand. `AdminSecurityHeadersMiddleware` added LAST so it is outermost (headers on every `/admin` response incl. rejections).

## Jobs dispatcher (internal, authenticated)
- **File:** backend/app/jobs/router.py
- **Purpose:** single authenticated cron/self-invoke endpoint running a named-job registry.
- **Public API:** `router` — `GET|POST /internal/jobs/{name}`; `JOBS: dict[str, JobFn]` registry; `JobFn = Callable[[Container], Awaitable[dict[str, Any]]]`. Registered: `ensure_subscription` (reads config `public_base_url`, calls subscriptions.ensure_subscription against `{base}/webhooks/shopify`); `retention_purge` (DPDP age-based purge, 2026-08-06).
- **Used in:** main.app, Vercel cron (future).
- **Notes:** `settings.cron_secret` empty/<16 chars → 503 (never an open endpoint, F11); header `X-Cron-Secret` constant-time compared → 403 on mismatch/missing; unknown job → 404; `ensure_subscription` with no `public_base_url` → 200 `{"error": "public_base_url not configured"}`. A job raising any `ShopifyError` (base class) → 502 `{"job": name, "error": "job failed"}` — exception text is NEVER echoed (may carry vendor detail); non-`ShopifyError` exceptions still propagate as raw 500.

## Retention purge job (DPDP, config-gated)
- **File:** backend/app/jobs/retention.py
- **Purpose:** scheduled-job-ready age-based purge across the erasure/retention tables (item 5).
- **Public API:** `async run_retention_purge(c: Container) -> dict[str, Any]`; registered as the `retention_purge` job.
- **Used in:** jobs.router (JOBS registry → `GET|POST /internal/jobs/retention_purge`, CRON_SECRET-authed).
- **Notes:** reads `retention_days` from `load_controls` (ADR-005 config). **DEFAULT 0 = disabled → `{"status": "disabled"}` no-op** (no policy invented until client answers Q15). When > 0: `cutoff = now(UTC) - retention_days days` → `c.ingest.purge_older_than(cutoff)` → `{"status": "purged", "retention_days", "deleted": {...per-table counts}}`. Cron wiring is live (safe: default is a no-op); flip on by setting `retention_days` from the panel once the client confirms the period.

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
- **Public API:** `admin_router: APIRouter` (prefix `/admin`); `require_admin(admin_session: str | None = Cookie())` dependency (401 unless the session cookie verifies); `AdminBodyCapMiddleware` (ASGI, 413 on oversized `/admin` bodies); `_audit(action, outcome, *, resource=None, detail=None)` actor-free audit helper (2026-08-06). Endpoints: `POST /login` (rate-limited 5/min, 503 if unset password, 401 on wrong, sets httponly `admin_session` cookie), `GET /session`, `GET|POST /shopify`, `GET|POST /whatsapp`, `GET /providers`, `GET /config`, `POST /provider`, `GET|PUT /knowledge/{kind}`, `GET|PUT /controls`, `GET /mappings`, `GET /outbox`, `POST /erasure` (DPDP delete-by-phone).
- **Used in:** main.app, static panel (`/admin/ui`).
- **Notes:** every route except `/login` depends on `require_admin`. **Audit logging (item 4, 2026-08-06):** `_audit` writes `admin_audit action=… [resource=…] outcome=…` at INFO on `logger("app.admin")` (lands in Vercel function logs, no new infra). Actor-free (single shared `ADMIN_PASSWORD` → no per-user identity); logs the action/resource-NAME/outcome ONLY, NEVER a value (passwords, keys, credential contents, knowledge bodies, or the erased phone). Wired at: login success/failure/not_configured; `credential_set` (resource `shopify`/`whatsapp`); `provider_set` (resource `llm:{provider}`, all 3 success paths); `knowledge_set` (resource `knowledge:{kind}`); `controls_set`; `erasure` (resource `phone`, never the number). **DPDP erasure (item 5, 2026-08-06):** `POST /erasure` `{phone: E.164 ^\+[0-9]{7,15}$}` (rate-limited `@limiter.limit("10/minute")`, needs `request: Request` like login) → `ingest.delete_by_phone` → `{ok, deleted:{order_mappings,outbound_messages,conversations,messages,pending_actions,order_actions}}`; a bad phone → 422 with `input` stripped by the global handler (PII never echoed). **Security-review fixes (2026-08-06):** (a) erasure audit resource is now `phone:{_phone_fingerprint(phone)}` — an HMAC-SHA256 of the number keyed with `settings.app_master_key` (the Fernet key REUSED as an HMAC key, no new secret), truncated to 16 hex chars: recomputable from a customer's number to correlate/prove compliance, not PII at rest, not reversible; the audit line also appends `deleted={sum of counts}` (blast radius without touching the response body). `_audit` gained an optional `detail=` suffix param for this. (b) E.164 pattern pinned to `[0-9]` (was `\d`, Unicode-aware → Arabic-Indic/fullwidth digits passed yet matched zero rows = false success + fake audit); the `ErasureRequest` body pattern stays `^\+[0-9]{7,15}$` (pydantic-core regex, not vulnerable to the Python `re` `$`-before-newline quirk). (c) `require_admin` takes `request: Request` and emits `_audit("authz","denied", resource=…)` before every 401. **ROUND 2 (2026-08-06):** the resource is now the matched route TEMPLATE via `_audit_resource(request)` (`request.scope["route"].path`, e.g. `/admin/knowledge/{kind}`) — round 1 logged the raw `request.url.path`, which uvicorn percent-decodes (`%0A`→literal newline in `scope["path"]`) BEFORE auth, letting an unauthenticated attacker split the log line and forge a fake `outcome=success` record (or flood a 4000+ char line). `_sanitize_path(raw)` (strip C0 controls + DEL via `_CONTROL_CHARS_RE`, truncate to `_MAX_RESOURCE_LEN=128`, before the logger) is the fallback used only when no route matched. Secrets are NEVER echoed — GET status routes return `{"configured": bool}` + non-secret fields only. Creds POSTs are partial-update (blank/omitted keeps the stored value; first-time setup requires the full set). `_clean(v)` trims blank→None. LLM `POST /provider` verifies with the provider BEFORE persisting (verify-key-before-save); it builds the verifier via `deps.build_provider(settings)` (Vertex env creds wired in). Provider verify is monkeypatchable at `app.admin.router.verify_key`. **Vertex/env-auth branch:** `GET /providers` returns `auth_kind` per provider; `set_provider` branches on `info.auth_kind` — `env` (Vertex) verifies with an empty key + `info.request_params` and, on success, sets `llm:active_provider` WITHOUT storing any key (on failure `_env_verify_detail(kind)` returns a SAFE message, never `raw_error`, which could embed the service-account JSON); `api_key` keeps the require-key + store-key path. **Env failure wording (code-review LOW 2026-08-04):** env-auth providers have their OWN message map `_ENV_KIND_MESSAGES` (AUTH/RATE_LIMIT/NOT_FOUND/TIMEOUT) that references "the service-account JSON, project, and location" — NOT "the API key" (Vertex has no key). `_env_verify_detail` maps a known kind via `_ENV_KIND_MESSAGES`, else (UNKNOWN/None) falls back to `_ENV_VERIFY_FAILED`; the api_key wording in `_KIND_MESSAGES` is used only by the api_key path. `GET /config` reports `configured=true` for an active env provider (no key needed) OR an api_key provider with a stored key. **Security-review fixes (2026-07-30):** (1) login cookie `secure` flag now comes from `settings.app_env == "prod"` (NOT the client-supplied `x-forwarded-proto`) — prod always forces Secure, dev/tests over http stay usable; (2) `PUT /knowledge/{kind}` maps pydantic `ValidationError` with `errors(include_url=False, include_input=False, include_context=False)` — a custom field_validator's raw `ValueError` in `ctx` was non-serializable → 500; (3) `WhatsAppCredsRequest` `phone_number_id`/`waba_id` are `pattern=r"^\d{5,20}$"`, `api_version` `pattern=r"^v\d+\.\d+$"` (path-like junk can't reach the Graph URL; still optional so partial update works); (4) `set_provider` `UNKNOWN` provider-error kind → generic "Could not verify the key with the provider." (added to `_KIND_MESSAGES`), never the raw litellm/vendor text.

## AdminBodyCapMiddleware
- **File:** backend/app/admin/router.py
- **Purpose:** cap `/admin` request bodies at the ASGI layer, before FastAPI parses the body — two rejections so the cap cannot be bypassed at the edge.
- **Public API:** `AdminBodyCapMiddleware(app, max_body=1_048_576)` (pure ASGI); registered via `app.add_middleware(AdminBodyCapMiddleware)`.
- **Used in:** main.app.
- **Notes:** a router-level dependency CANNOT do this — FastAPI parses (and may 422 on) the JSON body before path dependencies run, so an oversized invalid body would 422 before any cap check. Enforced on Content-Length + explicit no-length rejection at the ASGI layer, matching the webhook edges' raw-body posture: (1) `Content-Length` over the cap → **413**; (2) a body-bearing method (POST/PUT/PATCH) with NO `Content-Length` header — e.g. chunked transfer-encoding, which would otherwise skip the header check and reach a pre-auth route unbounded — → **411** (length required). Browsers/fetch always send Content-Length for JSON, so 411 rejects only unusual clients. Only inspects `/admin` paths.

## AdminSecurityHeadersMiddleware
- **File:** backend/app/admin/router.py
- **Purpose:** set security response headers on every `/admin` response (JSON API + static panel) — security-review fix (2026-07-30).
- **Public API:** `AdminSecurityHeadersMiddleware(app)` (pure ASGI); registered via `app.add_middleware(...)` LAST so it is the outermost middleware. Module tuple `_ADMIN_SECURITY_HEADERS`.
- **Used in:** main.app.
- **Notes:** sets `Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'` (scripts stay `'self'` — external same-origin `admin.js` is allowed, inline `<style>`/`style="..."` covered by `style-src 'unsafe-inline'`, NO `'unsafe-inline'` for scripts) + `X-Frame-Options: DENY` + `X-Content-Type-Options: nosniff` + `Referrer-Policy: no-referrer` + `Cache-Control: no-store` (mappings/outbox views carry customer phone PII — must never be cached). Replaces any pre-existing header of the same name (dedupe) then appends, so `Cache-Control` can't double up. Only inspects `/admin` paths.

## Admin knowledge validation models
- **File:** backend/app/admin/knowledge_models.py
- **Purpose:** kind-specific Pydantic validation for knowledge PUTs; serialize to the cafe-loader-compatible stored format.
- **Public API:** `validate_and_serialize(kind, payload: dict[str, object]) -> str` (raises `pydantic.ValidationError` → router maps to 422; unknown kind → `KeyError`, guarded upstream). Models `BrandVoiceBody`, `FaqBody`/`FaqItem`, `PatternsBody`/`PatternItem`, `BusinessBody`.
- **Used in:** admin.router (PUT /knowledge/{kind}).
- **Notes:** faq/patterns store a JSON **list**, business a JSON **object**, brand_voice raw markdown — so `KnowledgeLoader` reads overrides and seeds identically. Size caps: brand_voice ≤100k chars; faq 1..200 items (q/a ≤2000); patterns 1..100 items (≤20 examples, **each example ≤200 chars** — security-review 2026-07-30, per-element cap so one giant example can't inflate the Phase-4 prompt); business fields ≤2000, extra dict ≤50 pairs (each key ≤200 / value ≤2000, `_cap_extra_entry_lengths`).

## Admin operational controls
- **File:** backend/app/admin/controls.py
- **Purpose:** ADR-002 (send-mode kill switch) + ADR-005 (client decisions as config) document; each field persists as its own plain `app_config` key so runtime readers keep their existing keys.
- **Public API:** `AdminControls` (Pydantic) + `TagLists`; `async load_controls(config) -> AdminControls` (stored-or-default, never crashes on corrupt values); `async save_controls(config, controls) -> None`; `REVEAL_ALLOWED`.
- **Used in:** admin.router (GET|PUT /controls); the individual keys are read by webhook eligibility / jobs / future outbox drain.
- **Notes:** validated: `send_mode` ∈ off/shadow/allowlist/live; `push_policy` ∈ cod_only/all/all_prepaid_no_buttons; `default_language` ∈ en/hi/gu; `reveal_fields` ⊆ {order_number,email,status}; `allowlist_phones`/`owner_alert_number` E.164; `public_base_url` https-or-empty (trailing slash stripped); `push_staleness_hours` 1..168; **`retention_days` 0..3650, DEFAULT 0 = disabled** (DPDP retention window, 2026-08-06 — 0 invents no policy; exact period is pending client decision Q15; persisted like `push_staleness_hours` via the `_INT_KEYS` int-string path). **Security-review fix (2026-08-06, ROUND 2 — supersedes round 1):** the `_INT_KEYS` load path now requires `raw.isascii() and raw.isdigit()` BEFORE `int()` (round 1's `try:int()` closed the `"²"` crash but `int("٣٠")==30` still enabled an unintended retention period — a bare int() is Unicode-permissive); `_E164_RE` pinned to `^\+[0-9]{7,15}\Z` (round 1 used `[0-9]` but kept `$`, which matches before a trailing newline — `\Z` rejects `"+91…\n"`) so allowlist_phones/owner_alert_number reject Arabic-Indic/fullwidth digits and trailing newlines. **each tag ≤64 chars (`_Tag` Annotated cap — security-review 2026-07-30; Shopify caps tags at 255 so an over-long tag would fail in tagsAdd)**. EXISTING keys reused unchanged: `push_policy`, `push_staleness_hours`, `public_base_url`. New keys: `send_mode`, `allowlist_phones`, `reveal_fields`, `tags`, `default_language`, `owner_alert_number`. **`load_controls` per-field fail-safe (security-review 2026-08-06 ROUND 2 — supersedes the round-1 whole-doc fallback):** validation goes through `_validate_per_field(data)` — it validates the assembled dict and, on a `ValidationError`, drops only the top-level keys pydantic reports invalid and re-validates, so ONE corrupt field (e.g. an out-of-range `retention_days`) defaults only itself instead of resetting the whole document (round 1 fell back to `AdminControls()` on any error, which silently re-widened a deliberately-narrowed `reveal_fields` back to the PII-exposing default).

## KnowledgeLoader + Thetavas seeds
- **File:** backend/app/knowledge/loader.py (+ backend/app/knowledge/seeds/{brand_voice.md,faq.json,business.json,patterns.json})
- **Purpose:** override-else-seed reader for the store's voice + policy knowledge (cafe pattern, our names).
- **Public API:** `KnowledgeLoader(repo: ConfigRepo, seeds_dir: Path)` with `get(kind) -> str`, `knowledge_version() -> str` (config `knowledge_version` or "0"), `assemble_all() -> dict[str, str]`; module `KINDS = ("brand_voice","faq","business","patterns")`, `SEEDS_DIR: Path`.
- **Used in:** admin.router (knowledge GET); Phase 4 engine (prompt assembly) will consume it.
- **Notes:** DB override (from `knowledge_overrides`) wins; else the packaged seed FILE. Seeds are Thetavas defaults (no emojis, plain text, no menu — we don't sell in chat); business support fields are blank for the store team to fill from the panel. `knowledge_version` bumps on every PUT (Phase 4 cache invalidation).

## Providers layer (LLM behind LLMProvider)
- **File:** backend/app/providers/{base.py,registry.py,litellm_provider.py,verify.py}
- **Purpose:** minimal Phase 3.5 LLM provider abstraction — enough to verify a provider's credentials before saving; full engine use is Phase 4.
- **Public API:** base: `LLMProvider` Protocol (`complete(model, messages, api_key, timeout, *, extra_params=None) -> CompletionResult`), `Message`, `CompletionResult`, `ProviderError(.kind)`, `ProviderErrorKind(StrEnum: AUTH/RATE_LIMIT/NOT_FOUND/TIMEOUT/UNKNOWN)`. registry: `AuthKind = Literal["api_key","env"]`; `ProviderInfo(key,label,default_model,accept_on_rate_limit=False,auth_kind: AuthKind="api_key",request_params=None)`, `PROVIDERS` (`gemini` api_key + `vertex` env), `get_provider(key)`, `list_providers()`. litellm_provider: frozen `VertexConfig(credentials_json,project,location)`; `LiteLLMProvider(vertex: VertexConfig|None=None)` (lazy `import litellm` inside `complete`; api_key scrubbed from api_key-provider errors, vertex errors collapsed to a fixed message). verify: `VerifyResult(ok,error,kind)`, `async verify_key(provider, model, api_key, timeout=15.0, *, extra_params=None) -> VerifyResult` (never raises, never leaks the key/creds).
- **Used in:** admin.router (GET /providers, GET /config, POST /provider verify-before-save), deps.build_provider.
- **Notes:** NEW dep `litellm>=1.89,<2` (untyped → `[[tool.mypy.overrides]] module="litellm.*"`), imported LAZILY so the webhook cold path never pays its import cost (rule F10). `ProviderErrorKind` is a `StrEnum` (repo ruff UP042). **Two providers:** `gemini` (`auth_kind="api_key"`, direct key pasted+stored encrypted) and `vertex` ("Gemini (Vertex AI)", model `vertex_ai/gemini-3.5-flash`, `auth_kind="env"`, `request_params={"temperature":0.3,"reasoning_effort":"medium"}`). `ProviderInfo` with a dict `request_params` is unhashable — fine, only stored as dict values. **LiteLLMProvider.complete** branches on the model: `vertex_ai/*` injects `vertex_credentials/vertex_project/vertex_location` (from `VertexConfig`) and NO `api_key` — raises `ProviderError(AUTH, "Vertex AI credentials are not configured")` if creds missing; all other models inject `api_key` as before. **Error handling (security-review LOW-1 2026-08-04):** a `vertex_ai/*` upstream error collapses to a FIXED `ProviderError("Vertex AI request failed", kind)` — the raw litellm text is DISCARDED entirely (exact-substring redaction can't catch a reformatted/re-serialized copy or a lone `private_key` field of the service-account JSON). Only the api_key path still uses `_redact(str(exc), api_key)` (now a 2-arg helper — `model`/`vertex` params removed). `verify_key`'s `extra_params` forwards a provider's `request_params` so env providers verify with their tuned params. `ProviderInfo.auth_kind` is `Literal["api_key","env"]` (`AuthKind`), not bare `str`.
