# Shopify Order Mirror (Schema + Sync) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Postgres mirror of Shopify order + customer data (three new tables: `customers`,
`orders`, `order_items`), populated by extending the existing Shopify webhook receiver to handle
`orders/updated` and `customers/update` (in addition to the already-handled `orders/create`) and
by a one-time backfill script — with zero change to the bot's current live-read behavior.

**Architecture:** Two new `IngestStore` methods (`upsert_customer`, `upsert_order_mirror`) sit
behind the existing `Order`/`Customer`/`LineItem` dataclasses, fed by two new REST-webhook-payload
parsers and, for the one-time backfill, a new paginated GraphQL method on `ShopifyClient`. The
existing single-topic webhook subscription manager becomes multi-topic. Nothing in
`order_resolver.py`/`order_tracking.py`/the bot's live-read path changes in this plan.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, asyncpg (Postgres), existing `ShopifyClient`/
`TokenManager` (no new Shopify-side auth code).

## Global Constraints

- Critical Rule 2 (LLM never mutates) — untouched; no new Shopify write path anywhere in this plan.
- Full type hints on every function signature; `mypy` strict clean; `ruff` clean; no bare `except`;
  no `print()` (use `logging` where needed).
- No new secrets; the backfill script reuses the existing `get_container()` wiring (Shopify creds
  are already decrypted via the existing `SecretVault`/`ConfigService` chain).
- Design spec: `docs/superpowers/specs/2026-08-10-shopify-order-mirror-design.md`.
- This sub-project makes **no behavior change** to how the bot answers customer questions — every
  task here is additive infrastructure only.

---

### Task 1: Schema + `Customer` dataclass + `Order.customer` field

**Files:**
- Modify: `backend/app/store/schema.sql`
- Modify: `backend/app/shopify/models.py`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Customer` dataclass (`app/shopify/models.py`) with fields `gid: str`,
  `first_name: str | None`, `last_name: str | None`, `email: str | None`, `phone: str | None`,
  `address_line1: str | None`, `address_line2: str | None`, `city: str | None`,
  `state: str | None`, `postal_code: str | None`, `country: str | None`. `Order` gains
  `customer: Customer | None = None` (new last field — `Order`'s existing fields, including the
  already-present `line_items: tuple[LineItem, ...] = ()`, are unchanged; `customer` is added
  after `line_items` so both defaulted fields sit at the end, valid dataclass ordering). New tables
  `customers`, `orders`, `order_items` exist in the schema (used starting Task 2).

- [ ] **Step 1: Read current state of both files**

Read `backend/app/store/schema.sql` in full and `backend/app/shopify/models.py` in full. Confirm
`Order`'s current last field is `line_items: tuple[LineItem, ...] = ()` before editing (this was
added earlier the same day this plan was written — if it's missing, stop and flag the drift rather
than guessing).

- [ ] **Step 2: Add the three new tables to `schema.sql`**

Append to the end of `backend/app/store/schema.sql` (after the existing `knowledge_overrides`
table):

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

- [ ] **Step 3: Add `Customer` and extend `Order` in `models.py`**

Find the `LineItem` dataclass in `backend/app/shopify/models.py` (added earlier the same day).
Add this new dataclass directly after it:

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

Find `Order`'s field list, ending in `line_items: tuple[LineItem, ...] = ()`. Add one more field
directly after it:

```python
    line_items: tuple[LineItem, ...] = ()
    customer: Customer | None = None
```

- [ ] **Step 4: Write the failing test**

In `backend/tests/test_models.py`, add:

```python
from app.shopify.models import Customer


def test_order_customer_defaults_to_none() -> None:
    assert make_order().customer is None


def test_order_accepts_a_customer() -> None:
    cust = Customer(
        gid="gid://shopify/Customer/1", first_name="Suman", last_name="Bayala",
        email="c@example.com", phone="+919999999999", address_line1="12 MG Road",
        address_line2=None, city="Bengaluru", state="Karnataka", postal_code="560001",
        country="India",
    )
    order = make_order(customer=cust)
    assert order.customer is cust
    assert order.customer.city == "Bengaluru"
