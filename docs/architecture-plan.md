# Architecture Plan — Thetavas Shopify × WhatsApp Order Bot

> Created 2026-07-28. **v1.1** — owner-approved, then independently reviewed
> (`architecture-review-2026-07-28.md`, verdict **SOUND WITH FIXES**; all fixes additive,
> no restructure). Amendments below are ACCEPTED design; ADRs-001..005 written in Phase 1.
> The build follows this document level by level; nothing is coded before its level is
> CONFIRMED.
>
> **v1.1 amendments (from review — binding):**
> 1. **Durable outbox** (F1/F2): Shopify webhook handler = ONE short DB transaction
>    (dedupe + mapping upsert + `outbound_messages` queue row) → 200; sends happen from an
>    authenticated cron drain (+ optional self-invoke). Backfill/sweep ingest with
>    `eligible_for_push=False`; staleness guard ~6h; `dedupe_key` UNIQUE = one push per
>    order ever.
> 2. **Send-policy kill switch** (F3): `send_mode ∈ off|shadow|allowlist|live` +
>    `allowlist_phones`, enforced in the sender adapter only. Shadow mode = the parallel-run
>    strategy (and the ONLY strategy if WATI holds the same number).
> 3. **InboundButton** (F4): template taps arrive as `type:"button"` (NOT interactive) —
>    new typed variant; sender always attaches per-button payload `order:confirm:{gid}`.
> 4. **AuthorizedOrder** (F5/F8): mutations accept only ownership-checked orders;
>    message-id dedupe authority = DB unique constraint (LRU is fast path only) —
>    needs a one-line CLAUDE.md Rule 3 amendment (owner to approve).
> 5. **Two-phase cancel** (F9): no job polling; "cancellation requested" +
>    `bot-cancel-requested` tag → re-fetch confirms `cancelledAt` → final `cancelled` tag.
> 6. **Two-tier TokenManager** (F7): DB-persisted token + single-flight refresh +
>    12h refresh-ahead cron; in-process cache as fast path.
> 7. **Cold-start hygiene** (F10): lazy litellm import; consider route-split so
>    `/webhook/shopify` imports only asyncpg+hmac; keep `disable_aiohttp_transport` + lazy pool.
> 8. **`app/jobs/`** (F11): single dispatcher `/internal/jobs/{name}` + `CRON_SECRET`;
>    jobs = outbox drain, token refresh, reconciliation sweep, subscription self-heal
>    (checks version drift too), webhook-table retention, resumable backfill (F20/F21).
> 9. **Cancel double-check via buttons** (F12): `order:cancel:confirm/abort:{gid}` reply
>    buttons, not free-text YES (literal-YES fallback gated on `pending_actions` TTL row).
> 10. **Config-driven client decisions** (F14/F15): `push_policy`, `reveal_fields`,
>     template registry (language rule: customerLocale → learned pref → default), `tags`
>     as LISTS per action (Q13 dual-write = config edit).
> 11. **Failure-mode matrix + `app/obs/`** (F13): DB down → fail-closed 5xx / no mutation /
>     reveal nothing; Shopify down → ingest OK, REFUSE mutations; Meta down → outbox
>     retries; LLM down → deterministic paths unaffected.
> 12. **Cheap hedges** (F17/F18/F19/F22/F23): `pending_actions` now (unlocks v2 address
>     change); wider `order_mappings` status enum + both order-number forms + timestamptz;
>     `processed_webhooks` PK (webhook_id, topic) + retention; nullable `store_id`
>     default 'thetavas' + `tenant:` config prefix; `conversations.paused_until`.
> 13. **Launch checklist addition** (F16): confirm WABA messaging tier + Meta business
>     verification before cutover (unverified = 250 conversations/24h cap).
>
> **Status legend:**
> - ✅ **CONFIRMED** — verified against official docs, proven in the cafe project, or client-answered.
> - 🟡 **PROPOSED** — our recommended design; needs owner ("you") sign-off, no external blocker.
> - 🔴 **UNCONFIRMED** — blocked on a client answer or an external verification we cannot do alone.

---

## Level 0 — System context (who talks to whom)

