# Order Item Details + Attractive Formatting — Design

**Status:** Approved by owner (2026-08-10), conversational brainstorming.

## Problem

Two related gaps in `order_tracking`'s replies, found via live testing:

1. **No product-level detail.** The bot can only state order-level status (payment,
   fulfillment, cancellation) — it has no product name, price, or color/variant data at all.
   The Shopify query never fetches line items, and the `Order` model has no field for them.
2. **Plain, unstructured replies.** Order-detail replies read as one paragraph of prose, with
   no visual structure, and financial status "Pending" reads as alarming for a COD order (where
   Pending is the normal state until delivery) even though `Order.is_cod()` already exists and
   is unused.

## Owner decision (supersedes the earlier Q5 client decision)

The earlier recorded decision (`client-decisions-all.md`, D-2/Q5) said the bot may reveal order
status plus order ID and email, but **"items/amounts/tracking stay hidden."** The owner has now
explicitly reversed this: the bot should reveal product name, price, and color/variant per
order. This is recorded here as the update to that decision — `client-decisions-all.md` and
`_pipeline_status.md` should be updated by `doc-updater` after this ships to reflect the
reversal, not just the new build.

## Design

### Data layer

- `app/shopify/client.py`: extend `ORDER_FIELDS` with
  `lineItems(first: 50) { edges { node { title quantity variant { title }
  originalUnitPriceSet { shopMoney { amount currencyCode } } } } }`. `first: 50` is a query-time
  cap far above any realistic order size (the owner chose "show all, no display-time cap" — this
  is a technical ceiling, not the display limit).
- `app/shopify/models.py`: new `LineItem` dataclass (`title: str`, `quantity: int`,
  `variant_title: str | None`, `price: Money | None`); `Order` gains
  `line_items: tuple[LineItem, ...]`.
- `_order_from_node` (client.py) parses the new `lineItems` edges into `LineItem` tuples.

### Disclosure control

- `REVEAL_ALLOWED` (`app/admin/controls.py`) gains `"items"` — the fourth allowed value.
  `reveal_fields`'s `Field(max_length=3)` becomes `max_length=4`, and its default becomes
  `["order_number", "email", "status", "items"]` (items enabled by default, per the owner's
  explicit request, but still admin-toggleable later without a redeploy — same config-driven
  pattern as every other disclosure control here).
- `DEFAULT_REVEAL_FIELDS` (`app/agents/base.py`) updated to match, kept in sync by the existing
  test that pins the two together.

### Rendering + COD framing

- `order_tracking.py`'s `_order_line`: when `"items"` is in `reveal_fields`, render each line
  item as `- *{title}* ({variant_title}) — {formatted price}` (variant parens omitted if
  `variant_title` is `None`); when `"items"` is withheld, item lines are simply not rendered
  (same "omit entirely, don't just say don't mention" pattern the existing fields already use).
- Money formatting: a small local helper in `order_tracking.py` — `₹{amount}` for `currency ==
  "INR"`, else `f"{amount} {currency}"`; strips a trailing `.00` for a cleaner customer-facing
  number (`"999.00"` → `"₹999"`, not `"₹999.00"`).
- COD framing: `_order_line` appends `" (Cash on Delivery)"` to the financial-status text
  whenever `order.order.is_cod()` is true (using the existing, previously-unused method) — the
  prompt then instructs the model that Pending + Cash on Delivery is normal and should be
  explained as such, not flagged as a problem.

### Reply formatting

`order_tracking`'s system prompt gains explicit formatting guidance and a concrete example
(WhatsApp bold via `*text*` — already correctly produced by `strip_markdown` converting the
model's standard `**text**` output, no pipeline change needed): a short warm greeting line, bold
key fields (order ID, status), a moderate emoji touch, and items listed clearly. This is scoped
to `order_tracking` specifically (not a global `PERSONALITY` change), since the request was
specifically about order-detail replies.

## Out of scope (YAGNI)

- No display-time cap on line items (owner's explicit choice — show all).
- No change to `product_search`/`recommendations` formatting (separate agents, not requested).
- No change to what counts as "status" (financial/fulfillment/cancellation) — only adding the
  new "items" category, not altering the existing three.

## Testing

- Unit tests for `_line_items_from_node` parsing (multiple items, zero items, missing variant,
  missing price).
- Unit tests for the money-formatting helper (INR strips `.00`, non-INR shows currency code,
  non-`.00` amounts left as-is).
- Unit tests for `_order_line` with `"items"` in/out of `reveal_fields`, and with/without
  `is_cod()` true.
- Existing `test_default_reveal_fields_tracks_the_admin_allowed_set` continues to pin
  `DEFAULT_REVEAL_FIELDS == REVEAL_ALLOWED == tuple(AdminControls().reveal_fields)` — extending
  it is how the sync is verified, not a new separate test.
