# Reference Project: D:\ai_whatsapp_agent ("Beyond Loaf" cafe WhatsApp agent)

> Raw exploration report (2026-07-27). This is the existing cafe-order automation we
> will reuse patterns from for the Shopify project.

Mature, well-tested Python/FastAPI POC (~5.1k LOC app code, ~70 test files, deployed on Vercel). Branch: `feat/petpooja-integration`.

---

## 1. Overall structure & tech stack

**Language/framework:** Python 3.12+ · FastAPI · Pydantic v2 + pydantic-settings · async throughout · mypy `strict` · ruff.

**Key deps** (`backend\requirements.txt`, `backend\pyproject.toml`):
```
fastapi>=0.115 · pydantic>=2.7 · pydantic-settings>=2.3
litellm>=1.89,<2          # LLM abstraction (all providers incl. Gemini/Vertex)
google-auth>=2.41         # Vertex AI service-account auth for litellm
cryptography>=42          # Fernet encryption of secrets at rest
httpx>=0.27               # WhatsApp Graph API calls
asyncpg>=0.29             # Postgres (Supabase)
slowapi>=0.1.9            # rate limiting
dev: pytest, pytest-asyncio (asyncio_mode=auto), ruff, mypy
```

**Layout** — ports & adapters ("hexagonal-lite"), documented in `docs\instructions\ARCHITECTURE.md`:
```
backend/
  api/index.py                  # Vercel ASGI entrypoint: `from app.main import app`
  vercel.json                   # @vercel/python, all routes -> api/index.py, region icn1
  app/main.py                   # FastAPI app, CORS, TimingMiddleware, routers, /health
  app/deps.py                   # composition root (DI container, @lru_cache)
  app/channels/                 # transports: REST /chat + WhatsApp webhook + Graph senders
  app/core/                     # ConversationEngine + memory/language/handoff/sanitize/timing
  app/providers/                # LLMProvider Protocol + LiteLLM adapter + key verifier
  app/knowledge/                # seed loader, system-context assembler, cache, MenuSource
  app/catalog/                  # Meta catalog sync, message builders, order parser, menu list
  app/config/                   # Settings, Fernet crypto, DynamicConfig, provider registry
  app/store/                    # Repo Protocols + Postgres (asyncpg) + in-memory impls + schema.sql
  app/admin/                    # admin API + static panel (login, provider/WA config, knowledge)
  tests/                        # ~70 test modules mirroring app/
web-tester/                     # separate static Vercel project (browser chat tester)
docs/                           # specs, plans, memory/registries
```

**Dependency rule:** `core` imports only Protocols (`providers.base`, `store.base`, `knowledge.menu_source`) — never `litellm_provider` or `postgres_impl`. All wiring in `app/deps.py`.

---

## 2. WhatsApp integration

**Provider: Meta WhatsApp Cloud API (Graph API) directly over httpx.** No Baileys, no Twilio, no SDK — hand-rolled JSON payloads.

### Receiving — webhook (2 endpoints)
`backend\app\channels\whatsapp.py`

- `GET /webhook/whatsapp` — Meta hub verification, constant-time token compare:
```python
mode = params.get("hub.mode"); token = params.get("hub.verify_token")
challenge = params.get("hub.challenge")
expected = await store.get_verify_token()
if mode == "subscribe" and hmac.compare_digest(token, expected) and challenge is not None:
    return PlainTextResponse(challenge, status_code=200)
return PlainTextResponse("forbidden", status_code=403)
```

- `POST /webhook/whatsapp` — the main receive path. Pipeline (always returns 200 except 403, so Meta never retries):
```python
raw = await request.body()
cfg = await store.get()                                   # decrypted creds from DB
if not verify_signature(raw, request.headers.get("x-hub-signature-256"), cfg.app_secret):
    return PlainTextResponse("forbidden", status_code=403)
payload = json.loads(raw)
event = extract_event(payload)                            # typed union or None
if event is None or _seen(event.message_id): return 200   # dedup
# dispatch: InboundOrder -> _handle_order
#           InboundInteractive -> deterministic tap router, else engine
#           InboundText -> engine.handle + _render_engine_result
```

