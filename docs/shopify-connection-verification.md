# Shopify Connection — Verification Report

**Date verified:** 2026-07-27 (against official shopify.dev documentation)

## Verdict

✅ **The setup is correct and officially supported.** Shopify explicitly supports this
exact pattern — creating your own app, installing it on the store, and using the
**client credentials grant** (client_id + client_secret → 24-hour access token → Admin
GraphQL API). It is documented as the recommended approach for *"trusted,
server-to-server integrations owned by your organization (for example, internal
automation or back-office services)"* — which is exactly what this project is.

This is NOT a workaround or a hack. Shopify deliberately does not hand out raw API
credentials; the "create an app to integrate custom software" path is the official way.

---

## What was verified, item by item

### 1. Get token API (client credentials grant) — ✅ CORRECT

- Endpoint: `POST https://{shop}.myshopify.com/admin/oauth/access_token`
- Body params: `grant_type=client_credentials`, `client_id`, `client_secret` — matches req.md exactly.
- Token lifetime: **24 hours** (`expires_in: 86399`). Refresh = simply make the same request again. There is no separate refresh token.
- Constraint: the grant is only for apps **developed by your own organization and installed on stores you own/control**. Since the store (thetavas) belongs to the client, the app should be created under the **client's** Shopify account / Dev Dashboard organization (with you added as staff/collaborator). If it was created that way, fully compliant.
- Access scopes must be pre-configured in the app configuration (Dev Dashboard / `shopify.app.toml`) — they are granted at install time, not at token time.

Source: https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant

**Implementation note:** cache the token and refresh proactively (e.g., refresh when
< 1 hour remaining, or on a 401). Do not fetch a new token per request.

### 2. Fetch order by order number — ✅ CORRECT

- `orders(first:1, query:"name:tavas3723")` — the `name:` filter is officially supported.
- Also officially supported filters (relevant to us): `email`, `customer_id`, `tag`, `status`, `financial_status`, `fulfillment_status`, `created_at`.
- ❌ **Phone is NOT a supported order filter** — req.md's "Important Finding" is confirmed correct. `phone:` on the orders query is silently ignored.
- ✅ **However `customer_id:` IS supported** — this gives a Shopify-only fallback path:
  `customers(query:"phone:+91...")` → customer GID → `orders(query:"customer_id:...")`.
  So a local phone→order DB is the *recommended* path (fast, reliable), but not the *only* path.

Source: https://shopify.dev/docs/api/admin-graphql/latest/queries/orders

### 3. Add tags (tagsAdd) — ✅ CORRECT

- `tagsAdd(id, tags)` mutation as written in req.md is valid. Tags like `confirmed` /
  `cancel-requested` are a good, non-destructive way to mark order state (and they are
  visible/filterable in Shopify admin, and usable by a2ship / fulfillment rules).

### 4. Cancel order (orderCancel) — ✅ CORRECT, two small notes

- Required args confirmed: `orderId: ID!`, `reason: OrderCancelReason!`, `restock: Boolean!` — matches req.md.
- Useful optional args: `notifyCustomer: Boolean` (default false — set true if the client wants Shopify's own cancellation email), `refundMethod`, `staffNote`.
- Note A: the `userErrors` field is **deprecated** on this mutation — read `orderCancelUserErrors` instead.
- Note B: cancellation is **asynchronous** (returns a `job`). The order is not guaranteed cancelled the instant the mutation returns.
- Cannot cancel if: already cancelled, pending payment authorizations, active returns, or un-cancellable fulfillments. Handle these errors → tell the customer "please contact support."
- req.md is right that **cancellation is irreversible** — the bot must always ask for explicit confirmation ("Reply YES to cancel order #1234") before calling this.

Source: https://shopify.dev/docs/api/admin-graphql/latest/mutations/ordercancel

### 5. Update shipping address (orderUpdate) — ✅ CORRECT, with a business rule

- The mutation as written is valid.
- Business rule to add ourselves: only allow address change while the order is
  **unfulfilled** — Shopify will happily update the record after shipping, but the
  parcel already left. Check `displayFulfillmentStatus` first.

### 6. Search customer by phone — ✅ CORRECT

- `customers(query:"phone:+919876543210")` is supported (needs `read_customers` scope).
- Phone must be in E.164 format (+91...). WhatsApp gives numbers without `+` (e.g.
  `919876543210`) — normalize before searching.

---

## Additional findings (not in req.md)

### A. Protected customer data (PII: name, address, email, phone)

- Order/customer PII is "protected customer data" (Level 2 = name, address, email, phone).
- **Custom apps do NOT require Shopify approval** — access is "always available" for
  single-store custom apps (public App Store apps are the ones that need review).
- BUT for Dev Dashboard apps the access may still need to be *declared/enabled* in the
  dashboard (API access → Protected customer data access). **Gotcha:** if PII fields
  come back as `null` with an `errors` array mentioning redaction while HTTP status is
  still 200 — this setting is the cause, not a bug in our code.

Source: https://shopify.dev/docs/apps/launch/protected-customer-data

### B. API version — ⚠️ ACTION NEEDED

- req.md uses `2025-07`. Shopify supports each version ~12 months; as of **July 2026**
  the latest stable is **2026-07** and `2025-07` is at end-of-support (requests to an
  unsupported version silently fall forward to the oldest supported version).
- **Recommendation:** pin `2026-07` (or `2026-04`) in one config constant and re-test
  the five calls. Review Shopify's quarterly release notes going forward.

Source: https://shopify.dev/docs/api/usage/versioning

### C. Webhooks (needed for the "send template on new order" flow)

- Topic `orders/create` is supported. Two ways to subscribe:
  1. Declaratively in `shopify.app.toml` (`[[webhooks.subscriptions]]`, `topics = ["orders/create"]`, `uri = "https://our-backend/webhooks/shopify"`), or
  2. Via GraphQL `webhookSubscriptionCreate` mutation.
- Verify authenticity of each delivery via the HMAC header signed with the app's client secret.
- Webhook payloads include the customer phone/address (subject to the protected-data setting in point A).

Source: https://shopify.dev/docs/apps/build/webhooks/subscribe

### D. Rate limits

- Admin GraphQL API is cost-throttled (points per query, leaky bucket). At this
  project's volume (per-customer WhatsApp interactions) this is a non-issue, but batch
  jobs (e.g., re-syncing all orders) should be throttled.

---

## Alternative connection option (for awareness)

There is a second officially supported way for single-store integrations:
**admin-created custom app** (Shopify admin → Settings → Apps and sales channels →
Develop apps) which yields a **permanent** `shpat_` Admin API token — no 24h expiry, no
token-refresh code. Slightly simpler ops; slightly weaker security (long-lived secret).

The current Dev Dashboard + client credentials approach is the newer recommended
pattern and is the better choice — keep it. This alternative is documented only as a
fallback if the client's setup ever changes.
