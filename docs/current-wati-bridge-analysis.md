# Current WATI Bridge Analysis — github.com/C0bhavesh/tavas-wati-webhook

> Analyzed 2026-07-28. This is the owner-built Vercel bridge currently wired to the two
> admin-created webhooks (`/api/webhook` = order creation, `/api/order-update` = order update).

## How the current system actually works

```
Shopify order created ──(admin webhook)──▶ /api/webhook (Vercel, Node/axios)
   ├─ extract phone: order.phone → customer.phone → shipping_address.phone → billing_address.phone
   ├─ payment type from tags: "cod" → cod · "online" → prepaid
   ├─ cod_confirmation_status from tags: "confirmed by wati" | "cancel(led) by wati" | "cod pending"
   ├─ build ~20 contact attributes (name, phone, email, order_id, order_number
   │    = "tavas"+order_number, total, gateway, city/state/pincode, products, quantities…)
   └─ push to WATI: prepaid → addContact · COD → updateContactAttributes

Shopify order updated ──(admin webhook)──▶ /api/order-update
   └─ re-derive cod_confirmation_status from tags → push ONLY that attribute to WATI
```

**Key insight: the bridge never sends a WhatsApp message.** It only syncs contact
attributes INTO WATI. The actual confirm/cancel message, the button flow, and the
tag-writing back to Shopify ("Confirmed by wati" / "Cancel by wati") happen inside
**WATI's own automations + WATI's Shopify integration** (WATI holds its own Shopify
access). The bridge is a data-feeder; WATI is the brain.

**Note:** the repo contains **no tracking-id logic** — order-update only relays COD
status. If tracking/shipping messages exist today, they come from WATI's own automations
or a2ship directly, not from this bridge.

## Facts this repo locks down for our build (reuse as-is)

| Convention | Value |
|---|---|
| Phone extraction chain | `order.phone → customer.phone → shipping_address.phone → billing_address.phone` (matches our live-order verification exactly) |
| COD detection | tag `cod` (Shopflo writes it) — we pair it with `paymentGatewayNames` as secondary |
| Prepaid marker | tag `online` |
| Display order number | `"tavas" + order_number` (e.g. tavas3733) |
| Current tag vocabulary | `COD pending` (awaiting), `Confirmed by wati`, `Cancel by wati` (+ spelling variants), `HIGH_RISK`, `Shopflo` |
| Webhook payload format | REST-style JSON (snake_case, `tags` as comma-string) — matches admin-created webhooks; our app-created subscription payloads are equivalent |

## Gaps in the current bridge that our build fixes

1. **No HMAC verification** — anyone who discovers the URL can POST fake orders → fake
   WhatsApp messages to arbitrary numbers. We verify `X-Shopify-Hmac-Sha256` on every delivery.
2. **No idempotency** — Shopify redeliveries cause duplicate pushes (benign for attribute
   sync; would be duplicate customer messages in our design). We dedupe on webhook id.
3. **No database** — pure passthrough; cannot answer "where is my order", cannot map
   phone→order. Our `order_mappings` table is the core addition.
4. **Errors return 500** with no retry budget thinking — combined with no dedupe this
   invites retry storms. We ack-200-fast + process async.
5. **Two-vendor split-brain** — data in WATI, flows in WATI, tags via WATI's Shopify app,
   bridge on Vercel. Ours is one system: webhook → our backend → Meta Cloud API directly
   (no WATI fee) → deterministic buttons → our own Shopify mutations → Gemini for free-text.

## New client/ops question surfaced (tag compatibility)

The current flow's downstream may depend on the exact tag names: if the client's ops (or
their a2ship setup, which receives every order webhook) filters on
`Confirmed by wati` / `COD pending`, our bot's tags must either use the SAME names or the
downstream filters must be updated at cutover. → added to client-decisions as Q13.

## Verdict: same way or not?

**Same skeleton — webhook-triggered, same phone/COD conventions, same Vercel hosting —
but a fundamentally upgraded design:** one system instead of bridge+WATI, security
(HMAC) + idempotency + DB added, messages sent by us via Meta Cloud API with template +
buttons, mutations done by us via Admin GraphQL, and an LLM Q&A layer WATI cannot offer.
The repo's conventions (phone chain, tags, prefix) carry over verbatim as compatibility
requirements.
