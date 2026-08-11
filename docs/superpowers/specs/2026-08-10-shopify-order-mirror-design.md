# Shopify Order Mirror — Schema + Sync — Design

**Status:** Approved by owner (2026-08-10), conversational brainstorming.

**Sub-project 1 of N** in the larger "Tavas Bot — New Final Architecture" proposal (full
proposal recorded in this brainstorming session's transcript; decomposed into separate
sub-projects per its own scale). This sub-project covers order + customer schema and sync only.
Later sub-projects (not designed yet): the bot's read-path switch to the mirror, product
mirroring, knowledge/embeddings/storage split, retention automation.

## Problem

The bot currently has no local copy of Shopify order or customer data — every order question
re-queries Shopify live. This sub-project starts building a Postgres mirror (populated from
data the app already receives, not extra Shopify calls) so a later sub-project can make the bot
read from it instead. This sub-project alone makes **no change to bot behavior** — it only gets
data flowing into three new tables in the background.

## Owner decisions recorded here

- **The eventual bot read-path switch will mean order-status replies are no longer re-fetched
  live from Shopify on every question** — a deliberate, explicit reversal of the "always
  re-fetch live" language in `CLAUDE.md` Critical Rule 3, accepted knowingly (mirror staleness
  risk, mitigated by the fact that the actual cancellation *mutation* path already re-fetches
  live and re-verifies ownership at tap-time regardless of this change — see Phase 5's
  `resolve_by_gid`). **Not implemented by this sub-project** — recorded here as the reason this
  data layer is being built, actioned (rule-text update + read-path change) in the follow-up
  sub-project once it exists to design.
- Sync scope for this sub-project: **orders (via `orders/create` + `orders/updated`), plus
  customer/address data — both embedded in each order's payload AND kept independently fresh
  via a `customers/update` subscription** (see the schema section's scope note for why not
  `customers/create` too).
- **Backfill included**: last 12 months of existing orders get pulled once, so the mirror isn't
  empty on day one.
- **No follow-up Shopify API call for real-time sync** — Shopify's webhook payload already
  contains everything needed (financial/fulfillment status, cancellation, totals, line items);
  the sync is pure parse-and-store of data already received, not an additional live fetch.

## Design

### Schema (new tables in `app/store/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS customers (
    gid             text PRIMARY KEY,
    first_name      text,
    last_name       text,
    email           text,
    phone           text,
    address_line1   text,
    address_line2   text,
    city            text,
    state           text,
    postal_code     text,
    country         text,
    synced_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers (phone);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers (email);

