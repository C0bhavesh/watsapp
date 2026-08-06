# Phase 4 — Subagent Conversation Architecture — Design

> Supersedes `docs/superpowers/plans/2026-08-04-phase4-conversation-engine.md` (single
> inline-prompt `engine.py`), invalidated by the round-3 client decision (2026-08-06,
> `docs/FR/client-decisions-all.md`) requiring a subagent architecture instead. Brainstormed
> and approved 2026-08-06.

## What this builds

The actual point of the project: a customer WhatsApps the store, and a router + five
specialist agents resolve their intent and produce a grounded, in-character reply — order
status, product search, store policy, product recommendations, or general/human handoff —
without ever hallucinating a product, mutating an order, or revealing data to the wrong phone
number.

## Why a subagent architecture, not one big prompt

The owner explicitly ruled out a single large inline-prompt design. Beyond following that
instruction, it's the right shape for what's now in scope: five genuinely different jobs
(structured order lookups, Shopify product search, policy grounding, sales-style
recommendations, general triage) each want a different system prompt, different tools, and
different failure modes. One prompt trying to do all five reliably would be harder to test,
harder to keep from hallucinating, and harder to change one behavior without risking the
others.

## Architecture

Every fresh `InboundText` event runs through exactly two LLM calls:

1. **Router** (`app/agents/router.py`) — a fast classification call over the message plus a
   little recent history, returning exactly one of five intents: `order_tracking`,
   `product_search`, `policy`, `recommendations`, `customer_support`.
2. **Specialist** — the one matching agent handles the full turn and writes the
   customer-facing reply directly. No third "composer" call.

`customer_support` is the catch-all: greetings, small talk, unclear messages, and anything
that doesn't cleanly match the other four, so nothing falls through with no response. It also
owns the one-attempt-then-handoff rule (see Human Handoff below).

**Explicitly rejected alternatives** (from brainstorming, kept here so the reasoning isn't
lost): an orchestrator-with-tools design (one agent, all capabilities as callable tools, an
internal reasoning loop) was rejected as functionally the single-prompt architecture the owner
ruled out, just wrapped in tool-calling, and as more expensive/harder to test. A fixed pipeline
(every message runs every subagent, a final step composes) was rejected as wasteful — a "hi"
message would still pay for four unnecessary calls.

**Cross-selling scope, decided explicitly:** cross-sell/upsell only happens when
`recommendations` is the routed intent (the customer asked directly, or router judged the
message as recommendation-seeking). Other specialists (`order_tracking`, `product_search`,
`policy`) never append their own suggestions — keeps every specialist focused on its one job,
keeps "never pushy" easy to verify per-agent instead of needing the same guardrail duplicated
five times, and can be revisited later if it proves too passive in practice.

## Module structure

New package, not folded into `app/core/` — five independently-testable specialists is a
different shape than the superseded single-file `engine.py`, and five prompts crammed into one
file would be an oversized file by this project's own conventions.

```
app/agents/
  base.py               # AgentContext (shared input every specialist receives) + AgentReply
  router.py             # classify_intent(text, history) -> one of 5 intents
  order_tracking.py     # order status / cancel-intent replies -- uses order_resolver
  product_search.py     # Shopify product search (see below) -- new ShopifyClient method
  policy.py             # shipping/return/exchange/refund/privacy/T&C -- grounded in real text
  recommendations.py    # cross-sell/upsell/outfit-matching, ONLY when directly routed here
  customer_support.py   # fallback + handoff owner
```

Each specialist exposes `async run(context: AgentContext) -> AgentReply` — the same shape
across all five, independently testable with a fake `LLMProvider` the same way the superseded
`engine.py` was designed to be tested. Shared building blocks every specialist may draw on
(none of these are duplicated per-agent):

- `order_resolver` (phone/order-name resolution + `AuthorizedOrder` ownership check) — reused
  by `order_tracking`, and by `recommendations`/`customer_support` for VIP detection.
- `sanitize.strip_markdown` — applied once, after whichever specialist produced the reply.
- `KnowledgeLoader` — now carrying the real extracted policy text (see Knowledge below),
  primarily consumed by `policy.py`.
- One shared personality/voice prompt fragment, injected into every specialist's system
  prompt, so tone stays consistent regardless of which agent answers (see Personality below).
  Personality is not one agent's job — it's a cross-cutting constraint on all five.

## Data model additions

Two pieces of new state, both reusing existing, previously-unused schema rather than adding
new columns — `conversations.paused_until` already exists (`backend/app/store/schema.sql:81`)
and has been unused since Phase 4's original design; no new table for VIP status is needed
since it is always computed, never stored.

