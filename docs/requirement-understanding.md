# Requirement Understanding (updated 2026-07-28)

> **v1 SCOPE LOCKED (client answers 2026-07-28):**
> - WhatsApp: **Meta Cloud API direct** (like the cafe project); client already purchased a
>   verified WhatsApp Business number.
> - **v1 = replicate the current 3rd-party tool with our own software:** Shopify order placed
>   → `orders/create` webhook → WhatsApp UTILITY template with **Confirm / Cancel** buttons →
>   Confirm = `tagsAdd "confirmed"`; Cancel = `orderCancel` + tag `"cancelled"` → success reply.
> - Plus user-initiated Q&A (customer asks about their order; bot reveals **order id + email
>   + status only** for now) via Gemini.
> - **NOT in v1:** address change (future), a2ship/tracking (held), payment anything (fully on
>   Shopify checkout — out of scope).
> - Hosting: Vercel + Supabase, same as cafe bot. Languages: Hindi/English/Hinglish/Gujarati.
> - Template answer: templates ARE mandatory for the order push (business-initiated); the cafe
>   project never needed them because it is purely reactive. See FR/_pipeline_status.md.
> - Pipeline tracking now lives in `docs/FR/_pipeline_status.md` (cafe-project convention);
>   open client questions in `docs/FR/client-decisions-all.md`.

## Client context

- Client runs a **Shopify** store (kurtis / women's ethnic wear): `thetavas.myshopify.com`.
- Uses **a2ship** for shipping/fulfillment (exact role in this flow still to confirm).
- Wants customers to interact over **WhatsApp**, and our backend to read/update their
  Shopify orders automatically.

## The two flows in scope

### Flow 1 — User-initiated (customer asks about their order)
Customer sends a WhatsApp message ("where is my order?", "cancel my order", "order
status of #tavas3723"). We identify the customer (by WhatsApp phone number and/or the
order number they type), fetch order data from Shopify, and answer. An LLM (Gemini,
as in the cafe project) interprets the free-text question and drafts the reply.

### Flow 2 — Business-initiated (order confirmation push, per req.md)
New Shopify order → webhook to our backend → store phone→order mapping → send WhatsApp
**template** message "Reply CONFIRM / CANCEL / CHANGE ADDRESS" → customer taps a button
→ we call the matching Shopify API (tagsAdd confirmed / orderCancel / orderUpdate
address) → send success message.

This confirm/cancel flow is the classic **COD order confirmation** pattern used by
Indian D2C apparel sellers (tags like `confirmed` then typically drive which orders get
shipped via a2ship). To be confirmed with the client — see open questions.

## Why a Shopify app is needed (connection method)

Shopify does not hand out standalone API credentials. The official way to connect
custom software is to **create an app**, install it on the store, and authenticate with
the app's credentials. Our chosen method (create app → client_id + client_secret →
client credentials grant → 24h access token → Admin GraphQL API) is officially
documented and correct. Full verification: `shopify-connection-verification.md`.

## Core architecture (agreed direction)

```
                     ┌────────────────────────────┐
 Customer  ⇄  WhatsApp Cloud API  ⇄  Our Backend  │
                     │   (FastAPI, like cafe bot)  │
 Shopify ──webhook──▶│                            │
                     │  ├─ Meta webhook: parse msg, verify HMAC (hex)
                     │  ├─ Shopify webhook: orders/create, verify HMAC (base64)
                     │  ├─ DB: phone → order mapping, conversations
                     │  ├─ Gemini (LiteLLM): intent + reply for free text
                     │  ├─ Deterministic button router for CONFIRM/CANCEL/ADDRESS
                     │  └─ Shopify client: token manager + 5 API operations
                     └───────────▶ Shopify Admin GraphQL API
```

Key design rules (inherited from the cafe project — see
`reference-project-ai-whatsapp-agent.md` §7):

1. **Two-tier routing:** anything that *mutates* an order (confirm/cancel/address) goes
   through deterministic button-tap routing, never through LLM free-text. The LLM only
   handles open-ended questions and drafting replies.
2. **Never trust claimed state:** always re-fetch the order from Shopify before acting;
   validate any LLM-extracted order id against the customer's actual orders.
3. **Confirm destructive actions:** orderCancel is irreversible — always require an
   explicit "YES" confirmation step.
4. **Authorization check:** only ever show/modify orders that belong to the WhatsApp
   number that is messaging us (or verify order number + something else if no match).

## Phone → Order resolution (the req.md "Important Finding")

Orders can NOT be searched by phone in Shopify. Three resolution paths, in order:

1. **Primary:** local DB mapping built from the `orders/create` webhook
   (phone → order GID). Fast, one DB read.
2. **Fallback (Shopify-only, verified supported):**
   `customers(query:"phone:+91...")` → customer GID → `orders(query:"customer_id:...")`.
   Covers orders placed before our webhook went live.
3. **Last resort:** customer types their order number → `orders(query:"name:...")`,
   then cross-check that order's phone/customer against the WhatsApp sender.

## Tech stack direction (mirroring the proven cafe project)

- Python 3.12 + FastAPI, async, deployed like the cafe bot (Vercel) or any host with HTTPS.
- Postgres (Supabase) for mappings/conversations/config; Fernet-encrypted secrets.
- LiteLLM with Gemini (Vertex `gemini-3.5-flash` in cafe prod) for intent + replies.
- Meta WhatsApp Cloud API directly via httpx (same sender/webhook modules).
- **New pieces to build:** Shopify token manager (24h refresh), Shopify GraphQL client
  (5 operations), Shopify webhook receiver (base64 HMAC), WhatsApp **template** sending,
  phone-number normalization (+91 / E.164).