```

(Both use the file's existing `make_order(**overrides)` helper — no changes needed to it, since
it builds `Order(**base)` and `customer` simply isn't in `base`, so it defaults to `None` unless
overridden.)

- [ ] **Step 5: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_models.py -v`
Expected: FAIL — `ImportError` or `TypeError: Order.__init__() got an unexpected keyword argument
'customer'` (whichever surfaces first, since `Customer` doesn't exist yet).

- [ ] **Step 6: Confirm the tests pass**

Run: `cd backend && python -m pytest tests/test_models.py -v`
Expected: PASS, all tests including the two new ones.

- [ ] **Step 7: Run ruff and mypy on the touched files**

Run: `cd backend && python -m ruff check app/shopify/models.py tests/test_models.py`
Expected: All checks passed!

Run: `cd backend && python -m mypy app/shopify/models.py`
Expected: Success, no issues found.

- [ ] **Step 8: Commit**

```bash
git add backend/app/store/schema.sql backend/app/shopify/models.py backend/tests/test_models.py
git commit -m "feat(shopify): add customers/orders/order_items schema and Customer/Order.customer types"
```

---

### Task 2: `upsert_customer` + `upsert_order_mirror` store methods

**Files:**
- Modify: `backend/app/store/base.py`
- Modify: `backend/app/store/memory.py`
- Modify: `backend/app/store/postgres.py`
- Test: `backend/tests/store/test_order_mirror.py` (new file)

**Interfaces:**
- Consumes: `Customer`, `Order`, `LineItem` (Task 1, `app/shopify/models.py`).
- Produces: `IngestStore.upsert_customer(customer: Customer) -> None` and
  `IngestStore.upsert_order_mirror(order: Order) -> None`, implemented by both
  `InMemoryIngestStore` and `PostgresIngestStore`. Later tasks (webhook wiring, backfill script)
  call these two methods and nothing else on the store for this feature.

- [ ] **Step 1: Read current state of all three files**

Read `backend/app/store/base.py`, `backend/app/store/memory.py`, and
`backend/app/store/postgres.py` in full. Note `PostgresIngestStore`'s existing
`ingest_order_created` for the `async with self._pool.acquire() as conn: async with
conn.transaction():` pattern, and `InMemoryIngestStore.__init__`'s existing dict-based state
(`self.mappings: dict[str, MappingUpsert]`, etc.) — both new implementations must match these
established conventions.

- [ ] **Step 2: Add the two methods to the `IngestStore` Protocol**

In `backend/app/store/base.py`, add this import at the top (alongside the existing imports):

```python
from app.shopify.models import Customer, Order
```

Find the `IngestStore` Protocol's last method, `orders_awaiting_cancel_reconcile`. Add directly
after it:

```python
    # --- Order mirror sync (Shopify webhook -> Postgres, no live read-path change yet) ---

    async def upsert_customer(self, customer: Customer) -> None: ...

    async def upsert_order_mirror(self, order: Order) -> None: ...
```

- [ ] **Step 3: Write the failing tests**

Create `backend/tests/store/test_order_mirror.py`:

```python
from app.shopify.models import Customer, LineItem, Money, Order
from app.store.memory import InMemoryIngestStore


def _customer(gid: str = "gid://shopify/Customer/1", **overrides: object) -> Customer:
    base = dict(
        gid=gid, first_name="Suman", last_name="Bayala", email="c@example.com",
        phone="+919999999999", address_line1="12 MG Road", address_line2=None,
        city="Bengaluru", state="Karnataka", postal_code="560001", country="India",
    )
    base.update(overrides)
    return Customer(**base)  # type: ignore[arg-type]


def _order(gid: str = "gid://shopify/Order/1", **overrides: object) -> Order:
    base = dict(
        gid=gid, name="tavas3733", email="c@example.com", phone=None,
        shipping_phone="+919999999999", billing_phone=None, financial_status="PENDING",
        fulfillment_status="UNFULFILLED", cancelled_at=None, tags=("COD",),
        payment_gateway_names=("Cash on Delivery (COD)",),
        total=Money("949.00", "INR"), customer_locale="en",
        line_items=(
            LineItem(title="Blue Kurti", quantity=1, variant_title="Blue / M",
                      price=Money("999.00", "INR")),
        ),
        customer=_customer(),
    )
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


async def test_upsert_customer_stores_a_new_row() -> None:
    store = InMemoryIngestStore()
    await store.upsert_customer(_customer())
    assert store.customers["gid://shopify/Customer/1"].city == "Bengaluru"  # type: ignore[attr-defined]


async def test_upsert_customer_updates_existing_row_in_place() -> None:
    store = InMemoryIngestStore()
    await store.upsert_customer(_customer())
    await store.upsert_customer(_customer(city="Mumbai"))
    assert len(store.customers) == 1  # type: ignore[attr-defined]
    assert store.customers["gid://shopify/Customer/1"].city == "Mumbai"  # type: ignore[attr-defined]


async def test_upsert_order_mirror_stores_order_and_items_and_customer() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order())
    assert store.orders["gid://shopify/Order/1"].name == "tavas3733"  # type: ignore[attr-defined]
    assert len(store.order_items["gid://shopify/Order/1"]) == 1  # type: ignore[attr-defined]
    assert store.customers["gid://shopify/Customer/1"].first_name == "Suman"  # type: ignore[attr-defined]


async def test_upsert_order_mirror_without_customer_leaves_no_customer_row() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order(customer=None))
    assert store.orders["gid://shopify/Order/1"].customer is None  # type: ignore[attr-defined]
    assert store.customers == {}  # type: ignore[attr-defined]


async def test_upsert_order_mirror_replaces_items_not_appends() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order())
    updated = _order(line_items=(
        LineItem(title="New Item", quantity=2, variant_title=None, price=None),
    ))
    await store.upsert_order_mirror(updated)
    items = store.order_items["gid://shopify/Order/1"]  # type: ignore[attr-defined]
    assert len(items) == 1
    assert items[0].title == "New Item"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/test_order_mirror.py -v`
Expected: FAIL — `AttributeError: 'InMemoryIngestStore' object has no attribute 'upsert_customer'`.

- [ ] **Step 5: Implement `InMemoryIngestStore`**

In `backend/app/store/memory.py`, find `InMemoryIngestStore.__init__`. Add these three lines to
its body (alongside the existing state dicts):

```python
        self.customers: dict[str, Customer] = {}
        self.orders: dict[str, Order] = {}
        self.order_items: dict[str, tuple[LineItem, ...]] = {}
```

Add this import at the top of the file (alongside existing imports from `app.shopify.models` if
any exist — check first; if `app.shopify.models` isn't already imported here, add a fresh import
line):

```python
from app.shopify.models import Customer, LineItem, Order
```

Add these two methods to `InMemoryIngestStore` (anywhere among its other methods — after
`ingest_order_created` is a natural spot):

```python
    async def upsert_customer(self, customer: Customer) -> None:
        self.customers[customer.gid] = customer

    async def upsert_order_mirror(self, order: Order) -> None:
        if order.customer is not None:
            await self.upsert_customer(order.customer)
        self.orders[order.gid] = order
        self.order_items[order.gid] = order.line_items
```

- [ ] **Step 6: Confirm the in-memory tests pass**

Run: `cd backend && python -m pytest tests/store/test_order_mirror.py -v`
Expected: PASS, all five tests.

- [ ] **Step 7: Implement `PostgresIngestStore`**

In `backend/app/store/postgres.py`, add this import at the top (alongside the existing imports
from `app.store.base`):

```python
from app.shopify.models import Customer, Order
```

Add these two methods to `PostgresIngestStore` (after `ingest_order_created` is a natural spot):

```python
    async def upsert_customer(self, customer: Customer) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO customers (gid, first_name, last_name, email, phone, "
                "address_line1, address_line2, city, state, postal_code, country, synced_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now()) "
                "ON CONFLICT (gid) DO UPDATE SET first_name = $2, last_name = $3, email = $4, "
                "phone = $5, address_line1 = $6, address_line2 = $7, city = $8, state = $9, "
                "postal_code = $10, country = $11, synced_at = now()",
                customer.gid, customer.first_name, customer.last_name, customer.email,
                customer.phone, customer.address_line1, customer.address_line2, customer.city,
                customer.state, customer.postal_code, customer.country,
            )

    async def upsert_order_mirror(self, order: Order) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if order.customer is not None:
                    await conn.execute(
                        "INSERT INTO customers (gid, first_name, last_name, email, phone, "
                        "address_line1, address_line2, city, state, postal_code, country, "
                        "synced_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, "
                        "now()) ON CONFLICT (gid) DO UPDATE SET first_name = $2, "
                        "last_name = $3, email = $4, phone = $5, address_line1 = $6, "
                        "address_line2 = $7, city = $8, state = $9, postal_code = $10, "
                        "country = $11, synced_at = now()",
                        order.customer.gid, order.customer.first_name,
                        order.customer.last_name, order.customer.email, order.customer.phone,
                        order.customer.address_line1, order.customer.address_line2,
                        order.customer.city, order.customer.state, order.customer.postal_code,
                        order.customer.country,
                    )
                customer_gid = order.customer.gid if order.customer is not None else None
                total_amount = order.total.amount if order.total is not None else None
                total_currency = order.total.currency if order.total is not None else None
                await conn.execute(
                    "INSERT INTO orders (gid, name, order_number, customer_gid, email, "
                    "phone, shipping_phone, billing_phone, financial_status, "
                    "fulfillment_status, cancelled_at, tags, payment_gateway_names, "
                    "total_amount, total_currency, customer_locale, synced_at) VALUES "
                    "($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, "
                    "now()) ON CONFLICT (gid) DO UPDATE SET name = $2, order_number = $3, "
                    "customer_gid = $4, email = $5, phone = $6, shipping_phone = $7, "
                    "billing_phone = $8, financial_status = $9, fulfillment_status = $10, "
                    "cancelled_at = $11, tags = $12, payment_gateway_names = $13, "
                    "total_amount = $14, total_currency = $15, customer_locale = $16, "
                    "synced_at = now()",
                    order.gid, order.name, None, customer_gid, order.email, order.phone,
                    order.shipping_phone, order.billing_phone, order.financial_status,
                    order.fulfillment_status, order.cancelled_at, list(order.tags),
                    list(order.payment_gateway_names), total_amount, total_currency,
                    order.customer_locale,
                )
                await conn.execute(
                    "DELETE FROM order_items WHERE order_gid = $1", order.gid
                )
                for item in order.line_items:
                    price_amount = item.price.amount if item.price is not None else None
                    price_currency = item.price.currency if item.price is not None else None
                    await conn.execute(
                        "INSERT INTO order_items (order_gid, title, sku, quantity, "
                        "variant_title, price_amount, price_currency) VALUES "
                        "($1, $2, $3, $4, $5, $6, $7)",
                        order.gid, item.title, None, item.quantity, item.variant_title,
                        price_amount, price_currency,
                    )
```

Note: `order_number` and `sku` are inserted as `None` here — `Order`/`LineItem` don't carry
those fields yet at this point in the plan (they're populated starting Task 3's parser and
Task 1 already added `LineItem` without `sku`). This is corrected in Task 3, which adds `sku` to
`LineItem` and `order_number` handling — this task's job is only to prove the upsert mechanics
work; do not add fields here that don't exist on the dataclasses yet.

- [ ] **Step 8: Add a Postgres-gated integration test**

Read `backend/tests/store/` for an existing Postgres-gated test (search for `TEST_DATABASE_URL`
in any file under that directory) to match the exact skip-decorator pattern used elsewhere in
this codebase, then add one gated test to `backend/tests/store/test_order_mirror.py` following
that same pattern: build a `PostgresIngestStore` against `TEST_DATABASE_URL`, call
`upsert_order_mirror` twice with different data for the same `gid` (customer included), and
assert via raw `SELECT` that `customers`/`orders` have exactly one row each (not two) and
`order_items` reflects only the second call's items (not both calls' items combined).

