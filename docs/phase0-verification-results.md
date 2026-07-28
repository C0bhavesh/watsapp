# Phase 0 — Live API Verification Results (2026-07-28)

> Run against the live store `thetavas.myshopify.com` on API version **2026-07**, using the
> client-credentials app. Read-only queries + schema-validation mutations on a NON-EXISTENT
> order id (nothing on the store was modified; no real order was tagged or cancelled).
> Customer personal data is masked in this doc. Credentials live only in the session
> scratchpad — never in this repo.

## Results

| # | Test | Result |
|---|---|---|
| 1 | Token grant (client credentials) | ✅ `shpat_…` issued, `expires_in 86399` (24h) — matches design. |
| 2 | `shop` + `currentAppInstallation.accessScopes` (2026-07) | ✅ Works. Store INR. Scopes: `read_orders`, `write_orders`, `read_all_orders`, `write_order_edits`, `read_returns` + many more (products, inventory, discounts…). ❌ **`read_customers`/`write_customers` NOT granted.** |
| 3 | Latest order full read (protected fields) | ✅ Order-level PII **returned**: `email`, `phone` (+91, already E.164), `shippingAddress` (incl. phone), `billingAddress.phone` → **protected-customer-data access effectively ON at order level**. ❌ `customer { … }` sub-object = ACCESS_DENIED (needs `read_customers`). |
| 4 | Order search `name:tavas3733` | ✅ Works, returns the exact order. Note: order names carry **no `#` prefix** (Shopflo/store format `tavasNNNN`). |
| 5 | Customer search by phone | ❌ ACCESS_DENIED — blocked by the missing `read_customers` scope. The fallback resolution path (phone→customer→orders) is unavailable until the scope is added. |
| 6 | `webhookSubscriptions` list | ✅ Empty — clean slate for this app (other apps' subscriptions are invisible by design). |
| 7 | `tagsAdd` schema+permission validation (bogus id) | ✅ Reached business logic: `userErrors: "Order does not exist"` → mutation callable, `write_orders` sufficient. |
| 8 | `orderCancel` schema+permission validation (bogus id) | ✅ Same: `orderCancelUserErrors: "Order does not exist"` → callable on 2026-07 with `(orderId, reason, restock)`. |
| 9 | Old version `2025-07` check | ⚠️ Confirmed dead: Shopify silently serves it as **2025-10** (`x-shopify-api-version: 2025-10` + deprecation-warning header). Validates the decision to pin **2026-07** explicitly. |

## The real order shape (Shopflo) — field mapping for the webhook handler

From the latest live order (`gid://shopify/Order/12187547894128`, `tavas3733`, masked):

```
sourceName:            "Created by Shopflo"        ← Shopflo confirmed as order creator
tags:                  ["COD", "COD pending", "HIGH_RISK", "Shopflo"]
paymentGatewayNames:   ["Cash on Delivery (COD)"]  ← primary COD marker
displayFinancialStatus PENDING (COD unpaid) · displayFulfillmentStatus UNFULFILLED
email:                 b•••a@gmail.com             ← present at ORDER level
phone:                 +9196•••••413               ← present at ORDER level, E.164 with +91
shippingAddress:       name / phone (+91…) / address1 / city / province / zip / country — all present
customAttributes:      Shopflo session ids, utm_* attribution, customer_type=NEW,
                       gateway="Cash on Delivery (COD)" (duplicate COD marker)
```

**Implications locked into the design:**
1. **COD detection** = `paymentGatewayNames` contains "Cash on Delivery" (customAttribute `gateway` as secondary).
2. **Phone extraction** = `order.phone` → fallback `shippingAddress.phone` → `billingAddress.phone`; already E.164 (+91) — normalization is nearly free.
3. **Ownership check works without `read_customers`** — compare the WhatsApp sender's number to the order-level phones.
4. **`tags: ["COD pending", …]` is strong evidence the current confirm/cancel tool is Shopflo's COD-confirmation flow** (it stamps `Shopflo` + `COD pending` at creation). Presumably flips on confirm/cancel — verify on an older confirmed order + still ask client Q7.
5. Order numbers have no `#` prefix — Q&A flow should accept `tavas3733`, `3733`, and `#…` variants and normalize.

## Actions arising

| Action | Owner | Status |
|---|---|---|
| Add `read_customers` scope to the app config + re-grant (enables fallback path + customer object) | us + client's Dev Dashboard access | OPEN |
| App-org ownership (Phase 0 item b) | — | ✅ **CLOSED by behavior**: Shopify only issues client-credentials tokens to own-org apps; the grant succeeds. |
| Protected-data toggle (item c) | — | ✅ order-level verified (item 3). Re-verify `customer` object after scope add. |
| 2026-07 re-test (item d) | — | ✅ DONE (tests 2–8). |
| Real order JSON inspected (item e) | — | ✅ DONE (shape above). |
| Rotate the client secret once creds move into Fernet-encrypted `app_config` (it was shared in plaintext during setup) | owner | RECOMMENDED |