- **Signature verification** — `backend\app\channels\whatsapp_signature.py`:
```python
expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
return hmac.compare_digest(header[len("sha256="):], expected)
```

- **Dedup** — in-process LRU `OrderedDict` capped at 1000 message_ids (`_seen()`), plus DB-level idempotency for orders (`ON CONFLICT DO NOTHING`).

- **Payload parsing** — `backend\app\channels\whatsapp_inbound.py` turns `entry[0].changes[0].value.messages[0]` into a typed union `InboundText | InboundInteractive | InboundOrder | None`. Status callbacks and unknown types return `None` (no exception). Frozen dataclasses; all parsing wrapped in `except (KeyError, IndexError, TypeError)`.

### Sending
`backend\app\channels\whatsapp_sender.py` — one shared `_post_message` helper:
```python
def _messages_url(cfg): return f"https://graph.facebook.com/{cfg.api_version}/{cfg.phone_number_id}/messages"

headers = {"Authorization": f"Bearer {cfg.access_token}", "Content-Type": "application/json"}
resp = await active.post(url, json=payload, headers=headers, timeout=timeout)
# httpx.HTTPError -> WhatsAppSendError; >=400 -> SendResult(ok=False, status, None)
```
Senders: `send_text`, `send_single_product` (SPM), `send_multi_product` (MPM), `send_catalog_message`, `send_interactive_list`.

Payload builders (pure, no I/O) in `backend\app\catalog\message_builder.py` — `interactive.type` of `product` / `product_list` / `catalog_message` / `list`, with Meta hard caps encoded (MPM: ≤30 products, ≤10 sections, silently truncated; interactive list: ≤10 rows total, button ≤20 chars, row title ≤24 — raises `ValueError`).

### Templates vs free-form
**No template (HSM) messages anywhere.** Every outbound message is a free-form session message (`type: "text"` or `type: "interactive"`), which only works inside the 24-hour customer service window. **This is the single biggest gap for the Shopify project** — proactive order notifications (order created / shipped / cancelled) outside 24h require `type: "template"` with pre-approved templates and a components/parameters array; add a `send_template()` alongside `send_text()` using the identical `_post_message` helper.

### Credentials storage
`backend\app\channels\whatsapp_config.py` — creds live in the Postgres `app_config` table under `whatsapp:*` keys, secrets Fernet-encrypted, entered via the admin panel (not env):
```python
_SECRET_FIELDS = ("access_token", "app_secret", "verify_token")   # encrypted
_PLAIN_FIELDS  = ("phone_number_id", "waba_id", "api_version")    # plain
_OPTIONAL_PLAIN_FIELDS = ("owner_alert_number", "catalog_id")
```

---

## 3. Gemini / LLM integration

**Abstraction:** LiteLLM, so the provider is config-swappable at runtime. Registry at `backend\app\config\provider_registry.py`:
```python
"gemini":  ProviderInfo("gemini", "Gemini", "gemini/gemini-flash-latest"),
"vertex":  ProviderInfo("vertex", "Gemini (Vertex AI)", "vertex_ai/gemini-3.5-flash",
                        auth_kind="env",
                        request_params={"temperature": 0.3, "reasoning_effort": "medium"}),
# also: openai/gpt-4o, anthropic/claude-sonnet-4-6, openrouter, nvidia_nim (Sarvam-M)
```
Production path is **Vertex AI Gemini 3.5 Flash** with `reasoning_effort="medium"` (comment notes `"minimal"` caused "lazy 2–3 item sampling" during product selection).

