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

### Amendment 2026-08-15 — INLINE send is now the PRIMARY trigger (owner-directed)

**Status:** ACCEPTED (owner-directed reversal, live production code). The original decision
above (queue → external scheduler → atomic-claim → send) is NOT erased — it remains the design
of record for the drain machinery, and the drain endpoint is kept as a backstop. This amendment
records that the PRIMARY send trigger has moved back into the webhook request, inline.

**Context (what changed).** The external scheduler the owner relied on to hit
`/internal/jobs/outbox_drain` (cron-job.org) stopped working, so queued confirmations were not
being sent at all. The owner was presented with the tradeoffs — explicitly including that an
earlier same-day attempt to "send inline" via FastAPI `BackgroundTasks` was rejected because
BackgroundTasks do not reliably run after the response on Vercel's buffered Python runtime — and
chose to proceed with a **genuine inline `await`** rather than continue depending on an external
cron service for the primary order-confirmation send.

**Decision.** After its existing fast DB writes (`ingest_order_created` + best-effort mirror),
the `orders/create` handler `await`s the send **inline**, before constructing the 200 response
(`jobs.outbox_drain.send_inline_outbound`). This is NOT `BackgroundTasks`/`create_task`/any
fire-and-hope mechanism — it is a real awaited call, so Shopify's ack IS legitimately delayed by
the send. That delay is **bounded** by a short, path-specific timeout on the WhatsApp call
(`_INLINE_SEND_TIMEOUT_SECONDS = 3.0`, distinct from the drain's 20s default) so the whole
handler — DB writes + the bounded send + overhead — stays safely under Shopify's <5s ack budget
even against a slow/unresponsive Meta endpoint. This is the key difference from the reverted
BackgroundTasks attempt: predictable, bounded delay, not silent unreliability.

**Safety invariants (unchanged bar).** (1) The inline path operates ONLY on the single row it
just created: `ingest_order_created` now returns `IngestResult.outbound_id`, and
`claim_outbound_by_id` atomically flips **that** row `queued → processing` (`FOR UPDATE SKIP
LOCKED`), so a still-available backstop drain can never also claim and double-send it. It never
touches the generic `claim_queued_outbound`/drain path. (2) A retryable failure/timeout bumps the
row back to `queued`, and a killed invocation leaves it `processing` for the drain's 10-minute
stale-reclaim — so the durable outbox is still the source of truth and delivery is still
retryable. (3) The `send_mode` kill switch is enforced exactly as elsewhere (`off` leaves the row
queued and sends nothing; shadow/allowlist-miss suppress with zero Meta calls). (4) The inline
send NEVER raises past the webhook boundary — a send failure/timeout can never turn the 200 ack
into a 5xx.

**Consequences.** Push latency drops from ~1 minute to "within the confirming webhook request."
Delivery no longer depends on any external scheduler. The `/internal/jobs/outbox_drain` endpoint
and its 1-minute-cron shape are RETAINED as a manual/backstop tool (for retrying rows the inline
send could not complete within its short budget, or if the owner re-adds a scheduler later) — it
is simply no longer the primary trigger. `reminders.py`/`send_reminders` is unaffected and still
needs external periodic triggering. Tradeoff explicitly accepted by the owner: a bounded ack
delay (≤ the 3s send timeout + DB/overhead) in exchange for not depending on an external cron for
the primary send.

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
