# Shopify Order Mirror — Schema + Sync — Design

**Status:** Approved by owner (2026-08-10), conversational brainstorming.

**Sub-project 1 of N** in the larger "Tavas Bot — New Final Architecture" proposal (full
proposal recorded in this brainstorming session's transcript; decomposed into separate
sub-projects per its own scale). This sub-project covers schema + sync only. Later sub-projects
(not designed yet): the bot's read-path switch to the mirror, product/customer mirroring,
knowledge/embeddings/storage split, retention automation.

## Problem

The bot currently has no local copy of Shopify order data — every order question re-queries
Shopify live. This sub-project starts building a Postgres mirror of order data (populated from
data the app already receives, not extra Shopify calls) so a later sub-project can make the bot
read from it instead. This sub-project alone makes **no change to bot behavior** — it only gets
data flowing into two new tables in the background.

## Owner decisions recorded here

- **The eventual bot read-path switch will mean order-status replies are no longer re-fetched
  live from Shopify on every question** — a deliberate, explicit reversal of the "always
  re-fetch live" language in `CLAUDE.md` Critical Rule 3, accepted knowingly (mirror staleness
  risk, mitigated by the fact that the actual cancellation *mutation* path already re-fetches
  live and re-verifies ownership at tap-time regardless of this change — see Phase 5's
  `resolve_by_gid`). **Not implemented by this sub-project** — recorded here as the reason this
  data layer is being built, actioned (rule-text update + read-path change) in the follow-up
  sub-project once it exists to design.
- Sync scope for this sub-project: **orders only** (not products/customers yet).
- **Backfill included**: last 12 months of existing orders get pulled once, so the mirror isn't
  empty on day one.
- **No follow-up Shopify API call for real-time sync** — Shopify's webhook payload already
  contains everything needed (financial/fulfillment status, cancellation, totals, line items);
  the sync is pure parse-and-store of data already received, not an additional live fetch.

## Design

### Schema (new tables in `app/store/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS orders (
    gid                    text PRIMARY KEY,
    name                   text NOT NULL,
    order_number           integer,
    email                  text,
    phone                  text,
    shipping_phone         text,
    billing_phone          text,
    financial_status       text,
    fulfillment_status     text,
    cancelled_at           timestamptz,
    tags                   text[] NOT NULL DEFAULT '{}',
    payment_gateway_names  text[] NOT NULL DEFAULT '{}',
    total_amount           text,
    total_currency         text,
    customer_locale        text,
    order_created_at       timestamptz,
    synced_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders (phone);
CREATE INDEX IF NOT EXISTS idx_orders_shipping_phone ON orders (shipping_phone);
CREATE INDEX IF NOT EXISTS idx_orders_billing_phone ON orders (billing_phone);
CREATE INDEX IF NOT EXISTS idx_orders_name ON orders (name);

CREATE TABLE IF NOT EXISTS order_items (
    id              bigserial PRIMARY KEY,
    order_gid       text NOT NULL REFERENCES orders(gid) ON DELETE CASCADE,
    title           text NOT NULL,
    quantity        integer NOT NULL,
    variant_title   text,
    price_amount    text,
    price_currency  text
);
CREATE INDEX IF NOT EXISTS idx_order_items_order_gid ON order_items (order_gid);
```

Deliberately separate from the existing `order_mappings` table (which stays exactly as-is,
still the fast phone→order-gid index the CURRENT live-read path uses) — this sub-project is
purely additive, no existing table's shape or behavior changes.

### Shared upsert shape: reuse `app.shopify.models.Order`/`LineItem`

Both new data sources below (real-time webhook sync and the one-time backfill) end up needing
to write "one order plus its line items" into the mirror. Rather than inventing a second data
shape, both paths produce the **existing** `Order`/`LineItem` dataclasses (`app/shopify/models.py`
— already used by the live-read path and by this session's earlier order-item-details feature)
and hand them to one new store method:

```python
# app/store/base.py (IngestStore protocol)
async def upsert_order_mirror(self, order: Order) -> None: ...
```

Implemented in both `InMemoryIngestStore` and `PostgresIngestStore`. The Postgres
implementation: `INSERT ... ON CONFLICT (gid) DO UPDATE SET <every column> = EXCLUDED.<column>,
synced_at = now()` for the `orders` row, and for `order_items`, `DELETE FROM order_items WHERE
order_gid = $1` followed by a fresh bulk insert of the current item list — simpler and safer
than diffing individual items (handles an edited order's changed/removed items correctly, at
the cost of item `id` values not being stable across updates, which nothing depends on).

### Real-time sync: extend the existing webhook handler

`app/channels/shopify_webhook.py` currently hard-rejects every topic except `orders/create`:

```python
    if topic != "orders/create" or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})
