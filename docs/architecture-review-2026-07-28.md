# Independent Architecture Review — 2026-07-28 (architect agent)

> Full raw report. Verdict: **SOUND WITH FIXES** — no restructuring needed; all fixes are
> additive modules/tables/columns. Accepted amendments are folded into
> `architecture-plan.md` §"Review amendments v1.1". ADRs-001..005 to be written in Phase 1.

## Verdict

Layering is right and does not need rework. Ports & adapters boundaries, the
deterministic-mutation boundary, the topic-agnostic webhook receiver, re-fetch-before-act,
and dual HMAC verifiers are the load-bearing decisions and are all correct. **No finding
requires restructuring Level 3's module layout.** The plan was under-specified at exactly
the three points where money, irreversible mutations, and customer-visible messages live:

- Flow A's "return 200 first, process after" had no execution mechanism on serverless.
- Level 4 was missing every table that flow state needs (outbox/wamid/pending
  actions/audit).
- Phase 6 parallel run had no mechanism to prevent double-messaging real customers.

## P0 — must be resolved before Phase 1/2 code

**F1 [CRITICAL] No mechanism for "ack <5s, process after" on Vercel Python.** FastAPI
BackgroundTasks run inside the request cycle on buffered invocations; post-response work
dies on instance reclaim; webhook-id idempotency then permanently swallows the lost send.
Fix: **durable outbox** — handler does one short transaction (dedupe insert + mapping
upsert + `outbound_messages` row) → 200; failed transaction → 5xx so Shopify's 19×/48h
retry budget IS the queue. Drain via authenticated cron + optional fire-and-forget
self-call. → ADR-001.

**F2 [CRITICAL] Backfill/sweep can mass-send templates to old orders.** 90-day backfill +
reconciliation sweep share the ingestion path with sending. Fix: `ingest_order(order,
eligible_for_push=False)` for backfill/sweep; staleness guard (never push for orders older
than ~6h, config); `dedupe_key='order_created:{gid}'` UNIQUE = one push per order ever.

**F3 [CRITICAL] Parallel run double-messages customers; no kill switch.** Fix:
config-driven **send policy** read per send, enforced in the sender adapter (single
chokepoint): `send_mode ∈ {off | shadow | allowlist | live}` + `allowlist_phones[]`.
`shadow` records the exact would-be payload as `state='suppressed'` → genuine correctness
comparison vs WATI. If WATI uses the SAME number (Q7c), parallel run is physically
impossible → shadow mode is the fallback plan. → ADR-002.

**F4 [HIGH] Template quick-reply taps arrive as `type:"button"`, NOT
`interactive.button_reply`.** Cafe parser returns None → every Confirm/Cancel tap dropped
SILENTLY. Fix: `InboundButton` variant (payload, text, context.id) routed through the same
prefix router; template sender always attaches explicit per-button payload (≤128 chars —
`order:confirm:{gid}` ≈ 48). Logged in error_learnings.

**F5 [HIGH] Ownership check specified for reads only.** Mutating path must require
sender-phone ↔ order match too. Fix: `order_resolver` returns an `AuthorizedOrder` type;
`shopify/client.py` mutations accept ONLY that type (structurally impossible to mutate
unchecked). Level 5 rule 3 amended: "before revealing OR MUTATING anything." → ADR-004.

## P1 — fix before the phase that depends on it

**F6 [HIGH] Missing tables:** `outbound_messages` (outbox: state, attempts, template_wamid,
error_code, dedupe_key UNIQUE), wamid+delivery status tracking (Meta status callbacks;
detect 131026 "not a WhatsApp user"), `pending_actions` (multi-turn state: cancel
double-check now, v2 address-change later), `order_actions` (audit trail for irreversible
mutations — "your bot cancelled my order" must be answerable).

**F7 [HIGH] TokenManager in-process cache not serverless-safe.** Fix: two-tier — in-process
fast path over DB-persisted token (Fernet in `app_config` + expiry), single-flight refresh
(advisory lock/CAS), refresh-ahead cron ~12h, on-401 backstop. → ADR-003.

**F8 [HIGH] In-process LRU dedupe for Meta message ids unsound here** (multi-instance;
duplicate Confirm tap = duplicate mutation attempt). Fix: LRU stays fast path; authority =
DB unique constraint (`processed_messages` or `processed_webhooks` + source column).
**Requires owner-approved one-line amendment to CLAUDE.md Critical Rule 3.** → ADR-004.

**F9 [HIGH] orderCancel is async — don't tag "cancelled" before the job completes.** Fix:
no polling; on accepted mutation reply "cancellation requested" + provisional tag
`bot-cancel-requested`; confirm `cancelledAt` via re-fetch (next interaction or short-delay
outbox item) → then final `cancelled` tag. Two tags, two states.

