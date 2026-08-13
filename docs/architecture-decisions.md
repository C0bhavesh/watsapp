# Architecture Decision Records (ADR-001 … ADR-005)

> Written 2026-07-28 as the first Phase 1 task, per the accepted review
> (`architecture-review-2026-07-28.md`). These are BINDING for all implementation.
> Status of each: ACCEPTED (owner-approved architecture v1.1).

---

## ADR-001 — Durable outbox for the webhook→send seam

**Context.** Vercel serverless Python has no reliable "run after the response" primitive:
FastAPI BackgroundTasks execute inside the request cycle on buffered invocations, and any
post-response work can be killed on instance reclaim. Combined with webhook-id idempotency,
a send that is lost after ack is *never retried* — the customer silently gets nothing.
Shopify requires a 2xx ack in <5s and deletes the subscription after 19 consecutive failures.

**Decision.** The Shopify webhook handler performs exactly ONE short DB transaction and
returns:
`INSERT processed_webhooks (conflict → already handled, 200)` + `UPSERT order_mappings` +
`INSERT outbound_messages(state='queued', dedupe_key UNIQUE, e.g. 'order_created:{gid}')` →
200. Any transaction failure → 5xx, so Shopify's retry budget IS the queue. Actual sends
happen ONLY from an outbox **drain job** (authenticated), driven by a **1-minute Vercel Cron**
(`GET /internal/jobs/outbox_drain`). Each run **atomically claims** queued rows
(`queued → processing` in one statement, `FOR UPDATE SKIP LOCKED`, so two overlapping cron
ticks can never both claim the same row), sends, and transitions `processing → sent` /
`→ undeliverable` (Meta terminal codes 131026/131047/131049) / back to `queued` for a later
run on a transient error until `max_attempts` → `failed`.

The webhook does **not** send inline and does **not** register a FastAPI `BackgroundTask` to
send: on Vercel's Python runtime `BackgroundTasks` run inside the request cycle and can be
killed on instance reclaim, so a background-task send is neither low-latency nor durable — it
is exactly the "no reliable process-after-response" hazard this ADR exists to avoid. This is
NOT an endorsement of an in-request background send; the queue → cron → atomic-claim → send
shape is the only sanctioned path. (A one-off 2026-08-13 webhook self-invoke experiment was
reverted the same day for precisely this reason.)

**Consequences.** Sends are retryable, observable, and exactly-once per order by DB
constraint. Push latency is at most the ~1-minute cron cadence. No Meta/LLM call ever happens
inside the Shopify webhook request.

---

## ADR-002 — Send-policy chokepoint and kill switch

**Context.** Parallel run with the current WATI flow must not double-message customers;
production needs an instant stop control; testing needs a safe allowlist. If WATI turns out
to hold the same number, live parallel run is physically impossible.

**Decision.** A single config-driven policy in `app_config`, read per send and enforced in
ONE place (the WhatsApp sender adapter — never at call sites):
`send_mode ∈ {off | shadow | allowlist | live}` + `allowlist_phones[]`.
`shadow` executes the full pipeline but writes the exact would-be payload to
`outbound_messages` as `state='suppressed'` instead of calling Meta.

**Consequences.** Cutover, rollback, and emergencies are config edits (no deploy). The
shadow run produces a real correctness comparison against WATI on live orders. Default
mode in every new environment: `off`.

---

## ADR-003 — DB-persisted Shopify token with single-flight refresh

**Context.** Client-credentials tokens last 24h. In-process caching alone is unsound on
serverless (N instances × cold starts = token-grant stampede on the critical 5s webhook
path), and a token-endpoint outage at expiry would stop every flow simultaneously.

**Decision.** Two-tier TokenManager: in-process cache (fast path) over a DB-persisted
token (`app_config`: Fernet-encrypted token + `expires_at`). Refresh is single-flight
(Postgres advisory lock / compare-and-set), refreshed ahead by a ~12h job, with on-401
forced refresh as backstop. Never fetch-per-request.

**Consequences.** Cold starts read the DB instead of hitting OAuth; a token-endpoint
outage has a ~12h buffer; rotation of client credentials is a config operation.

---

## ADR-004 — Structurally-enforced mutation safety

**Context.** `orderCancel` is irreversible. Button payloads bind taps to orders but can be
replayed/misdirected in edge cases; Meta redelivers webhooks; multiple Vercel instances
defeat in-process LRU dedupe. CLAUDE.md Critical Rules 2–3 demand the LLM never mutates and
ownership is checked.

**Decision.** (1) `core/order_resolver.py` is the only source of `AuthorizedOrder` — a type
issued only after verifying the sender's phone matches the freshly re-fetched order.
All mutating methods in `shopify/client.py` accept ONLY `AuthorizedOrder` (compile-time /
review-time enforcement). (2) Message dedupe authority = DB unique constraints
(`processed_messages` for Meta, `processed_webhooks` for Shopify); in-process LRU is a fast
path only. (3) Cancel is two-phase: mutation accepted → "cancellation requested" +
provisional tag `bot-cancel-requested`; final `cancelled` tag only after a re-fetch shows
`cancelledAt`. (4) Cancel confirmation is a deterministic button
(`order:cancel:confirm/abort:{gid}`), never LLM-interpreted free text.

**Consequences.** Mutating an ownership-unchecked order is structurally impossible;
duplicate taps cannot double-mutate; ops/a2ship never see a false `cancelled` tag.
Pending: one-line CLAUDE.md Rule 3 amendment ("LRU fast path + DB uniqueness authority").

---

## ADR-005 — Client decisions land as runtime config, not code

**Context.** Q1 (which orders), Q3 (languages), Q5 (reveal fields), Q13 (tag names) are ON
HOLD or may change; the project rule is to build only what isn't blocked and never lock a
client decision in code.

**Decision.** Four `app_config` entries read at runtime:
- `push_policy: cod_only | all | all_prepaid_no_buttons` (default `cod_only`)
- `reveal_fields: [order_number, email]` (default; `status` added when Q5 confirms)
- `templates: {lang: {name, param_map}}` — registry; language rule:
  `order.customerLocale` → learned per-phone preference → `default_language` (en)
- `tags: {pending: [...], confirmed: [...], cancelled: [...]}` — LISTS per action, so
  Q13's dual-write (new names + WATI-era names during transition) and final cutover are
  config edits.

**Consequences.** Every client answer is a config change with no deploy. Defaults are the
recommended options, so the system is buildable and testable today.
