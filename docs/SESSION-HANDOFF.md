# SESSION HANDOFF — Thetavas Shopify × WhatsApp Order Bot

> **New Claude session: read this file first, then `CLAUDE.md`, then `docs/FR/_pipeline_status.md`.**
> Written 2026-07-28. Repo state at commit `41c782d` (33 commits) · suite **125 passed, 2 skipped**.
> Project root: `e:\bhvaesh_automation` · GitHub: `github.com/Devpanchal37/thetavas-order-bot` (private, pushed & current).

---

## 1. What we are building (one paragraph)

A WhatsApp bot for **thetavas.myshopify.com**, a Shopify kurti store (client uses **a2ship** for
shipping, **Shopflo** one-click checkout). Two flows:

- **THE CORE FEATURE — inbound conversation:** a customer WhatsApps the store ("where is my
  order?", "cancel my order"), we identify them by phone, fetch live order data from Shopify, and
  answer with **Gemini** in their language (hi/en/hinglish/gu).
- **Outbound push:** a new Shopify order triggers a WhatsApp **template** with **Confirm / Cancel**
  buttons; Confirm adds a tag, Cancel cancels the order. This replicates what the client's current
  **WATI** setup does, which we will replace.

**Owner correction (important):** the inbound conversation is the main point of the project. An
earlier plan scheduled it last; phases were re-sequenced on 2026-07-28 so it ships next.

## 2. Reference project — READ THIS, we copy from it

`D:\ai_whatsapp_agent` — a working cafe-order WhatsApp bot (FastAPI + Meta Cloud API + LiteLLM/
Gemini + Supabase, on Vercel). Most of our conversation layer is `[copy]`/`[adapt]` from it.
Summary already written: **`docs/reference-project-ai-whatsapp-agent.md`** (read that instead of
re-exploring the folder). Its `docs/FR/` convention is where our pipeline-status format comes from.

## 3. Which files to read, in order

| Priority | File | Why |
|---|---|---|
| **1** | `CLAUDE.md` | project rules — agent routing, no app code in main chat, never push without approval, secrets rules |
| **2** | `docs/FR/_pipeline_status.md` | **Tier-1 status board** — every phase, every review, every open item |
| **3** | `docs/inbound-conversation-design.md` | the CORE feature, designed end-to-end (10-step flow + module map) |
| **4** | `docs/architecture-plan.md` | Levels 0–6: system context, integrations, flows, modules, data model, security rules, phase gates |
| **5** | `docs/architecture-decisions.md` | ADRs 001–005 — BINDING (outbox, kill switch, token, mutation safety, config-driven decisions) |
| 6 | `docs/memory/error_learnings.md` | Tier-1 — read before any work; ~9 hard-won lessons |
| 7 | `docs/FR/client-decisions-all.md` | all open client questions, copy-paste ready |
| 8 | `docs/phase0-verification-results.md` | live Shopify API test results + the real order shape |
| 9 | `docs/security-review-2026-07-28-phase{1,2}.md` | what was found/fixed + **deferred items list** |
| 10 | `docs/current-wati-bridge-analysis.md` | what the system we're replacing actually does |
| 11 | `docs/architecture-review-2026-07-28.md` | independent review, findings F1–F24 |
| — | `docs/superpowers/plans/*.md` | task-level plans for Phases 1 & 2 (executed) |
| — | `docs/memory/{component_registry,api_registry}.md` | Tier-2 — grep before creating anything |

## 4. What is COMPLETE (built, reviewed, secured, pushed)

### Phase 1 — Shopify layer ✅
`backend/app/` — `config/` (Settings, Fernet `SecretVault`, `ConfigService`), `store/` (ConfigRepo
Protocol + in-memory), `shopify/` (models + error taxonomy, **TokenManager** per ADR-003:
DB-persisted 24h token, refresh margin, single-flight; **ShopifyClient**: order fetch, order search
by name, customer-orders-by-phone, `tagsAdd`, `orderCancel`), `deps.py` composition root,
`main.py` `/health`, Vercel entrypoint (region **bom1**/Mumbai), `scripts/smoke_shopify.py`.
**Live-verified against the real store.**

### Phase 2 — Order ingestion ✅
`app/core/phone.py` (E.164), `app/channels/` (`shopify_signature.py` = base64 HMAC,
`shopify_orders.py` = payload parse + language + eligibility, `shopify_webhook.py` =
`POST /webhooks/shopify`), `app/shopify/subscriptions.py` (self-heal incl. **API-version drift**),
`app/jobs/router.py` (authenticated dispatcher, `CRON_SECRET`), `app/store/` (full Level 4
`schema.sql`, `pg_factory.py` lazy asyncpg pool with `statement_cache_size=0`, `postgres.py`
atomic `IngestStore`), `scripts/apply_schema.py`.

**Verified end-to-end by the orchestrator** with a real Shopflo-shaped signed payload: valid →
mapped + queued; replay → duplicate, no double-queue; bad signature → 403; **hex (Meta-scheme)
signature → 403**; phone normalized from shipping fallback; `hi-IN` → Hindi template selected.

**All 8 security attack PoCs re-verified as fixed** (non-ASCII headers, type-confused signed
payloads, deep JSON, 2MB body, foreign shop domain, 200k field, corrupt master key).

### External setup ✅
- **Shopify:** client-credentials grant working on API **2026-07** (2025-07 is dead). Scopes
  include `read_orders`/`write_orders`; ❌ **`read_customers` NOT granted** (fallback lookup
  blocked — likely toggled under Customer Account API instead of Admin API; needs re-release +
  reinstall + fresh token).
- **Meta:** App `771818472295971` ("Demo") · WABA `2454816495000045` · Phone Number ID
  `1298805403309058` · test number **+1 555-651-8147** (GREEN) · permanent system-user token
  working (assets had to be assigned to the "Conversions API System User").
- **Templates `order_confirmation_cod` — APPROVED in en, hi, gu** (UTILITY category ≈ ₹0.12/msg).
  Draft/spec: `docs/whatsapp-templates.md`. A test template message was delivered to the owner's
  phone (+91 7575072795, a verified test recipient).

## 5. What is PENDING

### Not yet built (in the re-sequenced order)
| Phase | Deliverable | Task-level plan? |
|---|---|---|
| **3** | **WhatsApp channel** — Meta webhook (GET verify + POST receive, **HEX** HMAC), typed inbound parser incl. **`InboundButton`** (template taps arrive as `type:"button"`, NOT `interactive`), sender (`send_text`, **new** `send_template`, `send_buttons`), `channels/copy.py` fixed strings ×4 languages | ❌ not written |
| **4** | **THE CONVERSATION** — `providers/` (LiteLLM/Gemini) `[copy]`, `knowledge/` (loader+assembler+cache+seeds) `[adapt]`, `core/engine.py` strict-JSON intent `[adapt]`, `core/order_resolver.py` identity+ownership `[NEW]`, `core/memory.py` `[copy]` | ❌ not written |
| **5** | Order push automation — outbox **drain**, template send, button-tap mutations, two-phase cancel | ❌ not written |
| **6** | Parallel run (shadow mode) → cutover from WATI | ❌ not written |

**Planning status:** architecture is 100% planned for all phases; **task-level implementation
plans exist only for Phases 1–2**. Owner asked about this on 2026-07-28; the open recommendation
was: full task-level plan for Phase 3 now + feature-level specs for 4–6 (code-level plans go stale
if written too far ahead — Phase 1's plan needed reconciliation twice). **Owner has not chosen yet.**

### Blockers / needed from the owner
1. **Supabase project + connection string** — THE blocker. Everything runs in-memory today.
   Needed for: `python -m scripts.apply_schema`, the 2 skipped Postgres tests, and any deploy.
   Recommend Mumbai region. Use `statement_cache_size=0` (already set) for the pooler.
2. **⚠ Secrets do NOT survive a new session** — they lived in the session scratchpad, never in the
   repo. Re-provide as needed: Shopify client id/secret, Meta app secret + system-user token.
   (Shopify client secret and the Meta token were shared in plaintext in chat → **rotation
   recommended** once the Fernet vault is wired in production.)
3. **Client answers** (`docs/FR/client-decisions-all.md`): Q1 which orders get the push (default
   `cod_only`), Q5 may the bot state order **status** (blocks the Q&A being useful), Q7 does WATI
   use the **same number** we registered (migration risk), Q13 tag-name compatibility for
   a2ship/ops filters, **Q14 the FAQ/policy CONTENT** for the knowledge base (delivery times,
   returns, COD rules, damaged goods, support contact, tone).
4. **Vercel** — deliberately NOT connected yet (owner's choice: connect after the code is
   complete). Once connected, every push to `main` is a production deploy → approval required.

### Deferred but tracked (do not re-discover)
In `docs/security-review-2026-07-28-phase2.md`: pre-auth secret caching + rate limiting on the
webhook, idempotency key derived from the signed body, `public_base_url` allowlist, `app_env`
prod default, PII duplication in `outbound_messages.payload_json`, structured redacting logging,
least-privilege DB role. Plus: `/health` response model, dependency lockfile, `.vercelignore`,
ruff `S` ruleset.

## 6. Non-negotiable rules (violating these breaks the system)

1. **The LLM never mutates.** Free-text "cancel my order" → LLM classifies *intent* → we re-fetch
   from Shopify → we send **buttons** → only the deterministic tap calls `tagsAdd`/`orderCancel`.
2. **Two different HMAC schemes.** Meta = **hex** (`X-Hub-Signature-256`); Shopify = **base64**
   (`X-Shopify-Hmac-Sha256`). Constant-time compare on the **raw body**. Compare **bytes** — 
   `hmac.compare_digest` raises `TypeError` on non-ASCII strings from headers.
3. **Ownership check before revealing OR mutating.** `AuthorizedOrder` validates at construction
   that the verified phone matches the order (ADR-004). Mutating client methods accept only it.
4. **Always re-fetch live order state** before answering or acting — never trust our snapshot,
   the message, or the LLM.
5. **Never crash on a signed webhook.** A 500 burns Shopify's 19-consecutive-failure budget and
   **deletes the subscription**. Coerce every payload field type.
6. **Serverless has no "run after the response"** — use the durable outbox (ADR-001).
7. **Secrets:** only `APP_MASTER_KEY` + `DATABASE_URL` in env; everything else Fernet-encrypted in
   `app_config`. Never log/echo/commit.
8. **Never `git push` without explicit owner approval.**
9. **Main Claude does not write app code** — route to the `developer` agent (see
   `.claude/rules/common/agents.md`). Every phase: build → `code-reviewer` → `security-reviewer`
   (sensitive surfaces) → fix → verify.

## 7. Working conventions that have paid off

- **Independent verification:** after every agent report, re-run the critical claim yourself
  (the orchestrator re-ran all 8 security PoCs and the webhook E2E — both caught real detail).
- **Reviews find real bugs.** Phase 1: a forged `AuthorizedOrder` passed `cancel_order`. Phase 2:
  three crash paths on a public endpoint + the Supabase pgbouncer incompatibility. Do not skip.
- **Ad-hoc scripts:** the cafe project is pip-installed **editable** and shadows `app.*` imports —
  run with `cwd=backend` AND `PYTHONPATH=e:/bhvaesh_automation/backend`.
- **Client decisions land as config, not code** (ADR-005), so answers are a config edit.
- Verified store facts: order names have **no `#`** (`tavas3733`); COD = `paymentGatewayNames`
  contains "Cash on Delivery" (or tag `cod`); phone chain = `order.phone` → `shipping_address` →
  `billing_address`, already `+91` E.164; Shopflo tags orders `[COD, COD pending, HIGH_RISK, Shopflo]`;
  WATI writes `Confirmed by wati` / `Cancel by wati`.
- **Do not delete** the two `pro.ad2ship.com` admin webhooks (shipping depends on them). The two
  `tavas-wati-webhook` rows are the old tool's off-switch at cutover.

## 8. Suggested next action for a new session

1. Read `CLAUDE.md` → `docs/FR/_pipeline_status.md` → `docs/inbound-conversation-design.md`.
2. Ask the owner: (a) Supabase DSN, (b) the planning-depth choice from §5, (c) client answers.
3. Then: write the Phase 3 task-level plan (`superpowers:writing-plans`), execute via the
   `developer` agent, review with `code-reviewer` + `security-reviewer`, and proceed to Phase 4 —
   the conversation, which is the point of the whole project.
