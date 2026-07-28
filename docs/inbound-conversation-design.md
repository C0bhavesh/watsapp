# Inbound Conversation Design — "customer messages us, the bot answers"

> Created 2026-07-28. This is the CORE feature. Owner correctly flagged that the earlier plan
> under-weighted it. Mirrors `D:\ai_whatsapp_agent` (cafe bot) end to end, with Shopify order
> context replacing the cafe's menu/catalog context.

---

## The whole path, one message at a time

```
Customer types on WhatsApp: "mera order kaha hai?"  (hi/en/hinglish/gu)
        │
        ▼
POST /webhook/whatsapp                       ← Meta Cloud API delivers
        │
 1. verify HMAC  X-Hub-Signature-256 = HEX   ← (Shopify is base64 — two different verifiers)
 2. extract_event() → typed union            ← status callbacks / unknown types → None → 200
 3. dedupe on message_id (DB authority)      ← Meta retries; must never double-answer
        │
        ├── InboundButton      (template tap)      → DETERMINISTIC router  → Flow B (confirm/cancel)
        ├── InboundInteractive (reply-button tap)  → DETERMINISTIC router  → cancel confirm/abort
        └── InboundText        (free text)         → THE CONVERSATION PATH ↓
        │
 4. resolve identity + orders
        order_resolver.resolve(wa_id)
          a. normalize wa_id → E.164
          b. order_mappings lookup by phone  → [orders]        ← built by Phase 2 webhook
          c. none? → customers-by-phone → orders-by-customer_id (needs read_customers)
          d. still none? → ask for order number → orders(name:) → OWNERSHIP CHECK
        │
 5. re-fetch LIVE order state from Shopify   ← never answer from our snapshot (rule 5.4)
        │
 6. build the LLM turn
        system context = KnowledgeAssembler.system_context()
            ├── brand_voice.md   (Thetavas tone: warm, transactional, no emojis)
            ├── faq.json         (delivery times, returns, COD, damaged goods)  ← CLIENT Q14
            ├── business.json    (store name, hours, support contact)           ← CLIENT Q14
            └── patterns.json    (few-shot examples of real customer phrasings)
        + ORDER CONTEXT BLOCK (only fields allowed by `reveal_fields` config — client Q5)
        + conversation memory (last N turns + rolling summary)
        + user message
        │
 7. Gemini (LiteLLM) returns STRICT JSON — one completion:
        {"analysis": "...", "intent": "order_status|cancel_request|chat|handoff",
         "order_name": "tavas3733" | null,
         "reply": "<warm reply in the customer's language>"}
        │
 8. parse_llm_selection() hardening  ← copied from cafe verbatim
        strip <think>, strip ``` fences, json.loads, outermost-{} fallback,
        validate order_name against THE CUSTOMER'S OWN orders,
        never leak raw JSON to the customer (safe fallback string instead)
        │
 9. act on intent
        order_status  → send LLM reply (free-form, inside 24h window = FREE)
        cancel_request→ LLM does NOT cancel. We re-fetch, then send BUTTONS:
                        [Yes, cancel] [No, keep it]  → only the tap mutates
        chat          → send LLM reply
        handoff       → fixed copy + alert owner
        │
 10. persist turn (user msg + assistant msg + summary) in ONE transaction
```

## Why each piece exists (the non-obvious parts)

**Deterministic before LLM.** Steps 3→4 dispatch button taps *before* the engine is ever called.
A tap is already unambiguous; sending it to an LLM adds cost, latency, and hallucination risk to
an operation that cancels real orders. Same pattern that makes the cafe bot safe.

**The LLM never mutates (Critical Rule 2).** Free-text "cancel my order" produces an *intent*,
not an action. We re-fetch the order and send a button. Only the deterministic tap calls
`orderCancel`. This is why step 9 splits intent from action.

**Ownership check before revealing anything (Critical Rule 3).** Step 4d: if the customer gives
an order number, we verify that order's phone matches the WhatsApp sender before saying a word
about it. Otherwise anyone could read anyone's order by guessing `tavas3734`.

**Re-fetch, don't trust the snapshot (rule 5.4).** `order_mappings` holds a creation-time
snapshot for lookup only. Status can change in Shopify (staff cancels, a2ship fulfils) with no
webhook to us, so every answer reads live state.

**Knowledge, not improvisation.** Order facts come from Shopify; everything else
(delivery time, returns, COD rules) comes from the seeds. If the client hasn't given us an
answer, the bot says it will connect a human — it does not invent policy.

**The 24-hour window makes conversation free.** The customer's message opens it, so every reply
in this flow costs nothing (only the business-initiated order push costs ~₹0.12).

## Module map (all `[copy]`/`[adapt]` = from D:\ai_whatsapp_agent)

| Module | Source | Role in this flow |
|---|---|---|
| `channels/whatsapp.py` | [adapt] | webhook GET verify + POST receive, dispatch (step 1-3) |
| `channels/whatsapp_signature.py` | [copy] | HEX HMAC (Meta) — distinct from Shopify's base64 |
| `channels/whatsapp_inbound.py` | [adapt] | typed events + **NEW `InboundButton`** (template taps) |
| `channels/whatsapp_sender.py` | [copy+] | `send_text`, **NEW `send_template`**, **NEW `send_buttons`** |
| `channels/copy.py` | [NEW] | fixed strings (confirm/cancel/not-found/refusal) ×4 languages |
| `core/order_resolver.py` | [NEW] | step 4 chain + ownership → issues `AuthorizedOrder` |
| `core/engine.py` | [adapt] | steps 6-8: prompt assembly, JSON parse, hardening |
| `core/memory.py` | [copy] | windowed history + rolling summary |
| `core/sanitize.py` | [copy] | strip markdown (WhatsApp renders `**` literally) + strip reasoning |
| `knowledge/{loader,assembler,cache}.py` + `seeds/` | [adapt] | step 6 system context |
| `providers/` (LiteLLM + registry) | [copy] | Gemini call, error kinds, single retry |

## Re-sequenced phases (2026-07-28)

The conversation is the product, so it ships before the confirm/cancel push automation:

| Phase | Deliverable | Why this order |
|---|---|---|
| ~~1~~ ✅ | Shopify client, token manager | needed by every flow |
| ~~2~~ ✅ | orders/create webhook → phone→order mapping | **this is what makes "who is this customer?" answerable** — it feeds the conversation, not just the push |
| **3** | **WhatsApp channel**: Meta webhook (verify/receive/dedupe), inbound parser incl. `InboundButton`, sender (text/template/buttons), copy module | the pipe both directions need |
| **4** | **THE CONVERSATION**: providers + knowledge + engine + order_resolver + memory → customer asks, bot answers, live on the test number | the core feature, delivered as early as its dependencies allow |
| **5** | Order push + Confirm/Cancel automation (outbox drain, button mutations, two-phase cancel) | replaces WATI; reuses everything above |
| **6** | Parallel run (shadow mode) → cutover | |

Phases 1-2 were not detours: the mapping table built in Phase 2 is exactly how step 4b answers
"which orders belong to this WhatsApp number" — the question the cafe bot never had to ask.