- [ ] **Step 9: Run the full test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS (the new Postgres-gated test skips cleanly without `TEST_DATABASE_URL`,
matching every other gated test in this suite).

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 10: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/store/base.py app/store/memory.py app/store/postgres.py
```
Expected: EMPTY output.

- [ ] **Step 11: Commit**

```bash
git add backend/app/store/base.py backend/app/store/memory.py backend/app/store/postgres.py backend/tests/store/test_order_mirror.py
git commit -m "feat(store): add upsert_customer + upsert_order_mirror to IngestStore"
```

---

### Task 3: Webhook payload parsers + `LineItem.sku` + `order_number` wiring

**Files:**
- Modify: `backend/app/shopify/models.py` (add `sku` to `LineItem`)
- Modify: `backend/app/channels/shopify_orders.py`
- Modify: `backend/app/store/postgres.py` (wire `order_number`/`sku` now that they exist)
- Test: `backend/tests/test_shopify_orders.py`

**Interfaces:**
- Consumes: `Order`, `Customer`, `LineItem` (Task 1), the existing `_s`/`_d`/`_seq` helpers and
  `normalize_phone` import already in `shopify_orders.py`.
- Produces: `order_from_webhook_payload(payload: dict) -> Order | None` and
  `customer_from_webhook_payload(payload: dict) -> Customer | None`, both in
  `app/channels/shopify_orders.py`. `LineItem` gains `sku: str | None`.

- [ ] **Step 1: Read current state of all three files**

Read `backend/app/shopify/models.py`'s `LineItem` dataclass, `backend/app/channels/shopify_orders.py`
in full (the existing `_s`/`_d`/`_seq` helpers, `IncomingOrder`, `parse_order_created`), and
`backend/app/store/postgres.py`'s `upsert_order_mirror` from Task 2.

- [ ] **Step 2: Add `sku` to `LineItem`**

In `backend/app/shopify/models.py`, find:

```python
@dataclass(frozen=True)
class LineItem:
    title: str
    quantity: int
    variant_title: str | None
    price: Money | None
```

Replace with:

```python
@dataclass(frozen=True)
class LineItem:
    title: str
    quantity: int
    variant_title: str | None
    price: Money | None
    sku: str | None = None
```

(Defaulted so the existing GraphQL-path construction in `app/shopify/client.py`'s
`_line_items_from_node`, which doesn't pass `sku` yet, keeps working unmodified — Task 4 wires
`sku` into that path.)

- [ ] **Step 3: Write the failing tests**

In `backend/tests/test_shopify_orders.py`, add (the existing `PAYLOAD` dict at the top of this
file is reused as the base order payload):

```python
from app.channels.shopify_orders import (
    customer_from_webhook_payload,
    order_from_webhook_payload,
)

ORDER_WEBHOOK_PAYLOAD = {
    **PAYLOAD,
    "fulfillment_status": "fulfilled",
    "cancelled_at": None,
    "total_price": "949.00",
    "currency": "INR",
    "shipping_address": {
        "phone": "+919664290413", "address1": "12 MG Road", "address2": None,
        "city": "Bengaluru", "province": "Karnataka", "zip": "560001", "country": "India",
    },
    "billing_address": {"phone": None},
    "customer": {
        "id": 987654321, "admin_graphql_api_id": "gid://shopify/Customer/987654321",
        "first_name": "Suman", "last_name": "Bayala", "email": "c@example.com", "phone": None,
    },
    "line_items": [
        {"title": "Blue Chikankari Kurti", "sku": "KUR-BLU-M", "quantity": 1,
         "variant_title": "Blue / M", "price": "999.00"},
        {"title": "Cotton Dupatta", "sku": None, "quantity": 2,
         "variant_title": None, "price": "150.00"},
    ],
}


def test_order_from_webhook_payload_parses_full_order() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert order.gid == "gid://shopify/Order/12187547894128"
    assert order.name == "tavas3733"
    assert order.fulfillment_status == "fulfilled"
    assert order.cancelled_at is None
    assert order.total is not None
    assert order.total.amount == "949.00" and order.total.currency == "INR"
    assert order.shipping_phone == "+919664290413"
    assert order.billing_phone is None


def test_order_from_webhook_payload_parses_line_items() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert len(order.line_items) == 2
    first, second = order.line_items
    assert first.title == "Blue Chikankari Kurti"
    assert first.sku == "KUR-BLU-M"
    assert first.variant_title == "Blue / M"
    assert first.price is not None and first.price.amount == "999.00"
    assert second.sku is None
    assert second.variant_title is None


def test_order_from_webhook_payload_zero_line_items() -> None:
    p = {**ORDER_WEBHOOK_PAYLOAD, "line_items": []}
    order = order_from_webhook_payload(p)
    assert order is not None
    assert order.line_items == ()


def test_order_from_webhook_payload_missing_gid_returns_none() -> None:
    assert order_from_webhook_payload({"name": "x"}) is None