**Adapter:** `backend\app\providers\litellm_provider.py`
- `litellm.disable_aiohttp_transport = True` (documented fix for spurious serverless timeouts).
- Vertex auth injects service-account JSON instead of an api_key:
```python
if model.startswith("vertex_ai/"):
    call_kwargs["vertex_credentials"] = v.credentials_json
    call_kwargs["vertex_project"] = v.project
    call_kwargs["vertex_location"] = v.location
else:
    call_kwargs["api_key"] = api_key
resp = await litellm.acompletion(model=model, messages=msg_dicts, timeout=timeout, **call_kwargs)
```
- Error mapping to `ProviderErrorKind` (AUTH/RATE_LIMIT/NOT_FOUND/TIMEOUT/UNKNOWN), one retry with 0.4 s backoff on transient kinds only, and `_redact()` to scrub credentials from error strings.

**Intent extraction: JSON-in-a-single-completion — NOT function calling, NOT plain text.** The whole prompt+parse lives in `backend\app\core\engine.py`:

```python
_SELECTION_INSTRUCTION = (
    "You can show tappable product cards. For EVERY customer message, work through these steps\n"
    "IN ORDER and respond with ONLY a JSON object, nothing else:\n"
    '{"analysis": "<1 short line: what is the customer asking, any language or typo resolved>",\n'
    ' "intent": "products" | "menu" | "chat",\n'
    ' "product_type": ["<product type(s) you identified, empty if none>"],\n'
    ' "reply": "<short warm reply in the customer\'s language>",\n'
    ' "product_ids": ["<id>", ...]}\n'
    ...
)
_VALID_INTENTS = frozenset({"products", "menu", "chat"})
```

Prompt assembly (one system message + windowed history):
```python
parts = [system_ctx,                      # brand voice + menu + FAQ + business + patterns
         _build_catalog_block(products),  # "- {retailer_id} | {name} | {category} | ₹{price} | {desc} | bestseller:{y/n}"
         _SELECTION_INSTRUCTION]
if ctx.summary: parts.append(f"Conversation so far: {ctx.summary}")
parts.append(f"Reply in the customer's language ({language}).")
messages = [Message("system", "\n\n".join(parts))] + ctx.recent
result = await self._provider.complete(model, messages, api_key, self._timeout, extra_params=extra)
selection = parse_llm_selection(result.text, valid_ids={p.retailer_id for p in products})
```

`parse_llm_selection()` is the hardening layer worth copying verbatim:
- `strip_reasoning()` removes `<think>` blocks, strips ``` fences;
- tries `json.loads`, then falls back to the outermost `{...}` substring;
- **validates every returned id against the real catalog** (unknown ids dropped, capped at 30);
- derives `intent` from `product_ids` when the model omits/mangles it;
- `_looks_like_json_attempt()` guarantees raw JSON never reaches the customer — a failed parse that looks like JSON returns a brand-voice fallback string instead.

Also: `strip_markdown()` (`app/core/sanitize.py`) because WhatsApp renders `**bold**` literally.

**Observability:** `backend\app\obs\litellm_callback.py` registers a `litellm.CustomLogger` logging model/latency/prompt+completion/cached tokens — metadata only, never message content.

---

## 4. Conversation / session state

**Postgres (Supabase via asyncpg), with an in-memory fallback when `DATABASE_URL` is unset.** Engine compute is stateless (Vercel-serverless-safe).

Schema — `backend\app\store\schema.sql`:
```sql
conversations(id PK, user_id, running_summary, last_active_at, created_at)
messages(id BIGSERIAL, conversation_id FK, role, content, created_at)  -- idx (conversation_id, created_at)
app_config(key PK, value, updated_at)            -- active_provider, whatsapp:*, knowledge_version
provider_keys(provider PK, encrypted_key)        -- Fernet blobs
knowledge_overrides(kind PK, content)
products(retailer_id PK, name, description, price_paise, currency, availability,
         image_url, category, variant_label, bestseller, external_refs JSONB, is_active, …)