**F10 [HIGH] litellm import weight on the Shopify webhook cold-start path** threatens the
5s ack (counts toward the 19-failure subscription deletion). Fix: lazy litellm import
behind the provider factory; keep cafe's `disable_aiohttp_transport` + `_LazyPool`;
consider vercel.json route-split so `/webhook/shopify` has a minimal import graph (needs
only asyncpg + hmac with the outbox).

**F11 [MED-HIGH] No `app/jobs/` module; cron endpoints unauthenticated by omission.** Fix:
`app/jobs/` + single dispatcher `/internal/jobs/{name}` guarded by `CRON_SECRET`. Verify
Vercel plan cron granularity (Hobby = daily only) before relying on minute-level drains.

**F12 [MED-HIGH] Free-text "YES" cancel confirmation is the weakest point of the
LLM-never-mutates boundary.** Fix: confirm via interactive reply buttons
(`order:cancel:confirm:{gid}` / `order:cancel:abort:{gid}`) inside the 24h window — free,
deterministic, language-independent; literal-YES allowlist only as secondary path gated on
a live `pending_actions` row with TTL. (Implements client Q4-A exactly — no re-ask needed.)

**F13 [MED-HIGH] No failure-mode matrix / observability.** Fix: Level 5.x table added
(DB down → 5xx fail-closed / no mutation / reveal nothing; Shopify down → ingest still
works, REFUSE to mutate; Meta down → outbox retains + retries; LLM down → deterministic
paths unaffected, Q&A fixed fallback). Plus `app/obs/`: structured logs keyed by
webhook_id/order_gid/hashed wa_id, counters, admin recent-activity view.

**F14 [MED-HIGH] ON-HOLD client decisions must land as config, not code:**
`push_policy` (Q1), `reveal_fields` (Q5), `templates` registry (Q3), `tags` — a LIST per
action, making Q13's dual-write a config value and cutover a config edit. → ADR-005.

**F15 [MED] Template registry + language selection rule missing.** Fix:
`whatsapp_templates.py` pure builders + config registry + selection rule:
`order.customerLocale` → learned per-phone preference → default. `language` column on
order_mappings.

**F16 [MED] WhatsApp messaging-tier limits absent.** Unverified-business WABA = 250
business-initiated conversations/24h (then 1K/10K/100K by quality tier). "Verified number"
≠ business verification. **Added to launch checklist: confirm tier + business verification
before cutover.** Outbox drain = natural throttle; map Meta errors (130429/131048
retryable; 131026 permanent → `undeliverable`).

## P2 — cheap hedges

**F17** `pending_actions` now = v2 address-change becomes purely additive (highest
forward-compat leverage). **F18** order_mappings: wider status enum + `updated_at`;
snapshot columns marked non-authoritative; store BOTH `order_name` and numeric
`order_number`; timestamptz. **F19** `processed_webhooks` PK = (webhook_id, topic);
retention job >30d. **F20** `webhookSubscriptionCreate` never actually exercised — smoke
it before Phase 2; pin apiVersion on the subscription; self-heal checks version drift; use
payload's `admin_graphql_api_id`. **F21** backfill = resumable job with persisted cursor
(or local CLI); backfill+sweep read `throttleStatus` and back off. **F22** nullable
`store_id` default 'thetavas' + `tenant:` config-key prefix ≈ free multi-tenant insurance
(only item that would otherwise need restructuring). **F23** `conversations.paused_until`
column now → human handoff later is additive. **F24** template approval = critical path;
submit in parallel with Phase 1 (in motion — drafts done).

## Confirmed well-designed (do not touch)

Ports & adapters w/ Protocol-only core imports; deterministic-mutation boundary; re-fetch-
before-act; NOT subscribing to orders/updated (self-echo); topic-agnostic receiver; dual
HMAC verifiers; reconciliation sweep concept; DB-mapping as primary resolution; Fernet
secret vault; config-pinned API version; phase-gated plan; WATI-convention compatibility;
parse_llm_selection hardening; config-only provider swap.

## Forward-compat absorption summary

Proactive topics ✅ (needs F14/F15) · Gemini swap ✅ · handoff ✅ (F23 column) · a2ship ✅
(`app/shipping/` + TrackingProvider Protocol; engine takes optional tracking block) ·
languages ✅ (F15) · v2 address-change ✅ only after F17 · second store ❌ until F22 column.

## ADRs to write before Phase 1 code

ADR-001 outbox · ADR-002 send-policy kill switch · ADR-003 DB-persisted token ·
ADR-004 AuthorizedOrder + DB idempotency · ADR-005 config-driven client decisions.
