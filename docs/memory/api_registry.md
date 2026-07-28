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
