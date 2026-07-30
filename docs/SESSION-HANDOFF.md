# SESSION HANDOFF — Thetavas Shopify × WhatsApp Order Bot

> **New Claude session: read this file first, then `CLAUDE.md`, then `docs/FR/_pipeline_status.md`.**
> Written 2026-07-29. Project root: `d:\bhvaesh_automation` · GitHub: `github.com/C0bhavesh/watsapp`
> (private — supersedes the earlier `Devpanchal37/thetavas-order-bot`, moved 2026-07-28).

---

## 1. What we are building (one paragraph)

A WhatsApp bot for **thetavas.myshopify.com**, a Shopify kurti store (client uses **a2ship** for
shipping, **Shopflo** one-click checkout). Two flows:

- **THE CORE FEATURE — inbound conversation:** a customer WhatsApps the store ("where is my
  order?", "cancel my order"), we identify them by phone, fetch live order data from Shopify, and
  answer with **Gemini** in their language (English / Hindi / Hinglish, Gujarati templates ready
  but dormant).
- **Outbound push:** every new Shopify order triggers a WhatsApp **template** with Confirm/Cancel
  buttons; Confirm adds a tag, Cancel asks for confirmation then cancels the order. This replaces
  the client's current **WATI** setup.

## 2. Reference project

`D:\ai_whatsapp_agent` — a working cafe-order WhatsApp bot (FastAPI + Meta Cloud API + LiteLLM/
Gemini + Supabase, on Vercel), used as the design/copy source. **Not present on this machine** —
work from `docs/reference-project-ai-whatsapp-agent.md` (already-written summary) rather than
re-exploring the folder.

## 3. What is COMPLETE (built, reviewed, secured, pushed to GitHub)

### Phase 1 — Shopify layer ✅ CLOSED
`Settings`/Fernet `SecretVault`/`ConfigService`, **TokenManager** (ADR-003: DB-persisted 24h
token, refresh margin), **ShopifyClient** (order fetch, order-by-name search, customer-orders-by-
phone, `tagsAdd`, `orderCancel`), `AuthorizedOrder` invariant (ADR-004, enforced at construction),
`/health`, Vercel entrypoint (region `bom1`). 54 tests, code-reviewed + security-reviewed, all
fixes landed. **Live-verified against the real store.**

### Phase 2 — Order ingestion ✅ CLOSED
E.164 phone util, Shopify **base64** HMAC verifier, `POST /webhooks/shopify` (atomic dedupe +
phone→order mapping + outbox queue in one transaction — ADR-001), subscription self-heal
(URL *and* API-version drift), authenticated jobs dispatcher (`CRON_SECRET`), full Postgres
schema + `apply_schema.py`. 125 tests, both reviews passed (3 HIGH security findings, all fixed),
orchestrator-verified end-to-end against a real Shopflo-shaped payload.

### Phase 3 — WhatsApp channel (the pipe) ✅ CLOSED, MERGED, **PUSHED**
Meta **hex** HMAC verifier, Fernet-split `whatsapp:*` config loader, typed inbound parser
(`InboundText`/`InboundInteractive`/**`InboundButton`** — template quick-reply taps arrive as
`type:"button"`, not `interactive`), `MessageStore` dedupe port (in-memory + Postgres), sender
(`send_text`/`send_template`/`send_buttons`), deterministic 4-language reply copy (`copy.py`),
`GET`/`POST /webhook/whatsapp` router (verifies, dedupes, echoes event type — does **not** yet
route anywhere). 218 tests passed, 3 skipped (no `TEST_DATABASE_URL`). Code review: 1 blocker +
1 major, fixed. Security review: 1 HIGH (tenant guard failing open on batched messages) + 3
MEDIUM + 2 LOW, all fixed and independently re-verified with fresh PoCs — **APPROVE**.

**This phase is fully merged into `main` and pushed to `github.com/C0bhavesh/watsapp`.**
(An earlier note in `_pipeline_status.md` said "not pushed" — that was true when written; the
repo move + push on 2026-07-28 superseded it, 17 commits including all of Phases 1–3.)

**What works right now, end to end:** a real Shopify order → verified → recorded → queued in the
outbox. A real WhatsApp message or button tap → verified → deduped → parsed into a typed event.
**Nothing replies yet, and nothing sends yet** — that logic (Phase 4/5) is not built.

### External setup ✅
- **Shopify:** client-credentials grant on API **2026-07**. `read_orders`/`write_orders` granted;
  `read_customers` still NOT granted (likely toggled under the wrong API section) — not currently
  blocking, DB-mapping is the primary phone→order resolution path.
- **Meta:** WABA `2454816495000045`, Phone Number ID `1298805403309058`, permanent system-user
  token working. Templates `order_confirmation_cod` **approved in en, hi, gu**. A live template
  message was successfully delivered to a real test number.

### Client decisions — round 2, answered this session (2026-07-29)
Recorded in `docs/FR/client-decisions-all.md` and `docs/FR/_pipeline_status.md`:

| # | Question | Answer | Consequence |
|---|---|---|---|
| Q1 | Which orders get the push | **All orders** (not COD-only) | Prepaid customers now see a Cancel button on an already-paid order — flagged as a risk to watch; reversible as a config edit (`push_eligibility`, ADR-005), not a rebuild |
| Q3 | Launch languages | **English + Hindi + Hinglish** | Meta has no "Hinglish" template code — templates ship en+hi only; Hinglish is served free-form once the customer replies (no approval needed). Gujarati stays approved, dormant, free to enable |
| Q4 | Cancel handling | **Double-check before cancelling** | Matches the design already built (`pending_actions`) |
| Q5 | May the bot reveal order status | **Yes** | Reveal set = order id + email + status. Items/amounts/tracking stay hidden |
| Q14 | FAQ/policy content | **Delegated to us**, client edits later via admin panel | We seed Thetavas-appropriate defaults; **this is what makes the admin panel a hard requirement, not a nice-to-have** |

**Still open:** Q2 (order volume, for cost estimate), Q6 (no-match fallback: support contact only
vs. also alert staff), Q7c (which WhatsApp number WATI uses today — overlap risk), **Q8
(switchover plan — explained to owner 2026-07-29, no choice made yet)**, Q9–Q11 (held: post-
shipping cancel, a2ship tracking, handoff number), Q12 (confirm number is on Cloud API not the
phone app), Q13 (tag-name compatibility with a2ship/ops filters).

### Separate, parallel workstream: Shopify theme visual refresh
Unrelated to the bot — a visual-only refresh of the "TAVAS Unpublished Draft" theme (Editorial
Serif direction, rounded buttons). Design spec and a 7-task implementation plan are written and
committed to `main`. **All 7 tasks are built** (webfonts, button/card hover states, hero frame
accent, spacing rhythm, header/footer polish) — but on worktree branch
`worktree-theme-visual-refresh`, **not yet merged into `main`, not pushed**. Needs a manual
`shopify theme dev` visual check before merging (no automated test framework applies to theme
work).

## 4. What is PENDING

### 4a. Admin panel (Phase 3.5) — brainstorming IN PROGRESS, not finished
**This is where we stopped.** Once Q14 delegated FAQ/policy content to client self-service, and
given the architecture already requires all credentials to be entered through an admin panel
(never env vars, per CLAUDE.md Critical Rule 1), it became clear the admin panel is a **hard
blocker for both the client's workflow and the production deploy** — yet it exists nowhere in the
phase plan (Phases 0–6 never build it). We started designing it as an inserted Phase 3.5.

**Decided so far (via brainstorming, not yet written to a spec doc):**
- Scope: login, credential entry (Shopify + Meta secrets), knowledge/FAQ/policy editor, order-
  mappings view, **and** operational controls — the send kill-switch (off/shadow/allowlist/live,
  ADR-002), push eligibility, tag names. (Owner chose to include operational controls — this
  makes the panel the actual control surface for the WATI cutover, not just a config form.)
- Access model: **one shared password, full access** (no owner/staff role split) — a deliberate
  v1 simplification; the design will keep the door open to add roles later without a restructure.
- Knowledge editor UX: **structured forms** (FAQ as add/edit/delete rows, labeled policy fields,
  a plain textarea for brand voice) — not raw JSON, so a malformed save is impossible rather than
  merely rejected.
- Password storage: **`ADMIN_PASSWORD` environment variable** — matches the cafe reference
  project, avoids a first-run bootstrap race. **This requires a one-line amendment to CLAUDE.md
  Critical Rule 1**, which currently states only `APP_MASTER_KEY` + `DATABASE_URL` may live in
  env vars. Not yet applied — needs your explicit OK since it's a Critical Rule.

**Not yet decided (design was interrupted before these were presented/approved):**
- How the panel is served: JSON API + one static HTML page, no build step (recommended — zero new
  dependencies, deploys cleanly to Vercel serverless) vs. server-rendered Jinja2 forms.
- Where live knowledge content lives: the architecture describes it as shipped seed **files**
  (`app/knowledge/seeds/*.json`), but files are read-only and reset on every serverless deploy —
  incompatible with "the client edits it and it applies immediately." The proposal on the table
  (not yet approved) is to store the *live* values in the existing `app_config` table, with the
  shipped files becoming defaults-only fallback.

No design document has been written yet — nothing under `docs/superpowers/specs/` for this. No
implementation plan exists. No code has been written for the admin panel.

### 4b. Phase 4 — THE CONVERSATION (not started, not planned)
The actual point of the project: LiteLLM/Gemini provider layer, `app/knowledge/` (loader +
assembler + cache + seeds), `core/engine.py` (strict-JSON intent extraction), `core/
order_resolver.py` (phone→order + ownership check, **NEW**, no cafe equivalent), windowed memory.
Full end-to-end design already exists: `docs/inbound-conversation-design.md` (10-step flow). No
task-level implementation plan written yet (by design — plans are written one phase ahead so they
don't go stale).

### 4c. Phase 5 — Order push automation (not started, not planned)
Outbox drain (actually sending the queued template messages), button-tap → mutation dispatch
(`order:confirm:{gid}` / `order:cancel:{gid}` → `tagsAdd`/`orderCancel`), two-phase cancel
confirmation.

### 4d. Phase 6 — Parallel run + cutover (not started)
Gated on Q8 (switchover plan — see below).

## 5. Where we stopped, and why

We were mid-way through brainstorming the admin panel design (see 4a) — several scoping and UX
questions had been answered, but the two architecture questions (how it's served, where knowledge
lives) had not yet been presented for approval, and no spec document had been written. You asked
for this status summary instead, which paused that thread. **Nothing was lost** — the decisions
made so far are recorded above and should be picked back up from "where knowledge lives" the next
time this is resumed.

## 6. What's needed from you

1. **Resume or redirect the admin panel design** — either continue answering the two open
   architecture questions in §4a, or tell me to proceed with the recommended options (static
   HTML+JSON API; database-backed knowledge with file fallback).
2. **Approve the `ADMIN_PASSWORD` env-var exception to CLAUDE.md Critical Rule 1** (or pick one of
   the other two storage options discussed) — this is a Critical Rule change, so it needs your
   explicit sign-off, not an assumption on my part.
3. **Create the Supabase project** (you confirmed none exists yet). Rough steps once you're ready:
   create a project at supabase.com (Mumbai/`ap-south-1` region recommended), open
   Project Settings → Database → Connection string, copy the **pooler** (port 6543, "Transaction"
   mode) connection string, and share it so I can apply the schema and unblock the 3 skipped
   Postgres tests.
4. **Answer or triage the remaining open client questions** (Q2, Q6, Q7c, Q8, Q9–Q13) — Q8
   (switchover plan) is the one actively blocking Phase 6 design; the rest can wait.
5. **Decide on the theme visual refresh branch** — it's fully built on
   `worktree-theme-visual-refresh` but not merged. Say when you want it reviewed/merged (it's
   independent of the bot work, no rush either way).
6. **Vercel connection** — deliberately still not connected (your call, from earlier). Once the
   admin panel exists and Supabase is wired in, the steps are: connect the GitHub repo in Vercel,
   set `APP_MASTER_KEY`/`DATABASE_URL`/`APP_ENV=prod`/`CRON_SECRET`/`ADMIN_PASSWORD` as env vars,
   deploy, enter Shopify+Meta credentials via the admin panel, then point Meta's webhook at the
   live URL. I'll walk through this step by step when we get there — and per CLAUDE.md, every
   push to `main` becomes a live deploy once Vercel is connected, so each one needs your explicit
   go-ahead.

## 7. Non-negotiable rules (unchanged, still binding)

1. **The LLM never mutates.** Free-text "cancel my order" → LLM classifies *intent* → re-fetch
   from Shopify → send **buttons** → only the deterministic tap calls `tagsAdd`/`orderCancel`.
2. **Two different HMAC schemes.** Meta = **hex**; Shopify = **base64**. Constant-time, byte
   compare on the raw body — `hmac.compare_digest` raises `TypeError` on non-ASCII header strings.
3. **Ownership check before revealing or mutating.** `AuthorizedOrder` validates at construction
   that the verified phone matches the order (ADR-004).
4. **Always re-fetch live order state** before answering or acting.
5. **Never crash on a signed webhook** — a 500 burns Shopify's 19-failure budget and deletes the
   subscription. Coerce every payload field type.
6. **Serverless has no "run after the response"** — the durable outbox is the only safe pattern.
7. **Secrets:** only `APP_MASTER_KEY` + `DATABASE_URL` in env today; everything else Fernet-
   encrypted in `app_config`, entered via the (not-yet-built) admin panel. `ADMIN_PASSWORD` is a
   proposed third exception, pending your approval (§6.2).
8. **Never `git push` without explicit owner approval.**
9. **Main Claude does not write app code** — route to the `developer` agent. Every phase: build →
   `code-reviewer` → `security-reviewer` (sensitive surfaces) → fix → verify.

## 8. Suggested next action for a new session

1. Read `CLAUDE.md` → `docs/FR/_pipeline_status.md` → this file.
2. Get the two outstanding admin-panel architecture answers (§4a) and the `ADMIN_PASSWORD` rule
   sign-off, then write and commit the admin panel design spec.
3. `superpowers:writing-plans` → `developer` agent → `code-reviewer` → `security-reviewer` for the
   admin panel build.
4. In parallel or after: Supabase DSN unblocks live Postgres tests and the eventual deploy.
5. Then Phase 4 — the conversation engine, the actual point of the project.