**Human handoff → `conversations.paused_until`.** When `customer_support` decides to hand
off — the customer explicitly asks for a human, or the AI's one attempt didn't resolve the
issue and they ask again — it sends its final reply ("connecting you with our team on this
chat") and sets `paused_until = now + 24h`. The webhook checks this **before** invoking the
router at all: if a conversation is currently paused, the AI stays completely silent (the
inbound message is still recorded via the existing `persist_turn`/`ConversationStore` path, so
a human reviewing the conversation sees it, but nothing auto-replies). The pause self-expires
after 24 hours — "resets per conversation window" per the client's answer — with no manual
admin action required to resume.

**VIP / repeat-customer detection.** No new table, no new column. VIP classification uses
`order_count` alone, computed on the fly from `order_mappings` grouped by phone (this is
exactly the "kept-indefinitely" data per Q15, so the computation stays cheap and accurate for
the customer's full history, forever). `order_mappings` has no amount/total column — corrected
during planning (2026-08-06) after the original draft of this spec incorrectly assumed
`total_spend` could be computed the same way. Total spend is **not** pre-computed or tracked
proactively; it's looked up live from Shopify (`find_customer_orders_by_phone`, summing
`Order.total`) only on the rare turn a customer explicitly asks about it — acceptable since the
hard rule below means it's essentially never surfaced anyway. The VIP *threshold* (e.g., 3+
orders) is a config value — matches ADR-005 (client decisions as config), tunable from the
admin panel without a redeploy, not hardcoded in a prompt or in Python. The hard constraint
that spend/order count may never be **stated** unprompted (only used to inform tone —
"welcome back" vs. a first-time greeting) lives in the shared personality prompt fragment, not
per-specialist, so it can't be forgotten when a new specialist is added later.

## Product search

Approach A (of three considered — vector/semantic search and a keyword-then-semantic hybrid
were the other two, rejected for now as real new infrastructure a 100–500-orders/day boutique
store doesn't need yet, and can be added later as a clean upgrade if search quality turns out
to matter more in practice than keyword matching does).

One new `ShopifyClient` method, `search_products(query: str, limit: int = 5) -> list[Product]`,
calling Shopify's own `products(query: "...", first: N)` GraphQL search, filtered to
`status:active` and excluding zero-inventory items (mirrors the existing `find_order_by_name`
pattern in `app/shopify/client.py` for how a Shopify search call is structured and error-mapped
in this codebase). `product_search.py` builds the query from the customer's message and is
**structurally** only permitted to describe products present in that result set — this is the
concrete mechanism behind "never hallucinate," not just a prompt instruction. If the search
returns nothing, it broadens the search once (e.g., dropping a color/size qualifier); if that's
still empty, it defers to `recommendations`-style "here's what's similar," and failing that, to
`customer_support` for a human.

**Product recommendations are always filtered to currently-available-for-sale** — never
archived, draft, unavailable, or out-of-stock — enforced by the same `status:active` +
in-stock filter `search_products` already applies, so `recommendations.py` reuses the same
method rather than a separate query path.

## Personality and knowledge grounding

**Personality — "Friendly Fashion Advisor."** A shared prompt fragment (not duplicated
per-agent — one constant, imported by all five) carries: warm, professional,
fashion-knowledgeable, honest, conversational tone; replies in English, Hindi, or Hinglish
matching the customer; emojis used sparingly; never states VIP-derived stats unprompted; never
invents product or policy information. This materially replaces the shipped
`app/knowledge/seeds/brand_voice.md` (currently written as plain "transactional, polite"),
which needs its content rewritten to match, not just its consumer.

**Policy grounding.** `app/knowledge/seeds/faq.json` and `business.json` currently hold
placeholder Thetavas-generic content from Phase 3.5. This design requires them replaced with
the real, verbatim policy text extracted from `D:\TAVAS Website\policy\*.docx` (already
recorded in `docs/FR/client-decisions-all.md`, round 3) — shipping (1–3 day dispatch, 4–7 day
delivery, COD by eligible PIN code, cancel-before-dispatch-only), return (no returns after
delivery except damaged/defective/incorrect within 24h + unboxing video + photos), exchange
(size exchange within 48h, unwashed, stock-dependent, customer pays shipping), refund
(replacement-unavailable only, original payment method, 3–5 business days), privacy, and
terms. **Published policy always takes precedence over AI behavior** if the two ever conflict —
`policy.py`'s system prompt is grounded in this text directly (via `KnowledgeLoader`, same
override-else-seed mechanism already shipped), not a paraphrase the model might drift from.