```
Customer (WhatsApp, hi/en/hinglish/gu)
        ⇅
Meta WhatsApp Cloud API  (client's verified number, WABA)
        ⇅  webhooks / Graph sends
┌──────────────────────────────────────────────┐
│  OUR BACKEND (FastAPI, Vercel serverless)    │
│  Supabase Postgres · Gemini via LiteLLM      │
└──────────────────────────────────────────────┘
        ⇅  Admin GraphQL API / orders-create webhook
Shopify store: thetavas.myshopify.com
        ↑
   Shopflo one-click checkout  (creates the orders; upstream of us)
   a2ship shipping             (downstream; OUT OF SCOPE v1)
```

| Element | Status | Basis |
|---|---|---|
| Meta Cloud API **direct** (no BSP) | ✅ CONFIRMED | Client answer 2026-07-28; same as cafe project. |
| Client's verified WhatsApp number | 🔴 UNCONFIRMED | Purchased, but must verify it's registered on **Cloud API** (not the WhatsApp Business phone app) + we get Meta Business Manager access. Client-decisions Q12. |
| Our backend on Vercel + Supabase | ✅ CONFIRMED | Client answer (12/13); stack proven in cafe project. |
| Gemini via LiteLLM | ✅ CONFIRMED | Client direction + proven in cafe prod (vertex gemini-3.5-flash). |
| Shopify Admin GraphQL access | ✅ CONFIRMED | Client-credentials grant verified against shopify.dev; token flow already working (client's own curls). |
| Shopflo as order producer | ✅ CONFIRMED it exists (theme scan) / 🔴 its order-JSON shape unverified — must inspect one real order. |
| a2ship | ⏸ OUT OF SCOPE v1 | Client answer (9): "no idea", held. |

---

## Level 1 — Integrations (how each connection works)

### 1.1 Shopify — authentication ✅ CONFIRMED (3 verifications pending, all ours to do)
- Client credentials grant: `POST /admin/oauth/access_token` → 24h token → `X-Shopify-Access-Token`.
- Design: a **TokenManager** module — caches token, refreshes when <1h left or on 401. Never fetch-per-request.
- Phase 0 verifications — **✅ CLOSED 2026-07-28 by live API test** (`docs/phase0-verification-results.md`):
  app-org ownership proven by successful grant · protected data ON at order level
  (email/phone/addresses returned, E.164 +91) · 2026-07 verified end-to-end (2025-07 is dead —
  silently served as 2025-10) · ⚠ NEW GAP: **`read_customers` scope not granted** → add in Dev
  Dashboard + re-grant to enable the fallback path; primary flows unaffected.

### 1.2 Shopify — operations ✅ CONFIRMED (all verified against shopify.dev)
| Operation | API | v1 use |
|---|---|---|
| Find order by number | `orders(query:"name:...")` | Q&A flow |
| Find orders by phone | ✗ not supported → local mapping (primary) or `customers(phone)` → `orders(customer_id:)` (fallback) | Q&A flow |
| Confirm | `tagsAdd ["confirmed"]` | button tap |
| Cancel | `orderCancel` (async job; read `orderCancelUserErrors`; irreversible) + `tagsAdd ["cancelled"]` | button tap |
| Address update | `orderUpdate` | ⏸ NOT v1 (client answer 7) |

### 1.3 Shopify — webhook ✅ CONFIRMED mechanism / 🔴 payload shape
- `orders/create` via GraphQL `webhookSubscriptionCreate` (works with our token).
- Verify `X-Shopify-Hmac-Sha256` = **base64** HMAC-SHA256(raw body, client secret) — note: Meta uses hex; two distinct verifiers.
- Ack 2xx **< 5s** → persist + return 200 first, process after. Idempotency on `X-Shopify-Webhook-Id`.
- Monitor: after 19 consecutive failures Shopify **deletes** the subscription → daily self-check re-subscribes.
- ✅ RESOLVED 2026-07-28 — real Shopflo order inspected (`docs/phase0-verification-results.md`):
  `sourceName "Created by Shopflo"`; **COD detection** = `paymentGatewayNames` contains
  "Cash on Delivery"; **phone** = `order.phone` → `shippingAddress.phone` → `billingAddress.phone`
  (already E.164 +91); Shopflo stamps tags `[COD, COD pending, HIGH_RISK, Shopflo]` (its own
  COD-confirmation flow — likely THE current tool); order names have no `#` prefix.

**Webhook topic strategy (create vs update) 🟡 PROPOSED:**
- v1 subscribes to **`orders/create` ONLY**. Our DB is a lookup index (phone→order GID) +
  our own flow state — never a mirror of Shopify's order state.
- **Updates on Shopify are handled by re-fetch-on-demand, not callbacks** (rule 5.4:
  always re-fetch before acting/answering). Covers staff-cancels, fulfillment, tag edits —
  including the race "staff cancels after template sent, customer then taps Confirm/Cancel"
  (re-fetch sees `cancelledAt` → bot replies "already cancelled", no double action).
- `orders/updated` is deliberately NOT subscribed: too noisy, and **our own tagsAdd fires
  it** → would need echo-loop filtering for zero correctness gain.
- Receiver is **topic-agnostic** (single endpoint, dispatch on `X-Shopify-Topic`,
  idempotency keyed `(webhook_id, topic)`) so future topics are additive one-handler
  plug-ins: `orders/cancelled` (proactive "store cancelled your order" push) and
  `orders/fulfilled` ("your order shipped" — where a2ship/tracking attaches). Whether to
  send those proactive paid template messages = future client decision.

### 1.4 WhatsApp — messaging ✅ CONFIRMED rules / 🔴 account setup
- Business-initiated push (order created) → **UTILITY template mandatory**, one per language.
  ~₹0.12–0.13/delivered msg (India per-message pricing). Marketing miscategorization = ~₹0.88 → template copy must be transactional.
- Template has QUICK_REPLY buttons **Confirm / Cancel** → taps return as button payloads.
- Customer reply/tap opens **24h service window** → all follow-ups free-form + free (Gemini talks here).
- Send/receive/HMAC/inbound-parsing modules: ✅ copy from cafe project. `send_template()`: 🟡 new, trivial extension of the same `_post_message` helper.
- 🔴 UNCONFIRMED: WABA created, number on Cloud API, display name approved, templates approved, languages at launch (client-decisions Q3, Q12).

### 1.5 LLM — Gemini via LiteLLM ✅ CONFIRMED
- Same engine pattern as cafe: single completion returning strict JSON
  `{"analysis","intent":"order_status"|"confirm_request"|"cancel_request"|"chat","order_number","reply"}`,
  hardened parser (`parse_llm_selection` pattern: fence-strip → JSON parse → outermost-{} fallback →
  never leak raw JSON), reply in customer's language (hi/en/hinglish/gu).
- **Hard rule (inherited): the LLM never triggers a mutation.** Free-text "cancel my order" →
  LLM classifies intent → we re-fetch the order from Shopify → we send a **button** message →
  only the deterministic button tap mutates.

---

## Level 2 — Flows

### Flow A — Order push (replaces the 3rd-party tool) — architecture ✅ / 2 decisions 🔴
```
Shopify orders/create ──▶ verify HMAC ──▶ 200 OK (<5s)
  ──▶ idempotency check (webhook id)
  ──▶ extract: order GID, name, phone (E.164 normalize), customer name, COD?
  ──▶ store mapping phone→order in DB
  ──▶ send UTILITY template (customer language): "Hi {name}, order {number} received.
       [Confirm] [Cancel]"
```
🔴 D-1: which orders get it — COD-only (rec) vs all (client-decisions Q1).
🔴 D-3: launch languages (client-decisions Q3).

### Flow B — Button taps (deterministic, no LLM) — ✅ PROPOSED-approved pattern / 1 decision 🔴
```
tap Confirm ──▶ prefix router ──▶ re-fetch order from Shopify (still cancellable? not already tagged?)
            ──▶ tagsAdd "confirmed" ──▶ "Order confirmed ✓"
tap Cancel  ──▶ ask "Are you sure? Reply YES to cancel order {number}" (rec, Q4)
   YES      ──▶ orderCancel(restock:true, reason:CUSTOMER) ──▶ poll/accept job ──▶ tagsAdd "cancelled"
            ──▶ "Order cancelled" | on userErrors → human-handoff message
```
Button ids minted by us: `order:confirm:{gid}` / `order:cancel:{gid}` — cafe's tap-router pattern.
🔴 D-4: double-check before cancel (rec A) vs instant cancel (client-decisions Q4).

### Flow C — Free-text Q&A — architecture ✅ / reveal-scope 🔴
```
customer text ──▶ Meta webhook ──▶ (inside 24h window, free-form replies)
  ──▶ resolve orders: phone→mapping | customers-by-phone fallback | ask order number
  ──▶ ownership check: order's phone/customer MUST match sender (else refuse + support contact)
  ──▶ Gemini: intent + reply (reveals ONLY order id, email, status)
```
🔴 D-2: client said "id and email" — status reveal pending confirm (client-decisions Q5);
cannot answer "where is my order" without it.

**Phone→order resolution — ✅ CONFIRMED as primary architecture (owner, 2026-07-28):**
1. **DB mapping** (Supabase `order_mappings`), fed three ways: `orders/create` webhook (live),
   **one-time backfill** of last ~90 days via paginated `orders` query (works with current
   scopes — verified), and a **daily reconciliation sweep** (Vercel cron) that upserts recent
   orders — self-heals missed webhooks + doubles as subscription health check.
2. No/ambiguous match → ask order number → `orders(name:...)` → ownership check vs
   order-level phone (works without `read_customers` — verified live).
3. *Optional after scope fix:* customers-by-phone → orders-by-customer_id (largely redundant
   once 1's backfill+sweep exist).
Multiple orders per phone: lookup returns a list; bot asks which order, never guesses.
`read_customers` root-cause lead: granted list shows `customer_read_customers` (Customer
Account API scope) — the toggle was likely set in the wrong API section; fix = Admin API
scope + release new app version + reinstall + fresh token.

### Flow D — Handoff ⏸ deferred (client: yes, details later). Cafe D6 pattern ready to reuse.

---

## Level 3 — Backend module architecture 🟡 PROPOSED (mirrors cafe layout)

```
backend/
  api/index.py             # Vercel entrypoint                     [copy]
  app/main.py, deps.py     # FastAPI app + DI composition root     [copy/adapt]
  app/channels/
    whatsapp.py            # Meta webhook (GET verify + POST)      [copy + new button routes]
    whatsapp_inbound.py    # typed event parsing                   [copy + button_reply]
    whatsapp_sender.py     # send_text/_post_message               [copy + send_template() NEW]
    shopify_webhook.py     # orders/create receiver                [NEW]
  app/shopify/
    token_manager.py       # client-credentials cache/refresh      [NEW]
    client.py              # GraphQL: orders, tagsAdd, orderCancel [NEW]
    subscriptions.py       # webhookSubscriptionCreate + self-heal [NEW]
  app/core/
    engine.py              # Gemini JSON-intent (order schema)     [adapt]
    memory.py, sanitize.py # windowed memory, markdown strip       [copy]
    order_resolver.py      # phone→orders chain + ownership check  [NEW]
  app/knowledge/           # ADDED 2026-07-28 (gap found by owner) [adapt from cafe]
    loader.py, assembler.py, cache.py   # seeds → system context, version-cached
    seeds/brand_voice.md   # Thetavas tone: transactional, polite, multilingual
    seeds/faq.json         # shipping times, returns/exchange, COD rules, support contact
    seeds/business.json    # store name/hours/contact/policies  ← CONTENT FROM CLIENT (Q14)
    seeds/patterns.json    # few-shot examples for common order questions
    # NOTE: no menu.csv — we do NOT sell in chat (out of v1 scope)
  app/channels/copy.py     # ADDED 2026-07-28 [NEW] deterministic reply strings
    # confirm-success, cancel-confirm prompt, cancelled, not-found, refusal, error
    # fallbacks — 4 languages (en/hi/hinglish/gu). NEVER LLM-generated: these follow
    # button taps and mutations, so they must be fixed and reviewable (cafe CatalogCopy pattern).
  app/providers/           # LiteLLM adapter + registry            [copy]
  app/config/              # Settings + Fernet vault               [copy]
  app/store/               # repos: postgres + in-memory           [adapt schema]
  app/admin/               # panel: creds entry, mappings view     [copy, trim]
```

## Level 4 — Data model ✅ APPROVED as amended (v1.1, review F6/F17/F18/F19/F22)

```sql
order_mappings(order_gid PK, order_name, order_number_int, phone_e164 idx,
               customer_name, email, language,
               financial_status_at_create, is_cod,   -- creation-time SNAPSHOT, never authoritative
               status,        -- pending|template_queued|template_sent|send_failed|undeliverable|
                              -- awaiting_cancel_confirm|confirmed|cancel_requested|cancelled
               store_id DEFAULT 'thetavas',
               template_sent_at, responded_at, created_at, updated_at)   -- timestamptz

outbound_messages(id, dedupe_key UNIQUE,             -- e.g. 'order_created:{gid}' = 1 push/order EVER
               state,          -- queued|sent|suppressed|failed|undeliverable
               kind, phone_e164, payload_json, template_wamid, delivery_status,
               attempts, last_error_code, created_at, updated_at)

pending_actions(id, wa_id, order_gid, action, expires_at, created_at)  -- cancel confirm now; v2 address change

order_actions(id, order_gid, action, actor_wa_id, source_wamid, result,
              user_errors_json, created_at)          -- audit trail for irreversible mutations

processed_webhooks(webhook_id, topic, received_at, PRIMARY KEY(webhook_id, topic))  -- +retention >30d
processed_messages(message_id PK, received_at)       -- Meta dedupe AUTHORITY (LRU = fast path)
conversations(+ paused_until) / messages / app_config / provider_keys   -- as cafe schema
-- shopify token persisted Fernet-encrypted in app_config (+ expires_at) per ADR-003
```
Secrets (Shopify client id/secret, Meta token, app secret, verify token) → Fernet in `app_config`, entered via admin panel — never in code/env except `APP_MASTER_KEY`, `DATABASE_URL`.

## Level 5 — Security & integrity rules ✅ (inherited, non-negotiable)
1. Two HMAC verifiers: Meta = hex, Shopify = base64; constant-time compares; raw body before parsing.
2. LLM never mutates; buttons mutate; cancel double-confirmed (pending Q4).
3. Ownership check before revealing anything (sender's number must match the order).
4. Always re-fetch order state from Shopify before acting — never trust message/LLM claims.
5. Idempotency both directions (Meta message id LRU; Shopify webhook id table).
6. DPDP note: we store phone+order mapping; retention/deletion policy = later client decision (cafe D-DPDP pattern).

---

## Level 6 — Step-by-step implementation plan (each phase gated)

> **RE-SEQUENCED 2026-07-28 (owner correction).** The inbound conversation (customer messages →
> LLM answers) is the CORE feature, not a late add-on. Full flow design:
> `docs/inbound-conversation-design.md`. New order: 3 = WhatsApp channel (the pipe) ·
> **4 = THE CONVERSATION** (providers + knowledge + engine + resolver + memory) ·
> 5 = order-push/Confirm-Cancel automation (outbox drain + button mutations) · 6 = cutover.
> The table below keeps the original numbering for the already-completed phases 0–2.

| Phase | Deliverable | Gate to enter |
|---|---|---|
| **0. Verification** (no code) | ✅/❌ on: number on Cloud API + BM access · app org ownership · protected-data toggle · API 2026-07 re-test · **one real order JSON inspected (Shopflo shape)** · current-tool identity (Shopflo?) | Client answers Q7/Q12 + store/BM access |
| **1. Skeleton + Shopify client** | FastAPI skeleton, TokenManager, GraphQL client w/ 5 ops, tests | Phase 0 items 2–4 green |
| **2. Webhook + DB** | orders/create receiver (HMAC, idempotent, <5s ack), order_mappings, subscription self-heal | Phase 1; order-JSON shape known |
| **3. WhatsApp channel** | Copied cafe modules + `send_template()` + button routing; test number E2E | WABA + test template approved |
| **4. Flows A+B live-testable** | order → template → confirm/cancel round-trip on test number | Phases 2+3; decisions D-1/D-3/D-4 answered |
| **5. Flow C (Gemini Q&A)** | order-intent engine + resolver + ownership check, 4 languages | Phase 4; reveal-scope (Q5) answered |
| **6. Parallel run + cutover** | run alongside current tool on real orders → client approval → switch | Client switchover choice (Q8) |

**The design itself needs your sign-off on Levels 3–4 (🟡)** — then Phase 0 can start
immediately; it is pure verification and needs no client-side build work.
