# FastAPI Layering (ports & adapters)
Modules and their one responsibility (architecture-plan Level 3):
- `app/channels/` — Meta webhook (GET verify + POST), typed inbound parsing, sender (`send_text`/`send_template`), Shopify `orders/create` receiver. HMAC + idempotency live here at the edge.
- `app/shopify/` — TokenManager (client-credentials, 24h cache, refresh <1h/on-401), GraphQL client (orders query, tagsAdd, orderCancel, orderUpdate), webhook-subscription self-heal.
- `app/core/` — Gemini JSON-intent engine, windowed memory, sanitize, order_resolver (phone→orders + ownership check). No framework/provider/Shopify/db details leak in.
- `app/providers/` — LLM access behind an `LLMProvider` interface (LiteLLM adapter). Swappable by config.
- `app/config/` — pydantic-settings + dynamic config + Fernet key vault.
- `app/store/` — repositories behind interfaces: `order_mappings`, `processed_webhooks`, conversations, `app_config` (Postgres + in-memory impls).
- `app/admin/` — creds entry, mappings view, auth, minimal panel.
Rule: dependencies point inward; `core` depends on interfaces, not concrete adapters. Deferred features (address change, handoff, a2ship) add adapters, never restructure.
