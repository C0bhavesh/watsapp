# API Registry

> Every HTTP endpoint and external integration. Grep before adding a new route or external call — never create a parallel implementation.

## Format
## [METHOD /path]
- **Handler:** app/path/to/router.py
- **Request:** pydantic model
- **Response:** pydantic model
- **Notes:** auth, HMAC, rate limit, idempotency

---
<!-- entries below -->

## [GET /health]
- **Handler:** backend/app/main.py (`health()`)
- **Request:** none
- **Response:** `dict[str, str]` → `{"status": "ok", "service": "thetavas-order-bot"}`
- **Notes:** unauthenticated liveness probe. Vercel entrypoint `backend/api/index.py`, region bom1.

## [POST /webhooks/shopify]
- **Handler:** backend/app/channels/shopify_webhook.py (`shopify_webhook()`)
- **Request:** raw Shopify webhook body (JSON); headers `X-Shopify-Hmac-Sha256`, `X-Shopify-Topic`, `X-Shopify-Webhook-Id`.
- **Response:** 403 `forbidden` (bad/missing HMAC or unset secret); 200 `{"ok": true, "ignored": true}` (non-`orders/create` topic, missing webhook id, or unparseable/non-dict body); 200 `{"ok": true, "duplicate": bool, "queued": bool}` on ingest.
- **Notes:** HMAC = base64(HMAC-SHA256(raw body, config `shopify:client_secret`)), constant-time. NO network calls — one atomic `IngestStore.ingest_order_created` transaction then respond (ADR-001; 5xx → Shopify retries). Idempotent on `(webhook_id, topic)`. Eligibility from config `push_policy` (default `cod_only`) + `push_staleness_hours` (default 6); queued push dedupe_key `order_created:{gid}`. Outbox payload_json: `{template:"order_confirmation_cod", language, customer_name, order_name, amount}`.

## [GET|POST /internal/jobs/{name}]
- **Handler:** backend/app/jobs/router.py (`run_job()`)
- **Request:** header `X-Cron-Secret`; path `{name}`.
- **Response:** 503 if `settings.cron_secret` unset; 403 on secret mismatch/missing; 404 unknown job; 200 `{"job": name, "result": ...}`.
- **Notes:** constant-time secret compare (F11). Registered job: `ensure_subscription` (→ `{"status": "ok|created|updated"}` or `{"error": "public_base_url not configured"}`).

## [external] Shopify Admin token endpoint (client_credentials)
- **Caller:** backend/app/shopify/token_manager.py (`TokenManager._grant`)
- **Endpoint:** `POST https://{shop_domain}/admin/oauth/access_token`
- **Request:** form data `grant_type=client_credentials`, `client_id`, `client_secret` (secrets from ConfigService).
- **Response:** `{"access_token": "shpat_...", "expires_in": <sec>}`; persisted encrypted + cached with 1h refresh margin.
- **Notes:** non-200 → `TokenGrantError` (no secret in message). 24h token; refresh <1h before expiry / on 401.

## [external] Shopify Admin GraphQL API (2026-07)
- **Caller:** backend/app/shopify/client.py (`ShopifyClient._graphql` + 5 ops)
- **Endpoint:** `POST https://{shop_domain}/admin/api/{shopify_api_version}/graphql.json` (version from Settings, pinned 2026-07)
- **Request:** `{"query": ..., "variables": ...}`, header `X-Shopify-Access-Token`.
- **Response:** `{"data": ..., "errors": ...}` — see ShopifyClient notes for error mapping.
- **Notes:** ops — get_order, find_order_by_name, find_customer_orders_by_phone (reads); tagsAdd, orderCancel (mutations, AuthorizedOrder-gated, ADR-004). Orders NOT searchable by phone directly (error_learnings 2026-07-28) — customer→orders fallback. 401 → single force-refresh retry.

## [external] Shopify webhook subscription management (ORDERS_CREATE)
- **Caller:** backend/app/shopify/subscriptions.py (`ensure_subscription`)
- **Endpoint:** Admin GraphQL — `webhookSubscriptions(first:20, topics:[ORDERS_CREATE])` (list), `webhookSubscriptionCreate` (topic ORDERS_CREATE, format JSON), `webhookSubscriptionUpdate` (on URL drift).
- **Request/Response:** create/update take `$callbackUrl: URL!` (update also `$id: ID!`); return `{webhookSubscription{id}, userErrors{message}}`.
- **Notes:** invoked via the `ensure_subscription` job (`GET|POST /internal/jobs/ensure_subscription`). callbackUrl = `{public_base_url}/webhooks/shopify`. userErrors → `ShopifyGraphQLError`.
