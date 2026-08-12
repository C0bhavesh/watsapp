# Database-First Order Reads for Chat Q&A — Design

**Status:** Approved by owner (2026-08-12), via conversational brainstorming (not the visual companion).

## Problem

Every order lookup the bot performs — both answering a customer's chat question ("where's my
order?") and the safety re-check right before a Confirm/Cancel tap actually mutates an order —
currently goes live to Shopify's GraphQL API, on every single customer message. This was
correct and necessary before the order-mirror sub-project (2026-08-10/11) existed: there was
nowhere else to read from.

Now that `customers`/`orders`/`order_items` exist, are backfilled with 12 months of history, and
are kept in sync in near-real-time by the `orders/create`/`orders/updated`/`customers/update`
webhooks (fixed and confirmed live 2026-08-12), the owner wants informational Q&A to read from
our own database instead of hitting Shopify on every message — faster replies, no Shopify API
load for read-only questions.

The owner explicitly confirmed (via brainstorming) that this must NOT extend to the pre-mutation
safety re-fetch: Critical Rule 3 ("Always re-fetch order state from Shopify before acting — never
trust message/LLM claims... No exceptions") stays binding for the Confirm/Cancel button-tap path.

## What already exists (verified by reading the code, not assumed)

- `app/core/order_resolver.py` defines `OrderSource`, a `Protocol` with exactly three methods
  (`get_order`, `find_order_by_name`, `find_customer_orders_by_phone`) — this is already the
  seam `resolve_by_phone`/`resolve_by_order_name`/`resolve_by_gid` depend on, not a concrete
  Shopify type. `ShopifyClient` satisfies it structurally today.
- `resolve_by_phone(shopify, ingest, wa_id)` and `resolve_by_order_name(shopify, wa_id, raw_name)`
  are called from `app/core/conversation.py` — the chat Q&A pipeline. Both take an `OrderSource`
  as their first argument.
- `resolve_by_gid(shopify, wa_id, gid)` is called from `app/core/order_actions.py` — the
  deterministic Confirm/Cancel button-tap dispatcher, called BEFORE every `tagsAdd`/`orderCancel`
  mutation. Also takes an `OrderSource`.
- `app/shopify/models.py` defines the `Order`/`Customer`/`LineItem`/`Money` dataclasses that
  every caller downstream (agents, reply formatting, `AuthorizedOrder`) already consumes —
  provider-agnostic, no Shopify-specific types leak past `order_resolver.py`.
- `app/store/postgres.py::upsert_order_mirror`/`upsert_customer` already write the `orders`,
  `order_items`, and `customers` tables (columns: `orders.{gid, name, order_number, customer_gid,
  email, phone, shipping_phone, billing_phone, financial_status, fulfillment_status,
  cancelled_at, tags, payment_gateway_names, total_amount, total_currency, customer_locale,
  updated_at}`, `order_items.{order_gid, title, sku, quantity, variant_title, price_amount,
  price_currency}`, `customers.{gid, first_name, last_name, email, phone, address_line1/2,
  city, state, postal_code, country, updated_at}`) — this design only needs to read them back.

None of this is wired together — that's the entire gap this feature closes.

## Design

### Architecture

A new adapter, `MirrorOrderSource`, implements the existing `OrderSource` `Protocol`. It is
constructed with an `IngestStore` (database) and an `OrderSource` (the real Shopify client, used
only as a fallback). Each of its three methods:

1. Tries the corresponding database read first.
2. Returns the result immediately on a hit.
3. On a miss (`None` / empty list) OR any exception from the database read, falls through to the
   wrapped `OrderSource`'s live Shopify call.

Callers cannot tell which source actually answered — `MirrorOrderSource` is a drop-in
replacement for `ShopifyClient` wherever an `OrderSource` is expected.

### Wiring — the ONLY behavioral change

`app/core/conversation.py` constructs `MirrorOrderSource(c.ingest, c.shopify)` once per turn and
passes it (instead of `c.shopify` directly) into `resolve_by_phone` and `resolve_by_order_name`.

`app/core/order_actions.py` is **not modified**. `resolve_by_gid` keeps receiving `c.shopify`
directly, unchanged, at every call site. This is the enforcement point for the "Q&A only, never
the mutation path" boundary — it is visible as a one-line difference at each call site, not a
branch buried inside `order_resolver.py` (which itself is also not modified).

### New `IngestStore` methods

Added to the `IngestStore` `Protocol` (`app/store/base.py`) and implemented in both
`PostgresIngestStore` and the in-memory store:

- `get_mirrored_order(gid: str) -> Order | None` — one order by gid.
- `find_mirrored_order_by_name(raw_name: str) -> Order | None` — one order by its `name` column.
  Takes the RAW customer-typed value, exactly like `ShopifyClient.find_order_by_name` does today
  (its `OrderSource.find_order_by_name` signature is `raw_name: str` — normalization is the
  callee's job, not the caller's). Calls the existing `app.shopify.models.normalize_order_name`
  itself before querying, so both sources normalize identically (`"tavas3846"`).
- `find_mirrored_orders_by_phone(phone_e164: str) -> list[Order]` — every order where
  `phone_e164` matches `orders.phone` OR `orders.shipping_phone` OR `orders.billing_phone`
  (mirrors `AuthorizedOrder`'s own ownership check, which accepts a match against any of the
  three).

Each reconstructs a full `Order` — including its `customer` (via `orders.customer_gid` →
`customers` LEFT JOIN) and `line_items` (via `order_items` keyed on `order_gid`) — into the exact
same dataclasses `ShopifyClient.get_order`/`find_order_by_name`/`find_customer_orders_by_phone`
already produce. `Money` is reconstructed from the `_amount`/`_currency` column pairs, `tags` and
`payment_gateway_names` from their stored array columns.

### Error handling

Any exception raised while reading from the database (connection failure, unexpected row shape)
is caught inside `MirrorOrderSource` and treated identically to a miss: fall through to Shopify.
Nothing propagates to the caller. This matches the existing degrade-gracefully posture used
throughout the mirror-sync code (`_mirror_order`/`_mirror_customer` in
`app/channels/shopify_webhook.py`) and `resolve_by_phone`'s own `ShopifyError` handling — a
database or Shopify hiccup costs one extra round-trip or a stale-but-safe answer, never a broken
turn.

### Out of scope (YAGNI)

- `resolve_by_gid` / the Confirm/Cancel mutation path — explicitly excluded per the owner's
  decision; stays 100% live Shopify, no `MirrorOrderSource` involvement whatsoever.
- Any change to `order_resolver.py` itself, `AuthorizedOrder`, or the ownership-check invariant —
  all already correct and unaffected; `MirrorOrderSource` only changes what the `shopify`
  parameter to `resolve_by_phone`/`resolve_by_order_name` actually points at.
- Exposing data freshness/staleness to the customer (e.g. "as of X minutes ago") — the mirror is
  treated as authoritative for Q&A; no new copy.
- A cache/TTL layer, or wrapping every `OrderSource` consumer globally (rejected approach —
  see the design conversation) — this is a targeted swap at the two Q&A call sites only.
- Any change to how the mirror itself is populated (webhooks, backfill) — this feature is
  entirely a new read path over already-existing, already-synced data.

## Testing

- New `IngestStore` mirror-read method tests (Postgres-gated + in-memory): hit, miss, an order
  with no customer (`customer_gid IS NULL`), an order with no line items, phone match against
  each of the three phone columns independently.
- `MirrorOrderSource` unit tests: (a) a database hit never calls the wrapped Shopify source
  (assert on a fake's call count), (b) a database miss calls Shopify and returns its result,
  (c) a database read that raises still falls through to Shopify rather than propagating.
- Integration test at the `conversation.py` level confirming the Q&A pipeline's `OrderSource` is
  a `MirrorOrderSource`, not the raw Shopify client.
- Confirm the existing `order_actions.py` / `resolve_by_gid` test suite is untouched and still
  proves it only ever receives the real Shopify client — this is the regression guard for the
  "mutation path stays live" invariant.

## Global constraints (already binding, restated for this feature)

- Critical Rule 2 (LLM never mutates) — untouched; this feature adds no new Shopify write paths.
- Critical Rule 3 (ownership check before revealing anything, always re-fetch live before ANY
  mutation) — enforced entirely by `resolve_by_gid` remaining wired to the real Shopify client;
  this feature does not touch, weaken, or duplicate that path.
- No new secrets, no new admin-panel config, no schema/migration changes (the mirror tables
  already exist and are already populated).