orders(order_key PK, wa_id, conversation_id, recomputed_total_paise, raw_client_total_paise,
       currency, status, has_unmapped, created_at, deleted_at)
order_lines(id, order_key FK, retailer_id, qty, unit_price_paise, line_total_paise, mapped)
user_profiles(user_id PK, preferences JSONB)     -- scaffolded, unused
```

Session mapping: **`wa_id` is the `user_id`**; the webhook resolves the thread with `conv_repo.latest_conversation_id(event.wa_id)` before every engine call. `_resolve_conversation()` reuses the conversation only if `now - last_active_at <= 24h`, else creates a new one.

Memory windowing — `backend\app\core\memory.py`: load last 50 messages; if `len(history) > 15`, summarize the oldest 10 with a second LLM call (rolling `running_summary`) and keep the last 5 verbatim.

`persist_turn()` writes user msg + assistant msg + optional summary + `last_active_at` in **one transaction** (`app/store/postgres_impl.py`). Connection pool is lazy (`_LazyPool` in `app/store/pg_factory.py`) so no pool is created at import time on serverless cold start.

---

## 5. Order flow (end to end)

1. **Menu ingest:** `seeds/menu.csv` → `migrate_menu.py` builds `Product`s with deterministic slugs `bl-{category}-{item}`, `price_paise = price_inr * 100`. CSV is the declared single source of truth.
2. **Catalog sync:** `app/catalog/graph_batch_sync.py` pushes products to the Meta Product Catalog via `POST /{api_version}/{catalog_id}/items_batch` (50/chunk), and enables the native WhatsApp cart. CLI: `python -m app.catalog.bootstrap [--dry-run]`.
3. **Discovery:** customer texts → engine returns `intent` + `product_ids` → `_render_engine_result()`: menu → paginated interactive list; products → MPM cards; otherwise text.
4. **Deterministic taps bypass the LLM** — row ids the bot itself minted are dispatched by prefix *before* `engine.handle()` is called: a category tap costs zero tokens and cannot hallucinate.
5. **Cart & order:** customer uses WhatsApp's native cart; Meta delivers `type: "order"`. `capture_order()` **recomputes every price from our own product table** — client-sent price stored only as audit (`raw_client_total_paise`).
6. **Idempotency + ack:** `order_key = inbound message_id`; `INSERT … ON CONFLICT DO NOTHING RETURNING` so replays don't double-ack. Fixed multilingual ack (no LLM).
7. **Handoff:** `needs_handoff()` (keywords: refund, complaint, allergy, bulk order, "cancel my order"…) or unmapped items → WhatsApp alert to `owner_alert_number`. v1 stops at capture — no payment, no POS push.

---

## 6. Configuration / env vars

| Var | Purpose |
|---|---|
| `APP_MASTER_KEY` | Fernet key — encrypts provider keys + WA secrets; signs admin session cookie. Required. |
| `DATABASE_URL` | Postgres/Supabase DSN. Empty → in-memory repos. |
| `ADMIN_PASSWORD` | Admin panel login. |
| `CORS_ORIGINS` | JSON list; empty in prod = fail-closed. |
| `REQUEST_TIMEOUT_SECONDS` | Default 20; both LLM and Graph calls. |
| `APP_ENV` | `dev` allows wildcard CORS. |
| `WHATSAPP_CARDS_DELIVERY` | `body` \| `separate`. |
| `VERTEX_CREDENTIALS_JSON` / `VERTEX_PROJECT` / `VERTEX_LOCATION` | Vertex AI service account. |

Bootstrap-CLI-only: `CATALOG_ID`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `GRAPH_API_VERSION` (default `v23.0`).

**Not env vars — stored Fernet-encrypted in DB via admin panel:** WhatsApp `access_token`, `app_secret`, `verify_token`, `phone_number_id`, `waba_id`, `api_version`, `owner_alert_number`, `catalog_id`; per-provider LLM API keys.

---

## 7. What to reuse for the Shopify bot

**Copy nearly as-is:**

| Asset | Path | Why |
|---|---|---|
| Webhook GET verify + HMAC signature check | `backend\app\channels\whatsapp_signature.py`, `whatsapp.py::verify` | Identical for any Meta app; constant-time compares already correct. |
| Typed inbound event extraction | `backend\app\channels\whatsapp_inbound.py` | Returns `None` for status callbacks/unknown types instead of throwing. Add `button_reply` handling for Confirm/Cancel buttons (already parsed). |
| Graph sender + `_post_message` | `backend\app\channels\whatsapp_sender.py` | Add `send_template()` and `send_interactive_buttons()` using the same helper. |
| Pure payload builders + Meta caps | `backend\app\catalog\message_builder.py` | The cap constants save a day of Meta-doc reading. |
| **Deterministic reply-id routing** | `whatsapp.py` (~lines 500–525), `backend\app\catalog\menu_list.py` | Mint ids like `order:confirm:{id}` / `order:cancel:{id}` and dispatch by prefix *before* the LLM. Zero-token, zero-hallucination for mutating actions. |
| Structured-JSON intent extraction + `parse_llm_selection` | `backend\app\core\engine.py` | Swap schema to `{"intent": "order_status"\|"confirm"\|"cancel"\|"change_address"\|"chat", "order_id": "...", "reply": "..."}`. Keep id-validation-against-real-data and the "never leak raw JSON" fallback. |
| Provider abstraction + registry | `backend\app\providers\*`, `backend\app\config\provider_registry.py` | Runtime model swap; error-kind mapping + single retry. |
| Conversation state + windowed memory | `backend\app\store\*`, `backend\app\core\memory.py` | `wa_id → latest_conversation_id → 24h continuation` maps 1:1. |
| Fernet secret vault + admin panel | `backend\app\config\crypto.py`, `backend\app\admin\router.py` | Credential entry without redeploys. |
| Vercel serverless setup | `backend\vercel.json`, `backend\api\index.py`, `backend\app\store\pg_factory.py` | Lazy pool + `disable_aiohttp_transport` are hard-won cold-start fixes. |
| Idempotency pattern | `order_parser.py` + `PostgresOrderRepo.create_if_absent` | `order_key = message_id` + `ON CONFLICT DO NOTHING` — directly applicable to Shopify webhook dedup (`X-Shopify-Webhook-Id`). |
| Rate limiting / timing / sanitize | `app/ratelimit.py`, `app/request_timing.py`, `app/core/sanitize.py` | slowapi limits on webhook (60/min) + admin login (5/min); `strip_markdown` for WhatsApp. |

**What must be built new (not present in the cafe project):**
1. **Template messages** — mandatory for proactive pushes outside the 24h service window. Nothing in the repo does templates.
2. **Outbound-initiated flows** — the cafe bot is purely reactive. The Shopify bot needs Shopify-webhook → WhatsApp-push (`orders/create`) with Shopify's own signature scheme (`X-Shopify-Hmac-Sha256`, **base64**, not hex like Meta's).
3. **Identity binding** — mapping `wa_id` → Shopify customer/orders, plus an authorization check before exposing or mutating an order (only show orders belonging to that phone number).
4. **Write-side Shopify actions** — tagsAdd / orderCancel / orderUpdate as tool/action functions gated behind deterministic button taps, not LLM free-text.
5. **Shopify token manager** — client-credentials token cache with 24h refresh.

**Two design decisions worth inheriting deliberately:**
- **State integrity:** never trust client-sent or LLM-claimed values — always re-read order state from the Shopify Admin API before acting.
- **Two-tier routing:** deterministic prefix dispatch for anything that mutates state; LLM only for open-ended natural-language turns. That boundary is what makes the cafe codebase safe despite an LLM in the loop.