def test_order_from_webhook_payload_parses_customer() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert order.customer is not None
    assert order.customer.gid == "gid://shopify/Customer/987654321"
    assert order.customer.first_name == "Suman"
    assert order.customer.city == "Bengaluru"
    assert order.customer.postal_code == "560001"


def test_order_from_webhook_payload_no_customer_object() -> None:
    p = {k: v for k, v in ORDER_WEBHOOK_PAYLOAD.items() if k != "customer"}
    order = order_from_webhook_payload(p)
    assert order is not None
    assert order.customer is None


CUSTOMER_UPDATE_PAYLOAD = {
    "id": 987654321,
    "admin_graphql_api_id": "gid://shopify/Customer/987654321",
    "first_name": "Suman",
    "last_name": "Bayala",
    "email": "c@example.com",
    "phone": "+919999999999",
    "default_address": {
        "address1": "12 MG Road", "address2": "Flat 4B", "city": "Bengaluru",
        "province": "Karnataka", "zip": "560001", "country": "India",
    },
}


def test_customer_from_webhook_payload_parses_full_customer() -> None:
    cust = customer_from_webhook_payload(CUSTOMER_UPDATE_PAYLOAD)
    assert cust is not None
    assert cust.gid == "gid://shopify/Customer/987654321"
    assert cust.first_name == "Suman"
    assert cust.phone == "+919999999999"
    assert cust.address_line2 == "Flat 4B"
    assert cust.postal_code == "560001"


def test_customer_from_webhook_payload_missing_id_returns_none() -> None:
    assert customer_from_webhook_payload({"first_name": "x"}) is None


def test_customer_from_webhook_payload_no_default_address() -> None:
    p = {k: v for k, v in CUSTOMER_UPDATE_PAYLOAD.items() if k != "default_address"}
    cust = customer_from_webhook_payload(p)
    assert cust is not None
    assert cust.address_line1 is None
    assert cust.city is None
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_shopify_orders.py -v`
Expected: FAIL — `ImportError: cannot import name 'order_from_webhook_payload'`.

- [ ] **Step 5: Implement both parsers in `shopify_orders.py`**

In `backend/app/channels/shopify_orders.py`, add this import at the top:

```python
from app.shopify.models import Customer, LineItem, Money, Order
```

Add these two functions at the end of the file:

```python
def _line_items_from_webhook(raw: object) -> tuple[LineItem, ...]:
    items: list[LineItem] = []
    for node in _seq(raw):
        item = _d(node)
        title = _s(item.get("title"))
        if title is None:
            continue
        quantity = item.get("quantity")
        price_raw = _s(item.get("price"))
        items.append(
            LineItem(
                title=title,
                quantity=int(quantity) if isinstance(quantity, int) else 0,
                variant_title=_s(item.get("variant_title")),
                price=Money(amount=price_raw, currency="INR") if price_raw else None,
                sku=_s(item.get("sku")),
            )
        )
    return tuple(items)


def _customer_from_order_payload(payload: dict) -> Customer | None:  # type: ignore[type-arg]
    customer = _d(payload.get("customer"))
    gid = customer.get("admin_graphql_api_id")
    if not isinstance(gid, str) or not gid:
        return None
    shipping = _d(payload.get("shipping_address"))
    return Customer(
        gid=gid,
        first_name=_s(customer.get("first_name")),
        last_name=_s(customer.get("last_name")),
        email=_s(customer.get("email")),
        phone=normalize_phone(_s(customer.get("phone"))) or _s(customer.get("phone")),
        address_line1=_s(shipping.get("address1")),
        address_line2=_s(shipping.get("address2")),
        city=_s(shipping.get("city")),
        state=_s(shipping.get("province")),
        postal_code=_s(shipping.get("zip")),
        country=_s(shipping.get("country")),
    )