```

This becomes an allow-list of both topics (`{"orders/create", "orders/updated"}`), with the
existing dedupe/mapping/push-eligibility logic for `orders/create` unchanged, and a new step
added for **both** topics: parse the payload into an `Order` (see below) and call
`c.ingest.upsert_order_mirror(order)` — inline, in the same request, before the response is
sent (matches the file's existing "ack fast" discipline: this is pure JSON parsing + a Postgres
write, no external API call, so it stays well inside Shopify's 5-second ack window).

New parser, `order_from_webhook_payload(payload: dict) -> Order | None` (new function in
`app/channels/shopify_orders.py`, next to the existing `parse_order_created`), extracting the
additional fields the existing `parse_order_created`/`IncomingOrder` doesn't capture — REST
payload fields already present in every Shopify order webhook: `fulfillment_status`,
`cancelled_at`, `total_price` + `currency`, `line_items` (each with `title`, `quantity`,
`price`, and `variant_title` — Shopify's REST line-item shape carries `variant_title` as a
direct field, unlike the GraphQL shape's nested `variant.title`), and `shipping_address.phone`
/ `billing_address.phone` kept separate (not pre-merged into one cascaded phone, unlike the
existing `IncomingOrder.phone_e164`) so the mirror can store all three independently, matching
`Order`'s existing three-phone shape. Returns `None` on the same malformed-payload conditions
`parse_order_created` already guards against (missing gid/name).

### Backfill: one-time script

New `backend/scripts/backfill_orders.py`, following the exact shape of the existing
`scripts/apply_schema.py` (reads `DATABASE_URL` from env, connects directly with
`statement_cache_size=0` for the Supabase pooler). Uses the **existing, already-built**
`ShopifyClient` + `TokenManager` (no new Shopify-side code) to page through
`orders(first: 50, after: $cursor, query: "created_at:>=<12-months-ago>")` via GraphQL — the
same `ORDER_FIELDS` query (already includes `lineItems`, extended earlier this session) —
calling `upsert_order_mirror` for each page of results until exhausted. Idempotent by
construction (same `ON CONFLICT` upsert the webhook path uses), so it's safe to re-run if
interrupted partway through.

### Explicitly out of scope (belongs to later sub-projects)

- No change to how `order_tracking`/`order_resolver` read data — the bot's live behavior is
  completely unaffected by this sub-project.
- No `CLAUDE.md` Critical Rule 3 text change yet — that happens when the read-path switch
  sub-project is designed, since that's when the rule's actual guarantee changes.
- No products/customers/inventory mirroring.
- No retention/cleanup automation for these new tables (follows the existing `retention_days`
  pattern in a later sub-project, once there's read traffic against this data to reason about).

## Testing

- `order_from_webhook_payload`: a realistic full order payload (with line items, both address
  phones, fulfillment/cancellation fields) parses into a correct `Order`; missing/malformed gid
  or name still returns `None` (matching `parse_order_created`'s existing contract); an order
  with zero line items parses to `line_items=()`; a line item missing `variant_title`/`price`
  parses those as `None` without raising.
- `upsert_order_mirror` (both store impls): inserting a new order creates one `orders` row and
  N `order_items` rows; upserting the same `gid` again with different data updates the
  `orders` row in place and replaces (not duplicates/appends) the `order_items` rows; deleting
  the parent `orders` row cascades to `order_items` (Postgres only, via the FK).
- Webhook handler: an `orders/updated` delivery (previously silently ignored) now reaches
  `upsert_order_mirror`; an `orders/create` delivery still does everything it does today
  (mapping/push-eligibility unchanged) **and** now also populates the mirror; an unrecognized
  topic is still ignored exactly as before.
- Backfill script: not unit-testable against real Shopify — cover the pagination/cursor-loop
  logic with a fake `ShopifyClient` proving it pages until exhaustion and calls
  `upsert_order_mirror` once per order.

## Global constraints

- Critical Rule 2 (LLM never mutates) — untouched; this sub-project adds no new Shopify write
  path.
- Full type hints; `mypy` strict clean; `ruff` clean; no bare `except`; no `print()`.
- No new secrets; the backfill script reuses existing Shopify credentials via the existing
  `ShopifyClient`/`TokenManager`, no new credential storage.
