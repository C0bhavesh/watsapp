# Open Questions — ROUND 1 ANSWERED 2026-07-28 (historical)

> **Superseded:** client answered round 1 on 2026-07-28. Answers are recorded in
> `FR/_pipeline_status.md` (row "Client answers — round 1"); all still-open questions
> now live in `FR/client-decisions-all.md`. Kept for history.
>
> Quick answer key: 1→Meta Cloud API direct · 2→verified number purchased · 4→replicate
> current 3rd-party confirm/cancel tool, webhook confirmed possible · 5→payment fully on
> Shopify, out of scope · 6→HOLD · 7→address change NOT in v1 · 8→order id + email only ·
> 9→HOLD · 10/11→later · 12→Vercel like cafe bot · 13→Supabase yes · 14→yes, details later ·
> 15→unknown · 16→hi/en/hinglish/gu.

## WhatsApp side
1. **Which WhatsApp API access does the client have?** Meta WhatsApp Cloud API directly
   (own Meta Business + WABA, like the cafe project), or a BSP (AiSensy, Gupshup,
   360dialog, Twilio, Interakt…)? This decides our send/receive integration entirely.
2. **Is the client's number already on the WhatsApp Business Platform?** A number can't
   be on both the normal WhatsApp Business *app* and the Cloud API at the same time.
3. **Business-initiated messages need approved templates** (and per-conversation Meta
   pricing). Who owns template creation/approval and the Meta billing?

## Flow scope
4. Is Flow 2 (auto "CONFIRM / CANCEL" push on every new order) in scope for v1, or is
   v1 only Flow 1 (customer asks, bot answers)? req.md describes Flow 2; the verbal
   requirement described Flow 1.
5. Is this a **COD confirmation** flow? Should the `confirmed` tag gate shipping (i.e.
   only confirmed orders get pushed to a2ship)? Prepaid orders too, or COD only?
6. What should happen when a customer requests cancel **after fulfillment/shipping**?
   (Shopify will refuse or it's too late — handoff to a human?)
7. Address change: allowed only before fulfillment (our proposed rule) — confirm.
8. What info may the bot reveal? (order items, amount, payment status, tracking link?)
   Any customer-verification step needed beyond matching the WhatsApp number?

## a2ship
9. What exactly does a2ship consume from Shopify (tags? fulfillment status?) and does
   the client want tracking status in WhatsApp answers? If yes — does tracking info
   come back into Shopify (fulfillment tracking number) or only exist in a2ship? Do we
   need a2ship API access?

## Shopify app
10. Confirm the app was created under the **client's** Shopify organization (Dev
    Dashboard) and installed on the store, with scopes `read_orders, write_orders,
    read_customers` (+ protected customer data access enabled). The client-credentials
    grant officially requires the app and store to belong to the same organization.
11. Bump API version from `2025-07` (end-of-support ~now) to `2026-07` and re-test.

## Operations
12. Where do we host the backend? (Cafe bot runs on Vercel serverless + Supabase — same?)
13. Which database? (Supabase Postgres proposed, matching the cafe project.)
14. Human handoff: which number/inbox gets alerts when the bot can't help (refund
    demands, disputes, unmatchable phone numbers)?
15. Expected order volume/day (sizing, rate limits, Meta conversation costs).
16. Language(s) for customer replies (Hindi/Hinglish/English/Gujarati…)? The cafe
    engine already replies in the customer's language.
