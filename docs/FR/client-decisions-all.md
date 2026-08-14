# Thetavas — All client decisions (one place)

> **Internal note (not for the client):** consolidates every client-facing decision for the
> Shopify + WhatsApp order bot. Recommendations marked. Status: ON HOLD pending client answers
> (see `_pipeline_status.md`). Everything below the line is plain-language and copy-paste ready.
> Convention copied from the Beyond Loaf project.

## ✅ ANSWERS RECEIVED (2026-08-06) — Round 3: Phase 4 business & product decisions

Comprehensive decision set covering Phase 4 architecture, data retention, WATI migration,
scale, AI shopping-assistant behavior (product search, sales strategy, personality, VIP
handling), and the full policy stack (cancellation/return/exchange/refund) — sourced from the
actual policy documents at `D:\TAVAS Website\policy\*.docx` (extracted verbatim below) plus
direct owner decisions. **⚠ Two direct conflicts with already-shipped code, flagged for fix:**
(1) the retention-purge job built 2026-08-06 ages out `order_mappings`/`outbound_messages` by
date, but Q15 below requires order/customer data be kept **indefinitely** — currently harmless
only because `retention_days=0` (disabled) by default; (2) the Phase 4 implementation plan
committed 2026-08-04 (`docs/superpowers/plans/2026-08-04-phase4-conversation-engine.md`) used a
single inline-prompt engine, directly contradicted by decision 1 below (subagent architecture)
— that plan is superseded, not resumable as written.

