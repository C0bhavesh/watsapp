# Client Shopify Theme Exploration — E:\shopify_bhavesh\personal_june_theme

> Raw exploration report (2026-07-28). Store: thetavas.myshopify.com.

## 1. Theme structure & base theme

Standard Shopify 2.0 theme: `assets/` (50), `blocks/` (1 AI-generated block), `config/` (2),
`layout/` (2), `locales/` (en only), `sections/` (60, incl. 11 custom `tavas-*.liquid`),
`snippets/` (69), `templates/` (22, incl. 6 product template variants: product / 2pc / 3pc /
co-ord / new / testing).

Base theme (`config\settings_schema.json:1-8`): **"Jhango Theme V1"** v1.0.0 by Jhango
(jhango.com), overlaid with a "Tavas redesign" (`tavas-header`, `tavas-footer`,
`tavas-hero-arch`, `tavas-collection-grid`, etc. + `assets/tavas.css`, `assets/tavas.js`).

Non-theme folders in the same directory (not uploaded to Shopify):
`claude_update design\` (redesign mockups) and a stray `homepage-ux-validation-checklist.md`.

## 2. Third-party app / script traces (the important part)

**No WhatsApp / order-confirmation / COD-confirmation app found in the theme.** Expected —
that class of software runs server-side off order webhooks + Admin API and leaves no theme
footprint. Zero matches for: wa.me, a2ship, aftership, interakt, aisensy, gupshup, wati,
limechat, bitespeed, superlemon, zoko, delightchat, shiprocket, nimbuspost, clickpost,
gokwik, razorpay, shipway, pickrr, webhook, order.tags. (The only "whatsapp" hits are CDN
filenames of size-chart images uploaded from WhatsApp in `templates\product.2pc.json:97` and
`product.3pc.json:106`.)

What DOES exist:

### a) Shopflo (checkout replacement) — LIVE, loaded globally ⭐
`layout\theme.liquid:43-44`:
```liquid
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js"></script>
<script src='https://bridge.shopflo.com/js/shopflo.bundle.js' async></script>
```
Shopflo is an Indian one-click checkout that owns the COD/prepaid flow and the post-purchase
thank-you page. **Consequences for us:**
- The checkout that produces the order is Shopflo's — verify what it writes onto the Shopify
  order (COD vs prepaid markers, phone number placement, `note_attributes`, tags) by
  inspecting a real order JSON before designing the webhook handler.
- Shopflo's product suite includes COD-confirmation / WhatsApp engagement features — **it is a
  strong candidate for being the "current 3rd-party tool"** the client mentioned. Ask.
- Orders created through Shopflo still land in Shopify as normal orders, so the
  `orders/create` webhook approach stands.

### b) Cashfree One-Click Checkout — ORPHANED dead code
`snippets\cashfree.liquid` (317 lines): full checkout hijacker (MutationObserver on
checkout/buy-now buttons → loads `shopify-checkout.cashfree.com/bundle.js`). Also sets
`script.id = "zecpe-theme-script"` — a Zecpe remnant. **Never rendered** (no
`{% render 'cashfree' %}` anywhere). Evidence of checkout-app churn: Zecpe → Cashfree →
now Shopflo. Safe to delete (cleanup opportunity, not a blocker).

### c) Judge.me reviews — active app embed + app blocks (settings_data.json:76 + all 6 product templates).

### d) Loox — residual metafield-driven rating badge (`snippets\product-rating-badge.liquid:4`), app embed not present.

### e) Removed obfuscated script — notable
`assets\custom.js` is a single line: `/* custom.js cleared - obfuscated eval() code removed */`.
Someone previously injected obfuscated `eval()` code and it was stripped. `custom.js` is still
loaded **twice** (`snippets/styles-scripts.liquid:92` and `layout/theme.liquid:75`). If the
WhatsApp confirmation vendor ever had a storefront pixel, it lived here — the original content
is gone from this copy. **Check the live theme on thetavas.myshopify.com for the un-cleared
version.**

## 3. External domains called from the theme

| Domain | Location | Purpose |
|---|---|---|
| bridge.shopflo.com | layout\theme.liquid:44 | Shopflo checkout (active) |
| cdnjs.cloudflare.com (crypto-js 4.1.1) | layout\theme.liquid:43 | Shopflo dependency |
| fonts.googleapis.com / fonts.gstatic.com | layout\theme.liquid:38-40 | Fonts |
| shopify-checkout.cashfree.com, sdk.cashfree.com | snippets\cashfree.liquid | dead code |
| jhango.com family | section schemas | theme-author help links |

## 4. Order-status / tracking page customizations

**None.** No `templates/customers/` folder at all, no checkout.liquid, no order-status
extension. FAQ "How can I track my order?" still has a placeholder dummy phone
(`sections\faq.liquid:374-375`). Order status page is fully owned by Shopify checkout +
Shopflo.

## 5. `confirmed` / `cancelled` tag usage in storefront

**None.** Confirm/cancel tagging is invisible to the storefront — purely a backend concern.

## Practical takeaways for our build

1. Nothing in the theme needs changing to swap out the WhatsApp vendor — it's entirely
   server-side. Our work = Shopify Admin API + orders/create webhook + Meta Cloud API.
2. **Shopflo is the real constraint:** inspect a live order's JSON (note_attributes, tags,
   payment gateway names, phone fields) before finalizing the webhook handler, and ask the
   client whether Shopflo is the current confirm/cancel message sender.
3. Customer phone will come from `order.customer.phone` / `order.shipping_address.phone`
   (the theme itself never collects phone outside the contact form).
4. Cleanup opportunities (non-blocking): delete dead `cashfree.liquid`; `custom.js` loaded
   twice; FAQ placeholder phone number.