CREATE TABLE IF NOT EXISTS orders (
    gid                    text PRIMARY KEY,
    name                   text NOT NULL,
    order_number           integer,
    customer_gid           text REFERENCES customers(gid) ON DELETE SET NULL,
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
CREATE INDEX IF NOT EXISTS idx_orders_customer_gid ON orders (customer_gid);

CREATE TABLE IF NOT EXISTS order_items (
    id              bigserial PRIMARY KEY,
    order_gid       text NOT NULL REFERENCES orders(gid) ON DELETE CASCADE,
    title           text NOT NULL,
    sku             text,
    quantity        integer NOT NULL,
    variant_title   text,
    price_amount    text,
    price_currency  text
);
CREATE INDEX IF NOT EXISTS idx_order_items_order_gid ON order_items (order_gid);
CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items (sku);
```

`orders.customer_gid` is the relationship the owner asked for — one customer can have many
orders, each order links back to exactly one customer record (nullable, since a guest-checkout
order may carry no linkable customer id). Deliberately separate from the existing
`order_mappings` table (which stays exactly as-is, still the fast phone→order-gid index the
CURRENT live-read path uses) — this sub-project is purely additive, no existing table's shape
or behavior changes.

**Scope note on `customers`:** primarily populated from the customer/address data embedded in
each order's webhook payload — a customer only appears in this table once they've placed an
order the bot has seen, which is what matters for a WhatsApp order-support bot. On top of that,
this sub-project also subscribes to `customers/update` so a customer's profile (phone, address)
stays fresh even when it changes outside of an order — without it, a customer's row would only
ever refresh via order activity and could go stale if they update their contact info directly.
`customers/create` is deliberately still not subscribed: a brand-new customer with no order yet
has nothing for the bot to help with, so syncing them early has no payoff.

### Shared upsert shape: reuse `app.shopify.models.Order`/`LineItem`, add `Customer`

Both new data sources below (real-time webhook sync and the one-time backfill) end up needing
to write "one order, its line items, and its customer" into the mirror. Rather than inventing a
parallel data shape, both paths produce the **existing** `Order`/`LineItem` dataclasses
(`app/shopify/models.py` — already used by the live-read path and by this session's earlier
order-item-details feature), extended with one new dataclass and one new field:

```python
@dataclass(frozen=True)
class Customer:
    gid: str
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
```

`Order` gains `customer: Customer | None = None`. Both are handed to two new store methods
(the second reused by the first, so there is exactly one place that writes a `customers` row):

```python
# app/store/base.py (IngestStore protocol)
async def upsert_customer(self, customer: Customer) -> None: ...
async def upsert_order_mirror(self, order: Order) -> None: ...
```

Implemented in both `InMemoryIngestStore` and `PostgresIngestStore`. `upsert_customer`:
`INSERT ... ON CONFLICT (gid) DO UPDATE SET <every column> = EXCLUDED.<column>, synced_at =
now()` against the `customers` table. `upsert_order_mirror` runs one transaction: calls
`upsert_customer(order.customer)` first when `order.customer is not None` (skipped entirely
otherwise), then the same upsert pattern for the `orders` row (with `customer_gid` set from
`order.customer.gid` when present), then for `order_items`, `DELETE FROM order_items WHERE
order_gid = $1` followed by a fresh bulk insert of the current item list — simpler and safer
than diffing individual items (handles an edited order's changed/removed items correctly, at
the cost of item `id` values not being stable across updates, which nothing depends on).

### Subscription management: generalize from one hardcoded topic to a list

`app/shopify/subscriptions.py`'s `ensure_subscription` is currently hardcoded to a single topic
(`ORDERS_CREATE` is baked directly into `_LIST_QUERY`/`_CREATE_MUTATION`) — Shopify webhook
subscriptions are one-subscription-per-topic, so adding `ORDERS_UPDATED` and
`CUSTOMERS_UPDATE` means this needs to loop over a list of required topics, ensuring each has
its own correctly-configured subscription (same callback URL + API version check as today, per
topic), rather than special-casing a single one. This is a real, necessary change surfaced by
reading the actual code — not something the original proposal anticipated. The self-healing
behavior (create if missing, update if the callback URL or API version drifted) stays the same,
just applied per-topic in a loop instead of once.

### Real-time sync: extend the existing webhook handler

`app/channels/shopify_webhook.py` currently hard-rejects every topic except `orders/create`:

```python
    if topic != "orders/create" or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})