def order_from_webhook_payload(payload: dict) -> Order | None:  # type: ignore[type-arg]
    """Parse a full Shopify order webhook payload (orders/create or orders/updated) into an
    ``Order`` for the mirror -- the payload already carries everything needed, no extra Shopify
    call. Shares the same missing-gid/name guard as ``parse_order_created``."""
    gid = payload.get("admin_graphql_api_id")
    name = payload.get("name")
    if not isinstance(gid, str) or not isinstance(name, str) or not gid or not name:
        return None
    shipping = _d(payload.get("shipping_address"))
    billing = _d(payload.get("billing_address"))
    raw_tags = payload.get("tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, str):
        tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
    gateways = tuple(str(g) for g in _seq(payload.get("payment_gateway_names")))
    total_price = _s(payload.get("total_price"))
    currency = _s(payload.get("currency")) or "INR"
    return Order(
        gid=gid,
        name=name,
        email=_s(payload.get("email")),
        phone=normalize_phone(_s(payload.get("phone"))),
        shipping_phone=normalize_phone(_s(shipping.get("phone"))),
        billing_phone=normalize_phone(_s(billing.get("phone"))),
        financial_status=_s(payload.get("financial_status")),
        fulfillment_status=_s(payload.get("fulfillment_status")),
        cancelled_at=_s(payload.get("cancelled_at")),
        tags=tags,
        payment_gateway_names=gateways,
        total=Money(amount=total_price, currency=currency) if total_price else None,
        customer_locale=_s(payload.get("customer_locale")),
        line_items=_line_items_from_webhook(payload.get("line_items")),
        customer=_customer_from_order_payload(payload),
    )


def customer_from_webhook_payload(payload: dict) -> Customer | None:  # type: ignore[type-arg]
    """Parse a Shopify ``customers/update`` webhook payload -- a plain Customer resource, not
    nested in an order."""
    gid = payload.get("admin_graphql_api_id")
    if not isinstance(gid, str) or not gid:
        return None
    address = _d(payload.get("default_address"))
    return Customer(
        gid=gid,
        first_name=_s(payload.get("first_name")),
        last_name=_s(payload.get("last_name")),
        email=_s(payload.get("email")),
        phone=normalize_phone(_s(payload.get("phone"))) or _s(payload.get("phone")),
        address_line1=_s(address.get("address1")),
        address_line2=_s(address.get("address2")),
        city=_s(address.get("city")),
        state=_s(address.get("province")),
        postal_code=_s(address.get("zip")),
        country=_s(address.get("country")),
    )
```

- [ ] **Step 6: Confirm the tests pass**

Run: `cd backend && python -m pytest tests/test_shopify_orders.py -v`
Expected: PASS, all tests including the new ones.

- [ ] **Step 7: Wire `order_number` and `sku` into `PostgresIngestStore.upsert_order_mirror`**

`Order` doesn't carry `order_number` as a field (it's not part of the `Order` dataclass — only
`IncomingOrder` has it, for the separate `order_mappings` flow). Rather than adding a field to
`Order` this late, `order_number` in the `orders` mirror table can be derived from `Order.name`
at write time (Shopify order names are the store prefix + this same number, e.g. `"tavas3733"`
-> `3733`), which avoids widening `Order`'s shape for one column only the mirror needs. In
`backend/app/store/postgres.py`, find `upsert_order_mirror`'s `INSERT INTO orders` call (added
in Task 2) and change the second positional value from the literal `None` to a small inline
derivation. Replace:

```python
                    order.gid, order.name, None, customer_gid, order.email, order.phone,
```

with:

```python
                    order.gid, order.name, _order_number_from_name(order.name), customer_gid,
                    order.email, order.phone,
```

Add this small helper function near the top of `postgres.py` (after the existing
`_rows_affected` helper):

```python
def _order_number_from_name(name: str) -> int | None:
    digits = "".join(c for c in name if c.isdigit())
    return int(digits) if digits else None
```

Also find the `INSERT INTO order_items` call in the same method and replace the `None` for `sku`
with the item's own field. Replace:

```python
                        "($1, $2, $3, $4, $5, $6, $7)",
                        order.gid, item.title, None, item.quantity, item.variant_title,
                        price_amount, price_currency,
```

with:

```python
                        "($1, $2, $3, $4, $5, $6, $7)",
                        order.gid, item.title, item.sku, item.quantity, item.variant_title,
                        price_amount, price_currency,
```

- [ ] **Step 8: Add a regression test for `_order_number_from_name`**

Add to `backend/tests/store/test_order_mirror.py`:

```python
from app.store.postgres import _order_number_from_name


def test_order_number_from_name_extracts_digits() -> None:
    assert _order_number_from_name("tavas3733") == 3733


def test_order_number_from_name_no_digits_is_none() -> None:
    assert _order_number_from_name("tavas") is None
```

- [ ] **Step 9: Run the full test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 10: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/shopify/models.py app/channels/shopify_orders.py app/store/postgres.py
```
Expected: EMPTY output.

- [ ] **Step 11: Commit**

```bash
git add backend/app/shopify/models.py backend/app/channels/shopify_orders.py backend/app/store/postgres.py backend/tests/test_shopify_orders.py backend/tests/store/test_order_mirror.py
git commit -m "feat(channels): parse order/customer webhook payloads into the mirror's Order/Customer shape"
```

---

### Task 4: Extend `ORDER_FIELDS` (GraphQL) with customer, full shipping address, and SKU

**Files:**
- Modify: `backend/app/shopify/client.py`
- Test: `backend/tests/test_client_reads.py`

**Interfaces:**
- Consumes: `Customer` (Task 1), `LineItem.sku` (Task 3).
- Produces: `get_order`/`find_order_by_name` (both already existing, unchanged signatures) now
  also populate `Order.customer` and `LineItem.sku` — needed by Task 6's backfill script, which
  reads orders via this GraphQL path rather than webhook payloads.

- [ ] **Step 1: Read current state of `client.py`**

Read `backend/app/shopify/client.py` in full, focusing on `ORDER_FIELDS`, `_order_from_node`, and
`_line_items_from_node` (all added/extended earlier the same day this plan was written).

- [ ] **Step 2: Write the failing tests**

In `backend/tests/test_client_reads.py`, find `ORDER_NODE`. Add `customer` and extend
`shippingAddress` (keep every existing key, this only adds new ones) and each `lineItems` node
with `sku`:

```python
ORDER_NODE = {
    "id": "gid://shopify/Order/12187547894128",
    "name": "tavas3733",
    "email": "c@example.com",
    "phone": "+919999999999",
    "tags": ["COD", "COD pending"],
    "paymentGatewayNames": ["Cash on Delivery (COD)"],
    "displayFinancialStatus": "PENDING",
    "displayFulfillmentStatus": "UNFULFILLED",
    "cancelledAt": None,
    "customerLocale": "en-IN",
    "totalPriceSet": {"shopMoney": {"amount": "949.0", "currencyCode": "INR"}},
    "shippingAddress": {
        "phone": "+918888888888", "address1": "12 MG Road", "address2": None,
        "city": "Bengaluru", "province": "Karnataka", "zip": "560001", "country": "India",
    },
    "billingAddress": {"phone": None},
    "customer": {
        "id": "gid://shopify/Customer/987654321", "firstName": "Suman", "lastName": "Bayala",
        "email": "c@example.com",
    },
    "lineItems": {
        "edges": [
            {
                "node": {
                    "title": "Blue Chikankari Kurti",
                    "quantity": 1,
                    "variant": {"title": "Blue / M"},
                    "sku": "KUR-BLU-M",
                    "originalUnitPriceSet": {
                        "shopMoney": {"amount": "999.00", "currencyCode": "INR"}
                    },
                }
            },
            {
                "node": {
                    "title": "Cotton Dupatta",
                    "quantity": 2,
                    "variant": {"title": "Red"},
                    "sku": None,
                    "originalUnitPriceSet": {
                        "shopMoney": {"amount": "150.00", "currencyCode": "INR"}
                    },
                }
            },
        ]
    },
}
```

(This replaces the existing `ORDER_NODE` dict in place — every test that already reads from it
keeps working, since only new keys were added, nothing existing removed.) Then add two new
tests near the existing line-item tests:

```python
async def test_get_order_parses_customer(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.customer is not None
    assert order.customer.gid == "gid://shopify/Customer/987654321"
    assert order.customer.first_name == "Suman"
    assert order.customer.city == "Bengaluru"
    assert order.customer.postal_code == "560001"


async def test_get_order_missing_customer_parses_none(settings, master_key) -> None:
    node = {k: v for k, v in ORDER_NODE.items() if k != "customer"}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": node}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.customer is None


async def test_get_order_parses_line_item_sku(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    first, second = order.line_items
    assert first.sku == "KUR-BLU-M"
    assert second.sku is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_client_reads.py -v`
Expected: FAIL — the new customer/sku assertions fail (`AttributeError` or `None != expected`,
depending on current defaults), since `ORDER_FIELDS` doesn't request these fields yet.

- [ ] **Step 4: Extend `ORDER_FIELDS`, `_order_from_node`, and `_line_items_from_node`**

In `backend/app/shopify/client.py`, find `ORDER_FIELDS`. It currently ends with the `lineItems`
selection added earlier today. Replace the whole constant:

```python
ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    "displayFulfillmentStatus cancelledAt customerLocale "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone address1 address2 city province zip country } "
    "billingAddress { phone } "
    "customer { id firstName lastName email } "
    "lineItems(first: 50) { edges { node { title quantity variant { title } sku "
    "originalUnitPriceSet { shopMoney { amount currencyCode } } } } }"
)
```

Find `_line_items_from_node` (added earlier today). Locate the `LineItem(...)` construction
inside its loop and add one more keyword argument, `sku=item_node.get("sku")`, to it (the
existing arguments stay exactly as they are).

Find `_order_from_node`. It currently builds `Order(...)` with `line_items=_line_items_from_node(node),`
among its keyword arguments (added earlier today). Add one more keyword argument,
`customer=_customer_from_node(node),`, and add this new helper function directly above
`_order_from_node`:

```python
def _customer_from_node(node: dict[str, Any]) -> Customer | None:
    customer = node.get("customer")
    if not isinstance(customer, dict):
        return None
    gid = customer.get("id")
    if not isinstance(gid, str) or not gid:
        return None
    shipping = node.get("shippingAddress") or {}
    return Customer(
        gid=gid,
        first_name=customer.get("firstName"),
        last_name=customer.get("lastName"),
        email=customer.get("email"),
        phone=None,
        address_line1=shipping.get("address1"),
        address_line2=shipping.get("address2"),
        city=shipping.get("city"),
        state=shipping.get("province"),
        postal_code=shipping.get("zip"),
        country=shipping.get("country"),
    )
```

Find the import line near the top of `client.py` that imports from `app.shopify.models` (it
already imports `Order`, `LineItem`, `Money`, `AuthorizedOrder` or a subset — check the exact
current line) and add `Customer` to it.

- [ ] **Step 5: Confirm the tests pass**

Run: `cd backend && python -m pytest tests/test_client_reads.py -v`
Expected: PASS, all tests including the three new ones.

- [ ] **Step 6: Run the full test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 7: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/shopify/client.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/shopify/client.py backend/tests/test_client_reads.py
git commit -m "feat(shopify): fetch customer, full shipping address, and line-item SKU on order reads"
```

---

### Task 5: Generalize webhook subscription management to multiple topics

**Files:**
- Modify: `backend/app/shopify/subscriptions.py`
- Modify: `backend/app/jobs/router.py`
- Test: `backend/tests/test_subscriptions.py`

**Interfaces:**
- Consumes: `ShopifyClient` (unchanged).
- Produces: `ensure_subscription(client: ShopifyClient, callback_url: str) -> dict[str, str]` —
  signature CHANGES from returning a single `str` to a `dict[str, str]` mapping topic name (e.g.
  `"ORDERS_CREATE"`) to that topic's result (`"ok"`/`"created"`/`"updated"`).

- [ ] **Step 1: Read current state of both files**

Read `backend/app/shopify/subscriptions.py` in full and `backend/app/jobs/router.py`'s
`_job_ensure_subscription` function.

- [ ] **Step 2: Write the failing tests**

Read `backend/tests/test_subscriptions.py` in full first (it has six existing tests, all
asserting `ensure_subscription(...) == "ok"` or similar bare-string results — these will need
updating since the return type changes). Replace the entire file content:

```python
import json

import httpx

from app.shopify.subscriptions import REQUIRED_TOPICS, ensure_subscription
from tests.test_client_graphql import grant_or, make_client, seed


def sub_edge(url: str, topic: str = "ORDERS_CREATE", version: str = "2026-07") -> dict:
    return {"node": {"id": f"gid://shopify/WebhookSubscription/{topic}", "topic": topic,
                     "apiVersion": {"handle": version},
                     "endpoint": {"__typename": "WebhookHttpEndpoint", "callbackUrl": url}}}


async def test_all_topics_already_correct_makes_no_mutation(settings, master_key) -> None:
    calls: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify", topic=topic)]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result == {topic: "ok" for topic in REQUIRED_TOPICS}
    assert len(calls) == len(REQUIRED_TOPICS)  # one list query per topic, no mutations


async def test_missing_topic_is_created_independently(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/new"},
                "userErrors": []}}})
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        if topic == "ORDERS_CREATE":
            return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
                sub_edge("https://x.example/webhooks/shopify", topic="ORDERS_CREATE")]}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result["ORDERS_CREATE"] == "ok"
    assert result["ORDERS_UPDATED"] == "created"
    assert result["CUSTOMERS_UPDATE"] == "created"


