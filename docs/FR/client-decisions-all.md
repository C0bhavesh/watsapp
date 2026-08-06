# Thetavas — All client decisions (one place)

> **Internal note (not for the client):** consolidates every client-facing decision for the
> Shopify + WhatsApp order bot. Recommendations marked. Status: ON HOLD pending client answers
> (see `_pipeline_status.md`). Everything below the line is plain-language and copy-paste ready.
> Convention copied from the Beyond Loaf project.

## ✅ ANSWERS RECEIVED (2026-07-29)

| Q | Answer | Build consequence |
|---|---|---|
| **1** — which orders get the push | **B — every order** (not COD-only) | `push_eligibility` config = `all`. ⚠ Prepaid orders now carry a Cancel button; flagged to owner. Config edit if reversed. |
| **3** — languages | **English + Hindi + Hinglish** | Templates = `en` + `hi` only (Meta has no `hinglish` code). Hinglish served free-form post-reply. `gu` templates stay approved but dormant. |
| **4** — cancel double-check | **A — ask once before cancelling** | `pending_actions` cancel-confirm flow stays in scope (Phase 5). |
| **5** — what the bot may reveal | **YES — status may be revealed** | Reveal set = order id + email + **status** (confirmed/cancelled/shipped). Items/amounts/tracking still hidden. |
| **14** — FAQ / policy content | **Delegated to us**; client edits later via admin panel | We seed `app/knowledge/seeds/*` with sensible Thetavas defaults; admin panel must expose them for runtime editing. |
| **8** — switchover plan | ⏳ **still open** — explained to owner 2026-07-29 | Gates Phase 6 only. |

Remaining open: **Q2** (order volume, for cost estimate) · **Q6** (no-match fallback: support contact vs staff alert) · **Q7c** (which WhatsApp number WATI uses today) · **Q8** (switchover) · **Q9–Q11** (held: post-shipping cancel, a2ship tracking, handoff number) · **Q12** (number on Cloud API confirmation) · **Q13** (tag-name compatibility).

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

Please reply with your choice for each (or "agree with recommendations, except…"). Thank you.