| # | Question | Answer | Build consequence |
|---|---|---|---|
| **Architecture** | Phase 4 engine design | **Subagent architecture** — specialized agents for product search, order tracking, policies, recommendations, customer support. Explicitly NOT a single large inline-prompt engine. | Supersedes the committed single-`engine.py` plan; Phase 4 needs a fresh design pass before any implementation plan is written. |
| **Q15 — Retention** | What to keep vs. delete | **Keep indefinitely:** customer profile, name, phone, order history, order number/date, products purchased, SKU, payment method (COD/online), order status, total spending, order count, tags (VIP/Repeat Customer). **Delete after 365 days:** AI conversation history, temporary AI context, debug logs, temporary cache, processed-message logs, other non-essential operational data. | Conflicts with the shipped `purge_older_than` (see flag above) — needs a fix so `order_mappings`/`outbound_messages` are excluded from age-based purge, and only conversation/message/operational tables age out at 365 days. Right-to-erasure-on-request (`/admin/erasure`) is unaffected — that stays available regardless of default retention. |
| **Q8 — Switchover** | Migration approach | **Develop/test on a separate WhatsApp number first.** After successful testing, migrate the existing WATI production number to the Meta Cloud API. **No parallel production run required.** | Simpler than the "shadow mode" design (ADR-002's `shadow`/`allowlist` send modes still useful for the test-number phase, just not for a live parallel run on the real number). Gates Phase 6. |
| **Rate limiting** | Login/erasure throttle backing store | **Postgres-backed**, not Upstash Redis (unless future traffic requires it). | Closes the pre-deploy hardening item 1 open decision. Not yet built — the current `slowapi` in-memory limiter is a known-weak placeholder on serverless. |
| **Scale target** | Expected order volume | **100–500 orders/day.** Architecture must scale without major redesign at this range. | Informs DB indexing/outbox throughput assumptions; no immediate action, sanity-check against Phase 2 design. |
| **Q7c / Q12 (clarified)** | Which number, and does it change | **Same business WhatsApp number currently connected to WATI, throughout.** Only the backend changes after migration. | Confirms the higher-risk migration path (same number = a number can only be connected to one system at a time) — the Q8 "separate test number first" answer is what makes this safe: test on a throwaway number, only touch the production number at actual cutover. |
| **Q9 — Post-dispatch cancel** | Cancellation window | **Only before dispatch.** Once dispatched, no cancellation. Matches Shipping Policy verbatim ("Orders can be cancelled only before dispatch"). | `resolve_by_order_name`/cancel flow (Phase 5) must check fulfillment/dispatch status, not just financial status, before offering the Cancel button. |
| **Q10 — a2ship / tracking** | Live courier tracking | **No live courier integration.** Send the Shopify tracking link; continue assisting or escalate to human if more help is needed. | Decisively closes Q10 — no a2ship API integration ever needed for this feature. |
| **Q11 — Handoff number** | Where does "human" go | **No separate support number.** Human support continues on the **same** WhatsApp chat/number. AI tells the customer a team member will continue in the same conversation. | Simpler than originally framed — no `owner_alert_number` routing needed for customer-facing handoff (that field may still be useful for internal ops alerts, TBD). |
| **Human handoff protocol** | How many AI attempts before handoff | **One attempt.** If the customer still asks for a human, or the AI can't resolve it, hand off **immediately** on the second request — do not keep persuading. | New conversation state needed: track "has this customer already asked for a human once in this conversation" — not in the original engine design. |
| **AI product search fallback** | No exact match found | Search for similar products → recommend alternatives → if nothing suitable, offer human assistance. **Never hallucinate or invent product information.** | New Shopify capability needed: product/catalog search (current `ShopifyClient` only has order operations). New subagent: product search. |
| **Product recommendations** | Which products may be recommended | **Only currently available for sale** — never archived, draft, unavailable, or out-of-stock. If nothing suitable is in stock, recommend similar in-stock items or offer human help. | Product search must filter on Shopify availability/status, not just existence. |
| **AI personality** | Bot tone | **"Friendly Fashion Advisor,"** not just a support bot — warm, professional, fashion-knowledgeable, honest, conversational, en/hi/hinglish, emojis used sparingly. | Materially different from the shipped `brand_voice.md` seed (written as plain "transactional, polite") — needs rewriting. |
| **AI sales strategy** | Cross-sell / upsell | Cross-sell relevant products, upsell naturally, recommend full outfit combinations and matching accessories — **always answer the customer's original question first**, never pushy. | New "recommendations" subagent scope; a genuine feature addition beyond original v1 ("we do NOT sell in chat" is now reversed). |
| **VIP / repeat customers** | Personalization | Recognize repeat customers: welcome-back messages, thank returning customers, recommend based on purchase history. **Must NOT proactively state** total spending, order count, or detailed purchase history unless the customer explicitly asks. | Needs customer-tier classification logic (derived from the "keep indefinitely" order-count/spend data) + a hard constraint on the prompt/subagent to never surface those specific fields unprompted. |
| **Policy precedence** | Conflict resolution | Published store policies (shipping/returns/exchanges/refunds/privacy/terms) **always take precedence** over AI behavior if there's ever a conflict. | The policy subagent must be grounded in the actual policy text (extracted below), not a paraphrase, and must not be overridden by the personality/sales instructions above. |

### Extracted policy text (verbatim, from `D:\TAVAS Website\policy\*.docx`, last updated 2026-07-11 except Privacy/T&C undated)

**Shipping Policy:** Orders are dispatched within 1–3 business days. Estimated delivery: 4–7
business days depending on PIN code and courier availability. COD is available only in
eligible PIN codes. Orders can be cancelled only before dispatch.

**Return Policy:** TAVAS does not accept returns once a product has been delivered.
Exceptions: damaged, defective, or incorrect product — notify within 24 hours of delivery,
with a continuous unedited unboxing video and clear photographs showing the issue. Requests
submitted after 24 hours or without the required proof may be declined.

**Exchange Policy:** Damaged/incorrect products — after verification, a replacement is
provided subject to stock availability; if unavailable, a full refund is issued. Size
exchange: request within 48 hours of delivery; product must be unwashed and free from
perfume/deodorant/makeup stains/dirt/signs of use; subject to stock availability; exchange
shipping charges are borne by the customer.

**Refund Policy:** Refunds are issued only when an approved damaged/defective/incorrect
product cannot be replaced. Processed to the original payment method within 3–5 business days
after approval (bank processing times may vary).

**Privacy Policy:** Collects name, phone, email, shipping address, payment details to process
orders; used for order processing/delivery, customer support, order updates, service
improvement. Does not sell personal information; may share only with payment
gateways/couriers/service providers as needed to fulfil orders or comply with law. Customer is
responsible for accurate information; policy may be updated over time.

**Terms & Conditions:** Customers must provide accurate shipping details. TAVAS is not
responsible for courier delays, weather, festivals, strikes, government restrictions, or other
events beyond its control. Slight colour variation (lighting/photography/screen) is not a
defect. Minor variation in print placement, handwork, embroidery, stitching, or fabric texture
is normal, not a manufacturing defect. All exchange/refund requests are subject to quality
inspection. Fraudulent or abusive claims may be rejected; TAVAS may refuse service where
policy misuse is detected. Terms may be updated without prior notice.

**Still open, not addressed by this round:** Q6 (no-match fallback — support contact only, or
also alert staff?) and Q13 (tag-name compatibility with a2ship/ops filters during cutover).

---

## ✅ ANSWERS RECEIVED (2026-07-29)

| Q | Answer | Build consequence |
|---|---|---|
| **1** — which orders get the push | **B — every order** (not COD-only) | `push_eligibility` config = `all`. ⚠ Prepaid orders now carry a Cancel button; flagged to owner. Config edit if reversed. |
| **3** — languages | **English + Hindi + Hinglish** | Templates = `en` + `hi` only (Meta has no `hinglish` code). Hinglish served free-form post-reply. `gu` templates stay approved but dormant. |
| **4** — cancel double-check | **A — ask once before cancelling** | `pending_actions` cancel-confirm flow stays in scope (Phase 5). |
| **5** — what the bot may reveal | **YES — status may be revealed** | Reveal set = order id + email + **status** (confirmed/cancelled/shipped). Items/amounts/tracking still hidden. |
| **14** — FAQ / policy content | **Delegated to us**; client edits later via admin panel | We seed `app/knowledge/seeds/*` with sensible Thetavas defaults; admin panel must expose them for runtime editing. |
| **8** — switchover plan | ⏳ **still open** — explained to owner 2026-07-29 | Gates Phase 6 only. |

**Superseded by round 3 (2026-08-06, above):** Q7c, Q8, Q9, Q10, Q11, Q15 all answered.
Remaining genuinely open: **Q6** (no-match fallback: support contact vs staff alert) · **Q13**
(tag-name compatibility with a2ship/ops filters during cutover).

---

**Thetavas WhatsApp order assistant — a few decisions we need from you**

The plan is confirmed: when a customer places an order on the website, they get a WhatsApp
message with Confirm / Cancel buttons (replacing the current third-party tool), and customers
can also ask about their order on WhatsApp and get an automatic answer. Before we build,
please confirm the choices below. For most you can just reply "agree with the recommendation."

## Part 1 — The order-confirmation message

**1. Which orders should get the Confirm/Cancel WhatsApp message?**

- A (recommended): only Cash-on-Delivery orders — prepaid customers already paid, asking them
  to confirm mostly creates accidental cancellations.
- B: every order (this is what most confirmation tools do by default).
- C: every order, but prepaid customers get a simple "order received" message without buttons.

**2. Message cost awareness (no decision, just confirm you're aware):** WhatsApp charges per
business-initiated message. An order-update ("utility") message in India costs about
**₹0.12–0.13 per message**. Everything the customer and the bot chat afterwards (within 24
hours of their reply) is free. Roughly how many orders per day do you get, so we can estimate
the monthly cost?

**3. In which languages should the confirmation message be sent?** WhatsApp requires the
first message to use a pre-approved template, and each language needs its own approved
version. The follow-up conversation automatically happens in the customer's language.

- A (recommended): start with English + Hindi, add Gujarati next.
- B: English only to start.
- C: all three from day one.

**4. If the customer taps Cancel — cancel instantly or double-check?**

- A (recommended): ask once — "Are you sure? Reply YES to cancel order #1234" — because a
  Shopify cancellation cannot be undone.
- B: cancel immediately on the tap (what your current tool likely does).

## Part 2 — Order questions on WhatsApp

**5. When a customer asks "where is my order?", what may the assistant tell them?** You said
order number and email for now. Please confirm the assistant may also state the order's
**status** (confirmed / cancelled / shipped) — without that it cannot really answer the
question. Items, amounts, and tracking links stay hidden until you say otherwise.

**6. If the customer's WhatsApp number doesn't match any order** (e.g. they ordered with a
different number), the assistant will ask for their order number. If that also doesn't match:

- A (recommended): politely say it can't find the order and share your support contact.
- B: also alert a staff member on WhatsApp so a human follows up.

## Part 3 — Switching off the current tool

**7. Confirming the current WhatsApp tool — we believe it is WATI.** Your store's webhook
settings show a custom bridge (`tavas-wati-webhook` on Vercel) receiving every order and
forwarding to WATI. Please confirm: (a) the confirm/cancel messages come from WATI; (b) who
maintains that Vercel bridge; (c) which WhatsApp number WATI uses today — is it the same
verified number we will move to our system, or a different one? (If it's the same number, the
switchover needs one extra step, since a number can only be connected to one system at a time.)

**8. Switchover plan:**

- A (recommended): we build and test on real orders while your current tool stays on; when you
  approve, we switch ours on and yours off the same day.
- B: turn the old tool off as soon as our version is ready for testing.

**13. Order-tag names after the switch.** Today, confirmed/cancelled orders get the tags
"Confirmed by wati" / "Cancel by wati" and new COD orders get "COD pending". If your team
(or your a2ship setup) filters or searches orders by these exact tag names, our system
should either keep writing the same names or you update the filters when we switch.

- A (recommended): our system writes clean new tags ("confirmed", "cancelled") AND, until
  cutover is complete, also the old names for compatibility — nothing downstream breaks.
- B: keep the old tag names permanently.
- C: only the new clean tags — you confirm nothing filters on the old names.

**14. Store information the assistant should know.** Beyond order status (which it reads from
Shopify), customers ask general questions. Please send us your current answers to:

- How long does delivery usually take? (and does it differ by region?)
- Return / exchange policy — window, conditions, who pays return shipping?
- Cash-on-Delivery rules — any extra charge, any pincode restrictions?
- What should a customer do if a product arrives damaged or wrong?
- Support contact (phone / email / hours) for anything the assistant cannot handle.
- Anything else customers regularly ask.

Whatever you send becomes the assistant's knowledge; it will not invent answers. You can update
this at any time and the change applies immediately, without new software work. Also: any
wording or tone you want it to always use (or avoid) when talking to your customers?

## Part 4 — Held items (answer whenever ready)

**9. Cancel requests after the order is already shipped** — the assistant will say it can't
cancel and share support contact. OK, or different handling?

**10. a2ship** — should the assistant ever answer with courier/tracking status? If yes, we
need to check whether tracking numbers flow back into Shopify or only exist inside a2ship.

**11. Human handoff** — which WhatsApp number should receive alerts when the assistant can't
help? (Can be added any time.)

**15. How long should we keep customer data?** We store a customer's phone number, order
details, and chat history so the assistant can answer their questions. Indian data-protection
law (DPDP) expects a stated retention period and a way to delete someone's data on request.

- A (recommended): keep data for 12 months after their last order or message, then delete it
  automatically; delete sooner if a customer specifically asks.
- B: a different retention period — tell us how long.
- C: keep indefinitely for now (not recommended, but your call).

The delete-on-request capability is being built regardless of which period you choose.

## Part 5 — One technical confirmation about your WhatsApp number

**12. Your purchased verified WhatsApp number** — please confirm it is registered on the
**WhatsApp Business Platform (Cloud API)** under your Meta Business Manager, and NOT currently
running in the WhatsApp Business phone app. A number cannot do both at once; if it's in the
phone app today, moving it to the API will disconnect it from the app (we'll guide you through
this). Also please confirm you have access to the Meta Business Manager account that owns it.

## Part 6 — New question (2026-08-13)

**17. One-time reminder for an un-answered COD confirmation.** If a customer receives the
order-confirmation WhatsApp message and does not tap Confirm or Cancel within 1 hour, should we
resend the same message once as a reminder (and never again after that)?

- A (owner-directed, being built now): yes, exactly one reminder at the 1-hour mark, same
  template, same Confirm/Cancel buttons. Note: this is a second UTILITY-category template send
  (billed the same as the first) for any order that goes unanswered that long.
- B: no reminder — an unanswered order simply stays pending until the customer replies on their
  own or contacts support.

Being built as an owner-directed decision pending your confirmation; fully gated behind the
same send-mode kill switch as every other outbound message, so it has no live effect until
explicitly enabled.

## ✅ ANSWERED (2026-08-12)

**16. Whose phone number should unlock an order in WhatsApp chat?** When a customer messages
the assistant asking about their order, we now look it up in our own database by phone number
first (faster, and it still falls back to checking Shopify directly if there's ever a miss).
An order can have up to three phone numbers on it: the buyer's own number, a shipping-address
number, and a billing-address number — these are sometimes different people, for example when
an order is a gift and the shipping number belongs to the recipient, not the buyer.

**Answer: use the shipping mobile number.** This also matches a real constraint in your order
data: your checkout doesn't always capture the buyer's own phone number on the order record, but
the shipping contact number is reliably present — the order-confirmation push (built earlier)
already falls back to it for the same reason. The billing number stays out of scope (not asked
for). Shipped: the chat lookup now matches the order's own phone number OR its shipping phone
number.

<details>
<summary>Original question (for reference)</summary>

- A (recommended): only the buyer's own phone number unlocks the order in chat. If someone
  else's number was used only for shipping or billing on that order, messaging from that number
  will not surface the order.
- B: any of the three numbers on the order unlocks it in chat (matches how we already handle a
  customer's own request to delete their data, where checking all three numbers is intentionally
  broad). This means, for example, a gift recipient could message and see the buyer's order
  details.

We initially shipped Option A as a conservative default pending this answer.

</details>