async def test_wrong_url_subscription_is_updated_for_that_topic_only(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionUpdate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/updated"},
                "userErrors": []}}})
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        if topic == "ORDERS_CREATE":
            return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
                sub_edge("https://old.example/hook", topic="ORDERS_CREATE")]}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify", topic=topic)]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result["ORDERS_CREATE"] == "updated"
    assert result["ORDERS_UPDATED"] == "ok"
    assert result["CUSTOMERS_UPDATE"] == "ok"


async def test_create_sends_current_api_version(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/new"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await ensure_subscription(client, "https://x.example/webhooks/shopify")
    create_calls = [c for c in captured if "webhookSubscriptionCreate" in c["query"]]
    assert len(create_calls) == len(REQUIRED_TOPICS)
    assert all(c["variables"]["apiVersion"] == "2026-07" for c in create_calls)
```

(Four tests, deliberately fewer than the original six — the stale-API-version case is already
covered structurally by `test_wrong_url_subscription_is_updated_for_that_topic_only`'s per-topic
independence, so it isn't duplicated three more times per topic. `REQUIRED_TOPICS` is a new
exported constant this task adds.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_subscriptions.py -v`
Expected: FAIL — `ImportError: cannot import name 'REQUIRED_TOPICS'`.

- [ ] **Step 4: Generalize `subscriptions.py`**

Replace the entire content of `backend/app/shopify/subscriptions.py`:

```python
from app.shopify.client import ShopifyClient
from app.shopify.errors import ShopifyGraphQLError

REQUIRED_TOPICS: tuple[str, ...] = ("ORDERS_CREATE", "ORDERS_UPDATED", "CUSTOMERS_UPDATE")

_LIST_QUERY = (
    "query($topics: [WebhookSubscriptionTopic!]) { webhookSubscriptions(first: 20, "
    "topics: $topics) { edges { node { id topic apiVersion { handle } "
    "endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } } }"
)

_CREATE_MUTATION = (
    "mutation($topic: WebhookSubscriptionTopic!, $callbackUrl: URL!, $apiVersion: String!) "
    "{ webhookSubscriptionCreate(topic: $topic, webhookSubscription: {callbackUrl: "
    "$callbackUrl, apiVersion: $apiVersion, format: JSON}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)

_UPDATE_MUTATION = (
    "mutation($id: ID!, $callbackUrl: URL!, $apiVersion: String!) { webhookSubscriptionUpdate("
    "id: $id, webhookSubscription: {callbackUrl: $callbackUrl, apiVersion: $apiVersion}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)


def _raise_on_user_errors(node: dict) -> None:  # type: ignore[type-arg]
    errors = node.get("userErrors") or []
    if errors:
        raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])


async def _ensure_one_topic(
    client: ShopifyClient, topic: str, callback_url: str
) -> str:
    # F20: a subscription is correct ONLY when the callbackUrl AND the bound API version both
    # match — otherwise a version bump silently strands the sub on the old version.
    version = client.api_version
    data = await client._graphql(_LIST_QUERY, {"topics": [topic]})
    edges = (data.get("webhookSubscriptions") or {}).get("edges") or []
    for edge in edges:
        node = edge["node"]
        endpoint = node.get("endpoint") or {}
        current_version = (node.get("apiVersion") or {}).get("handle")
        if endpoint.get("callbackUrl") == callback_url and current_version == version:
            return "ok"
        result = await client._graphql(
            _UPDATE_MUTATION,
            {"id": node["id"], "callbackUrl": callback_url, "apiVersion": version},
        )
        _raise_on_user_errors(result.get("webhookSubscriptionUpdate") or {})
        return "updated"
    result = await client._graphql(
        _CREATE_MUTATION, {"topic": topic, "callbackUrl": callback_url, "apiVersion": version}
    )
    _raise_on_user_errors(result.get("webhookSubscriptionCreate") or {})
    return "created"


async def ensure_subscription(client: ShopifyClient, callback_url: str) -> dict[str, str]:
    """Ensure every topic in REQUIRED_TOPICS has a correctly-configured subscription.

    Shopify webhook subscriptions are one-per-topic, so each topic is checked/created/updated
    independently -- a stale ORDERS_CREATE subscription doesn't block CUSTOMERS_UPDATE from
    being created, and vice versa.
    """
    return {
        topic: await _ensure_one_topic(client, topic, callback_url)
        for topic in REQUIRED_TOPICS
    }
```

- [ ] **Step 5: Update the job wrapper**

In `backend/app/jobs/router.py`, `_job_ensure_subscription`'s body already does
`status = await ensure_subscription(...)` and returns `{"status": status}` — no code change is
needed there, since `status` now being a `dict[str, str]` instead of a `str` still serializes
correctly into the same `{"status": ...}` JSON response shape. Confirm this by reading the
function once more; if it does anything that assumes `status` is a string (e.g. string
formatting), adjust it — otherwise leave it as-is.

- [ ] **Step 6: Confirm the tests pass**

Run: `cd backend && python -m pytest tests/test_subscriptions.py -v`
Expected: PASS, all four tests.

- [ ] **Step 7: Run the full test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 8: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/shopify/subscriptions.py app/jobs/router.py
```
Expected: EMPTY output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/shopify/subscriptions.py backend/app/jobs/router.py backend/tests/test_subscriptions.py
git commit -m "feat(shopify): generalize webhook subscription management to multiple topics"
```

---

### Task 6: Wire `orders/updated` and `customers/update` into the webhook receiver

**Files:**
- Modify: `backend/app/channels/shopify_webhook.py`
- Test: `backend/tests/test_shopify_webhook.py`

**Interfaces:**
- Consumes: `order_from_webhook_payload`, `customer_from_webhook_payload` (Task 3),
  `c.ingest.upsert_order_mirror`, `c.ingest.upsert_customer` (Task 2).
- Produces: the webhook endpoint now accepts and correctly routes three topics instead of one.
  No new functions exported — this task is pure wiring inside the existing handler.

- [ ] **Step 1: Read current state of both files**

Read `backend/app/channels/shopify_webhook.py` in full and `backend/tests/test_shopify_webhook.py`
in full. Note the existing `test_other_topic_ignored` test, which currently posts with
`topic="orders/updated"` and asserts it's ignored — this describes the OLD behavior this task
changes and must be updated, not left as a contradiction (see Step 3).

- [ ] **Step 2: Write the failing tests**

In `backend/tests/test_shopify_webhook.py`, find `test_other_topic_ignored`:

```python
async def test_other_topic_ignored() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body, topic="orders/updated"))
    assert resp.json() == {"ok": True, "ignored": True}
```

Replace it with a version that tests a topic that's still genuinely unhandled:

```python
async def test_other_topic_ignored() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body, topic="products/create"))
    assert resp.json() == {"ok": True, "ignored": True}
```

Then add these new tests after it:

```python
async def test_orders_updated_populates_the_mirror() -> None:
    p = payload("gid://shopify/Order/mirror1")
    p["fulfillment_status"] = "fulfilled"
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, topic="orders/updated", webhook_id="wh-mirror1"))
    assert resp.status_code == 200
    store = get_container().ingest
    assert store.orders["gid://shopify/Order/mirror1"].fulfillment_status == "fulfilled"  # type: ignore[attr-defined]


async def test_orders_create_also_populates_the_mirror() -> None:
    body = json.dumps(payload("gid://shopify/Order/mirror2")).encode()
    resp = await post(body, headers(body, webhook_id="wh-mirror2"))
    assert resp.status_code == 200
    store = get_container().ingest
    assert "gid://shopify/Order/mirror2" in store.orders  # type: ignore[attr-defined]
    # The existing mapping/push-eligibility behavior for orders/create is unaffected:
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}


async def test_customers_update_populates_customers_table_only() -> None:
    p = {
        "id": 555, "admin_graphql_api_id": "gid://shopify/Customer/555",
        "first_name": "Anita", "last_name": "Rao", "email": "a@example.com",
        "phone": "+919888888888", "default_address": {"city": "Pune"},
    }
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-cust1"))
    assert resp.status_code == 200
    store = get_container().ingest
    assert store.customers["gid://shopify/Customer/555"].city == "Pune"  # type: ignore[attr-defined]
    assert store.orders == {}  # type: ignore[attr-defined]
    assert not store.mappings  # type: ignore[attr-defined]  # no order-mapping side effect


async def test_customers_update_malformed_payload_ignored() -> None:
    body = json.dumps({"first_name": "no id"}).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-cust2"))
    assert resp.status_code == 200
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py -v`
Expected: FAIL — the new mirror/customer assertions fail since `orders/updated` and
`customers/update` are still ignored (`{"ok": True, "ignored": True}` instead of `200` with
actual processing).

- [ ] **Step 4: Extend the webhook handler**

In `backend/app/channels/shopify_webhook.py`, add these two imports at the top (alongside the
existing import from `app.channels.shopify_orders`):

```python
from app.channels.shopify_orders import (
    choose_language,
    customer_from_webhook_payload,
    is_eligible_for_push,
    order_from_webhook_payload,
    parse_order_created,
)
```

Find:

```python
    topic = request.headers.get("X-Shopify-Topic", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    if topic != "orders/create" or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return JSONResponse({"ok": True, "ignored": True})
    incoming = parse_order_created(payload) if isinstance(payload, dict) else None
    if incoming is None:
        return JSONResponse({"ok": True, "ignored": True})
```

Replace with:

```python
    topic = request.headers.get("X-Shopify-Topic", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    handled_topics = {"orders/create", "orders/updated", "customers/update"}
    if topic not in handled_topics or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return JSONResponse({"ok": True, "ignored": True})
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True, "ignored": True})

    # orders/updated and customers/update deliberately do NOT go through the
    # processed_webhooks dedupe table the way orders/create does below: upsert_order_mirror /
    # upsert_customer are ON CONFLICT DO UPDATE, so replaying the same webhook twice just
    # re-writes identical data -- a no-op in effect, unlike orders/create's outbound-message
    # queuing, which has a real side effect that duplication would actually break.
    if topic == "customers/update":
        customer = customer_from_webhook_payload(payload)
        if customer is None:
            return JSONResponse({"ok": True, "ignored": True})
        await c.ingest.upsert_customer(customer)
        return JSONResponse({"ok": True, "ignored": False})

    if topic == "orders/updated":
        order = order_from_webhook_payload(payload)
        if order is None:
            return JSONResponse({"ok": True, "ignored": True})
        await c.ingest.upsert_order_mirror(order)
        return JSONResponse({"ok": True, "ignored": False})

    incoming = parse_order_created(payload)
    if incoming is None:
        return JSONResponse({"ok": True, "ignored": True})
    mirror_order = order_from_webhook_payload(payload)
    if mirror_order is not None:
        await c.ingest.upsert_order_mirror(mirror_order)
```

(The `orders/create` branch falls through to the existing, unchanged mapping/push-eligibility
code below it — only one new line, `mirror_order = ...` / the `if mirror_order is not None:`
block, is inserted before that existing code continues. Everything from `language =
choose_language(incoming.locale)` onward in the current file is untouched.)

- [ ] **Step 5: Confirm the tests pass**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Run the full test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 7: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/channels/shopify_webhook.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/channels/shopify_webhook.py backend/tests/test_shopify_webhook.py
git commit -m "feat(channels): handle orders/updated and customers/update webhooks, syncing the mirror"
```

---

### Task 7: One-time backfill script

**Files:**
- Modify: `backend/app/shopify/client.py` (new paginated method)
- Create: `backend/scripts/backfill_orders.py`
- Test: `backend/tests/test_client_reads.py` (pagination method)

**Interfaces:**
- Consumes: `ShopifyClient._graphql` (existing), `Order` (Task 1), `get_container()` (existing,
  `app/deps.py`), `c.ingest.upsert_order_mirror` (Task 2).
- Produces: `ShopifyClient.list_orders_created_since(since_iso: str) -> AsyncIterator[Order]` —
  new method. `backend/scripts/backfill_orders.py` — new standalone script, run manually by the
  owner (`python -m scripts.backfill_orders`), not wired into any cron job.

- [ ] **Step 1: Read current state**

Read `backend/app/shopify/client.py` in full (particularly `_graphql`, `find_order_by_name`, and
the now-extended `ORDER_FIELDS`/`_order_from_node` from Task 4) and `backend/scripts/apply_schema.py`
in full (the shape this new script follows for its `if __name__ == "__main__":` entrypoint).

- [ ] **Step 2: Write the failing test for pagination**

In `backend/tests/test_client_reads.py`, add:

```python
async def test_list_orders_created_since_pages_through_results(settings, master_key) -> None:
    page1 = {
        "orders": {
            "edges": [{"cursor": "c1", "node": ORDER_NODE}],
            "pageInfo": {"hasNextPage": True},
        }
    }
    page2_node = {**ORDER_NODE, "id": "gid://shopify/Order/second", "name": "tavas9999"}
    page2 = {
        "orders": {
            "edges": [{"cursor": "c2", "node": page2_node}],
            "pageInfo": {"hasNextPage": False},
        }
    }
    calls: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["variables"].get("cursor") is None:
            return httpx.Response(200, json={"data": page1})
        return httpx.Response(200, json={"data": page2})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    orders = [o async for o in client.list_orders_created_since("2025-08-10")]
    assert len(orders) == 2
    assert orders[0].gid == "gid://shopify/Order/12187547894128"
    assert orders[1].gid == "gid://shopify/Order/second"
    assert len(calls) == 2
    assert calls[1]["variables"]["cursor"] == "c1"


async def test_list_orders_created_since_empty_result(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": {
            "edges": [], "pageInfo": {"hasNextPage": False}}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    orders = [o async for o in client.list_orders_created_since("2025-08-10")]
    assert orders == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_client_reads.py -v`
Expected: FAIL — `AttributeError: 'ShopifyClient' object has no attribute 'list_orders_created_since'`.

- [ ] **Step 4: Implement `list_orders_created_since`**

In `backend/app/shopify/client.py`, add `from collections.abc import AsyncIterator` to the
imports at the top if not already present (check first). Add this new method to `ShopifyClient`,
near `find_order_by_name`:

```python
    async def list_orders_created_since(self, since_iso: str) -> AsyncIterator[Order]:
        """Page through every order created on or after ``since_iso`` (a date string like
        "2025-08-10"), yielding each as an ``Order``. Used only by the one-time backfill
        script -- not part of any live customer-facing read path."""
        query = (
            "query($q: String!, $cursor: String) { orders(first: 50, after: $cursor, "
            f"query: $q) {{ edges {{ cursor node {{ {ORDER_FIELDS} }} }} "
            "pageInfo { hasNextPage } } }"
        )
        cursor: str | None = None
        search = f"created_at:>={since_iso}"
        while True:
            data = await self._graphql(query, {"q": search, "cursor": cursor})
            connection = data.get("orders") or {}
            edges = connection.get("edges") or []
            for edge in edges:
                yield _order_from_node(edge["node"])
            if not edges or not (connection.get("pageInfo") or {}).get("hasNextPage"):
                return
            cursor = edges[-1]["cursor"]
```

- [ ] **Step 5: Confirm the tests pass**

Run: `cd backend && python -m pytest tests/test_client_reads.py -v`
Expected: PASS, all tests including the two new ones.

- [ ] **Step 6: Write the backfill script**

Create `backend/scripts/backfill_orders.py`:

```python
"""One-time backfill: pull the last 12 months of Shopify orders into the local mirror
(customers/orders/order_items tables). Run once: python -m scripts.backfill_orders

Unlike apply_schema.py, this needs Shopify credentials (which require APP_MASTER_KEY to
decrypt), so it goes through the normal app wiring (get_container()) rather than a bare
DATABASE_URL connection.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.deps import get_container

BACKFILL_WINDOW_DAYS = 365


async def main() -> None:
    c = get_container()
    since = (datetime.now(UTC) - timedelta(days=BACKFILL_WINDOW_DAYS)).strftime("%Y-%m-%d")
    count = 0
    async for order in c.shopify.list_orders_created_since(since):
        await c.ingest.upsert_order_mirror(order)
        count += 1
        if count % 50 == 0:
            print(f"backfilled {count} orders so far...")
    print(f"backfill complete: {count} orders synced (created since {since})")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 7: Run the full test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app scripts`
Expected: Success, no issues found. (If `scripts/` isn't currently included in the `mypy`
target, check `backend/pyproject.toml`'s mypy `files` setting first — `apply_schema.py` already
exists under `scripts/`, so it should already be covered; if it isn't, that's a pre-existing gap,
not something to fix as part of this task.)

- [ ] **Step 8: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/shopify/client.py scripts/backfill_orders.py
```
Expected: EMPTY output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/shopify/client.py backend/scripts/backfill_orders.py backend/tests/test_client_reads.py
git commit -m "feat(scripts): one-time backfill of the last 12 months of Shopify orders into the mirror"
```

---

## After all 7 tasks: manual, owner-run step (not part of any task's automated commit)

Once merged and deployed, the owner needs to actually run `python -m scripts.backfill_orders`
once (with `DATABASE_URL`/`APP_MASTER_KEY`/Shopify credentials available, same as the earlier
`apply_schema.py` run this session) to populate the mirror with existing orders — and the
`ensure_subscription` cron job (already scheduled per the existing deployment checklist) will
pick up the two new topics (`ORDERS_UPDATED`, `CUSTOMERS_UPDATE`) automatically on its next run,
no manual Shopify-admin action needed. Neither of these is a plan task since they're operational
steps, not code — call this out to the owner when the branch is ready to merge.