**Cancellation policy correction.** The existing (superseded) design only considered
`Order.financial_status` when deciding cancel-eligibility. The real Shipping Policy is
dispatch-based ("cancelled only before dispatch"), so `order_tracking`'s cancel-intent handling
must check `Order.fulfillment_status` (or an equivalent dispatch signal) before offering a
cancel path — this is a correction Phase 5's actual cancel-button flow needs to inherit too,
tracked here so it isn't lost.

## Human handoff protocol

One AI attempt per conversation window. If a customer asks for a human, or `customer_support`
judges it can't resolve the issue, the very next reply is the handoff message and
`conversations.paused_until` is set (see Data model above) — no further persuasion, no second
attempt to help. Handoff is to the **same WhatsApp number**, not a separate support line; the
AI's handoff message says a team member will continue in the same chat. This also answers the
original Q11 (no `owner_alert_number` routing is required for this — that field, if it's ever
used, would be for internal ops alerts, a separate and still-open question).

## Order tracking (unchanged from the superseded plan, restated for completeness)

`order_tracking.py` is a thin wrapper around the already-designed `order_resolver` chain
(phone→mapping fast path, Shopify customer-by-phone fallback, order-name lookup with ownership
check) and Shopify's own tracking data — **no live courier/a2ship integration is built**; the
reply simply includes the Shopify tracking link once an order has fulfillment data, and offers
to keep helping or escalate if the customer needs more than that link provides.

## Error handling

- **Router failure or timeout → default to `customer_support`.** Classification breaking must
  never leave a customer with silence.
- **Any specialist failure → the existing fixed `error_fallback` copy** from `app/channels/
  copy.py` (already shipped, proven in Phase 3.5's LiteLLM/Vertex work) — never a raw
  exception, never raw completion text.
- **A specialist receiving a genuinely wrong-fit message** (router misrouted) defers to
  `customer_support`'s fallback rather than forcing an answer it can't ground. No second
  routing pass is built — added complexity for what should be rare given the router only
  chooses among five well-separated buckets.
- **Critical Rules 2 and 3 are unchanged and apply identically across all five specialists:**
  no specialist ever mutates anything (a cancel-intent reply still only produces an intent
  signal; the actual `orderCancel` call stays behind a deterministic button tap, Phase 5's job);
  `order_tracking` only ever reveals order data through the same `AuthorizedOrder`
  runtime-enforced ownership check already shipped in Phase 1.
- **Hardened parsing** (strip reasoning/code-fence wrapping around a completion, extract and
  validate JSON, degrade to the safe fallback on any failure) applies per-specialist, following
  the exact pattern the superseded `engine.py` plan already worked out — not reinvented, just
  applied five times instead of once.

## Testing

Each specialist gets a fake `LLMProvider` returning canned completions (no real network calls),
exactly as the superseded `engine.py` design was already structured to be tested. `router.py`
is tested as a pure classifier — given message X, does it pick the right intent. `product_
search.py` gets a fake `ShopifyClient`, the same pattern `order_resolver`'s existing tests
already use. One webhook-level integration test per intent (five total) confirms the router →
specialist dispatch actually reaches the right agent end-to-end. A dedicated test covers the
pause/resume handoff behavior on `conversations.paused_until` (paused → AI silent but message
recorded; expired → AI resumes normally).

## Explicitly out of scope for this phase

- Live courier/a2ship tracking integration (client decision: link-only).
- Vector/semantic product search (Approach A only; B/C available later if needed).
- Cross-cutting recommendations inside other specialists' replies (recommendations-only,
  narrow scope, per the explicit brainstorming decision above).
- A second routing pass / re-classification when a specialist gets a poor-fit message.
- Any button-tap → mutation dispatch (`tagsAdd`/`orderCancel`) — still Phase 5.
- Q6 (no-match fallback: support-contact-only vs. also-alert-staff) and Q13 (tag-name
  compatibility with a2ship/ops filters) remain open client questions, unaffected by this
  design; `customer_support`'s current fallback behavior (politely can't-find + support
  contact) is used as the default until Q6 is answered, consistent with the existing recorded
  recommendation in `client-decisions-all.md`.

## Open items carried forward (not blocking this design, tracked for the implementation plan)

- The Postgres-backed rate limiter (client decision: Postgres, not Upstash) is not part of this
  conversation-engine work — separate follow-up, replaces the current weak in-memory `slowapi`
  limiter.
- The three tracked LOW/INFO security items from the DPDP review (`reveal_fields` fail-open on
  a corrupted value, controls-validation failures not audited, `processed_messages` residual
  PII on erasure) are unrelated to this design and remain tracked in `_pipeline_status.md`.