```

This becomes an allow-list of three topics (`{"orders/create", "orders/updated",
"customers/update"}`). For the two order topics: existing dedupe/mapping/push-eligibility logic
for `orders/create` unchanged, and a new step added for **both** order topics: parse the
payload into an `Order` (see below) and call `c.ingest.upsert_order_mirror(order)` — inline, in
the same request, before the response is sent (matches the file's existing "ack fast"
discipline: this is pure JSON parsing + a Postgres write, no external API call, so it stays
well inside Shopify's 5-second ack window). For `customers/update`: a new, simpler path — parse
the payload (a plain Shopify Customer resource, not nested in an order) into a `Customer` via a
new `customer_from_webhook_payload(payload: dict) -> Customer | None` parser, and call
`c.ingest.upsert_customer(customer)` directly — no order involved, no mapping/push-eligibility
logic applies to this topic at all.

New parser, `order_from_webhook_payload(payload: dict) -> Order | None` (new function in
`app/channels/shopify_orders.py`, next to the existing `parse_order_created`), extracting the
additional fields the existing `parse_order_created`/`IncomingOrder` doesn't capture — REST
payload fields already present in every Shopify order webhook: `fulfillment_status`,
`cancelled_at`, `total_price` + `currency`, `line_items` (each with `title`, `sku`, `quantity`,
`price`, and `variant_title` — Shopify's REST line-item shape carries `variant_title`/`sku` as
direct fields, unlike the GraphQL shape's nested `variant.title`), and `shipping_address.phone`
/ `billing_address.phone` kept separate (not pre-merged into one cascaded phone, unlike the
existing `IncomingOrder.phone_e164`) so the mirror can store all three independently, matching
`Order`'s existing three-phone shape. Returns `None` on the same malformed-payload conditions
`parse_order_created` already guards against (missing gid/name).

Also builds the `Customer` from the payload's `customer` sub-object (`id` → gid via the same
`admin_graphql_api_id`-style construction as the order itself, `first_name`, `last_name`,
`email`, `phone`) plus the order's `shipping_address` for the structured address fields
(`address1`, `address2`, `city`, `province`→`state`, `zip`→`postal_code`, `country`) — Shopify
always includes the shipping address directly on the order payload, so no extra fetch is
needed. `customer` is `None` when the payload has no `customer` object at all (e.g. some
guest-checkout shapes) — `upsert_order_mirror` treats that as "no customer to link," not an
error.

### Backfill: one-time script

New `backend/scripts/backfill_orders.py`, following the exact shape of the existing
`scripts/apply_schema.py` (reads `DATABASE_URL` from env, connects directly with
`statement_cache_size=0` for the Supabase pooler). Uses the **existing, already-built**
`ShopifyClient` + `TokenManager` (no new Shopify-side code) to page through
`orders(first: 50, after: $cursor, query: "created_at:>=<12-months-ago>")` via GraphQL —
`ORDER_FIELDS` (`app/shopify/client.py`) needs one more extension here, alongside the
`lineItems` addition from earlier this session: a `customer { id firstName lastName email }`
sub-selection and the full `shippingAddress { address1 address2 city province zip country
phone }` (currently only `phone` is selected from it) and a `sku` field on each `lineItems`
node — the same fields the webhook-path parser reads, so backfilled orders and
webhook-synced orders populate identical rows. Calls `upsert_order_mirror` for each order
across all pages until exhausted. Idempotent by construction (same `ON CONFLICT` upsert the
webhook path uses), so it's safe to re-run if interrupted partway through.

### Explicitly out of scope (belongs to later sub-projects)

- No change to how `order_tracking`/`order_resolver` read data — the bot's live behavior is
  completely unaffected by this sub-project.
- No `CLAUDE.md` Critical Rule 3 text change yet — that happens when the read-path switch
  sub-project is designed, since that's when the rule's actual guarantee changes.
- No products/inventory mirroring, and no `customers/create` webhook subscription (see the
  schema section's scope note for why `customers/update` alone is enough).
- No retention/cleanup automation for these new tables (follows the existing `retention_days`
  pattern in a later sub-project, once there's read traffic against this data to reason about).

## Testing

- `order_from_webhook_payload`: a realistic full order payload (with line items, both address
  phones, fulfillment/cancellation fields, and a `customer` object) parses into a correct
  `Order` with a populated `Customer`; missing/malformed gid or name still returns `None`
  (matching `parse_order_created`'s existing contract); an order with zero line items parses to
  `line_items=()`; a line item missing `variant_title`/`price`/`sku` parses those as `None`
  without raising; a payload with no `customer` object at all parses to `customer=None` without
  raising.
- `upsert_order_mirror` (both store impls): inserting a new order with a customer creates one
  `customers` row, one `orders` row (with `customer_gid` set), and N `order_items` rows;
  inserting an order with `customer=None` creates the order with a `NULL` `customer_gid` and no
  new customer row; upserting the same order `gid` again with different data updates the
  `orders` row in place and replaces (not duplicates/appends) the `order_items` rows;
  re-upserting with the same customer `gid` updates that customer's row in place (not a
  duplicate); deleting the parent `orders` row cascades to `order_items` but leaves the
  `customers` row intact (Postgres only, via the FKs).
- `customer_from_webhook_payload`: a realistic Shopify Customer payload parses into a correct
  `Customer`; missing/malformed `id` returns `None`; a payload with no `default_address` parses
  the address fields as `None` without raising.
- `upsert_customer` (both store impls): inserting a new customer creates one row; re-upserting
  the same `gid` with different data updates it in place, not a duplicate.
- Webhook handler: an `orders/updated` delivery (previously silently ignored) now reaches
  `upsert_order_mirror`; an `orders/create` delivery still does everything it does today
  (mapping/push-eligibility unchanged) **and** now also populates the mirror; a
  `customers/update` delivery reaches `upsert_customer` directly, with no mapping/push logic
  invoked; an unrecognized topic is still ignored exactly as before.
- `ensure_subscription`: with the topic list generalization, a fake Shopify client proving each
  of the three topics (`ORDERS_CREATE`, `ORDERS_UPDATED`, `CUSTOMERS_UPDATE`) gets its own
  independent create-if-missing/update-if-drifted check — not just the first one in the list.
- Backfill script: not unit-testable against real Shopify — cover the pagination/cursor-loop
  logic with a fake `ShopifyClient` proving it pages until exhaustion and calls
  `upsert_order_mirror` once per order.

## Global constraints

- Critical Rule 2 (LLM never mutates) — untouched; this sub-project adds no new Shopify write
  path.
- Full type hints; `mypy` strict clean; `ruff` clean; no bare `except`; no `print()`.
- No new secrets; the backfill script reuses existing Shopify credentials via the existing
  `ShopifyClient`/`TokenManager`, no new credential storage.
