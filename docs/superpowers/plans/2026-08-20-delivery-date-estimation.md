# Delivery-Date Estimation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a customer asks the WhatsApp bot "when will my order arrive," the order-tracking agent always gives a delivery-date estimate (never leaves them unanswered), computed from a fixed formula — never invented by the LLM, never scraped from a courier site.

**Architecture:** A new pure function `estimate_delivery(order, today)` in `app/core/delivery_estimate.py` computes a date from `Order.created_at` (a field that doesn't exist yet and must be added end-to-end) plus a regional zone lookup keyed on the customer's shipping state. `order_tracking.py` renders the result as a plain-text line in the LLM's order context; the LLM only relays it verbatim, it never computes a date itself.

**Tech Stack:** Python 3.12+, dataclasses, pytest/pytest-asyncio, asyncpg (Postgres mirror), existing Shopify GraphQL client.

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean.
- No bare `except:`; catch specific exceptions only.
- `ruff check .` (whole project, including `tests/`) must stay clean.
- No `print()` — use `logging` if logging is ever needed (not expected in this feature).
- Formula-only: do NOT read/scrape any courier tracking page or add any new external HTTP call. `estimate_delivery` must be a pure function of `(Order, date)` with no I/O.
- Every new/changed file must pass the no-secrets compliance grep (none of this feature touches credentials, so this should trivially pass, but run it anyway per the developer agent's standard checklist).
- The LLM must never be told to invent or adjust the estimated date — it only relays the precomputed line, exactly as given, including the "estimate, may vary by 1-2 days" caveat.
- Additive schema/model changes only: `Order.created_at` defaults to `None`; nothing existing changes shape.

---

## Reference: existing code this plan builds on

- `app/shopify/models.py` — `Order` dataclass (currently no `created_at` field; has `updated_at: str | None = None`).
- `app/shopify/client.py` — `ORDER_FIELDS` (GraphQL selection set, shared by every order-fetching query) and `_order_from_node` (parses a GraphQL node into `Order`).
- `app/channels/shopify_orders.py` — `order_from_webhook_payload` (parses a webhook payload into `Order` for the mirror; uses `_c(v)` = coerced + length-capped string accessor).
- `app/store/schema.sql` — the `orders` table **already has** an `order_created_at timestamptz` column (added in an earlier migration, already deployed — confirmed via `git log -S`). **No new migration needed.** It is simply unpopulated and unread by any code today.
- `app/store/postgres.py` — `_MIRROR_ORDER_SELECT` (read query), `_order_from_row` (row → `Order`), `upsert_order_mirror` (write; the `INSERT INTO orders` statement's `WHERE orders.updated_at IS NULL OR EXCLUDED.updated_at IS NULL OR EXCLUDED.updated_at >= orders.updated_at` staleness guard applies to the whole row — no separate guard needed for the new column).
- `app/store/memory.py` — `InMemoryIngestStore` stores whole `Order` objects directly, so it needs **no code change** for the new field (it round-trips automatically once the dataclass has it).
- `app/agents/order_tracking.py` — `_order_line`/`_tracking_line` render order data into the LLM's system prompt; `_is_cancel_eligible` is the existing pattern for a fulfillment-status predicate to copy.

---

### Task 1: `Order.created_at` field + Shopify GraphQL client wiring

**Files:**
- Modify: `backend/app/shopify/models.py` (`Order` dataclass, currently lines 72-93)
- Modify: `backend/app/shopify/client.py` (`ORDER_FIELDS` at line 58-74, `_order_from_node` at line 167-190)
- Test: `backend/tests/test_client_reads.py`

**Interfaces:**
- Produces: `Order.created_at: str | None` (raw ISO-8601, same convention as `Order.updated_at`/`Order.cancelled_at`) — consumed by Task 3 (Postgres wiring) and Task 4 (`estimate_delivery`).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_client_reads.py`, right after `ORDER_NODE`'s definition (after line 57, before `_FULFILLED_ORDER_NODE`):

```python
ORDER_NODE_WITH_CREATED_AT = {**ORDER_NODE, "createdAt": "2026-08-14T03:14:46Z"}
```

Then add a new test near `test_get_order_parses_full_node`:

```python
async def test_get_order_parses_created_at(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE_WITH_CREATED_AT}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.created_at == "2026-08-14T03:14:46Z"


async def test_get_order_missing_created_at_is_none(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.created_at is None


async def test_get_order_query_selects_created_at(settings, master_key) -> None:
    # The read path must SELECT createdAt, not just parse it -- mirrors the existing
    # test_get_order_populates_updated_at_for_the_mirrors_staleness_guard pattern.
    captured: dict[str, str] = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured["query"] = json.loads(request.content)["query"]
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.get_order("gid://shopify/Order/12187547894128")
    assert "createdAt" in captured["query"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/test_client_reads.py -k created_at -v` (from `backend/`, using whichever interpreter is currently provisioned on this machine — see `docs/memory/error_learnings.md`'s most recent machine-specific entry for the path)
Expected: FAIL — `order.created_at` doesn't exist (`AttributeError`), and the query never selects `createdAt`.

- [ ] **Step 3: Add the field to `Order`**

In `backend/app/shopify/models.py`, in the `Order` dataclass (around line 90), add the new field directly after `updated_at`:

```python
    updated_at: str | None = None
    # Shopify Order.createdAt, raw ISO-8601 -- the starting point for the delivery-date
    # estimate formula (app/core/delivery_estimate.py). None for orders synced before this
    # field existed; callers that need it must handle that (they do -- see delivery_estimate).
    created_at: str | None = None
```

- [ ] **Step 4: Wire the GraphQL client**

In `backend/app/shopify/client.py`, change `ORDER_FIELDS` (line 64) from:

```python
    "displayFulfillmentStatus cancelledAt customerLocale updatedAt "
```

to:

```python
    "displayFulfillmentStatus cancelledAt customerLocale createdAt updatedAt "
```

In `_order_from_node` (around line 187), add the new field right after `updated_at`:

```python
        updated_at=node.get("updatedAt"),  # selected by ORDER_FIELDS; see the note there
        created_at=node.get("createdAt"),  # selected by ORDER_FIELDS; see the note there
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_client_reads.py -v`
Expected: PASS (all tests in the file, not just the new ones — `ORDER_FIELDS` is shared by every order query, so a broken change here would show up broadly).

- [ ] **Step 6: Run the full compliance gate**

Run (from `backend/`):
```
python -m ruff check .
python -m mypy app
```
Expected: both clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/shopify/models.py backend/app/shopify/client.py backend/tests/test_client_reads.py
git commit -m "feat(shopify): add Order.created_at, wire through GraphQL client"
```

---

### Task 2: Webhook parser wiring

**Files:**
- Modify: `backend/app/channels/shopify_orders.py` (`order_from_webhook_payload`, around line 219-255)
- Test: `backend/tests/test_shopify_orders.py`

**Interfaces:**
- Consumes: `Order.created_at: str | None` (Task 1).
- Produces: `order_from_webhook_payload(payload)` now also populates `.created_at` — consumed by Task 3's mirror write path when a webhook (not a live GraphQL read) is the source.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_shopify_orders.py`, near `test_order_from_webhook_payload_parses_full_order`:

```python
def test_order_from_webhook_payload_parses_created_at() -> None:
    order = order_from_webhook_payload(ORDER_WEBHOOK_PAYLOAD)
    assert order is not None
    assert order.created_at == "2026-07-28T03:14:46-04:00"


def test_order_from_webhook_payload_missing_created_at_is_none() -> None:
    p = {k: v for k, v in ORDER_WEBHOOK_PAYLOAD.items() if k != "created_at"}
    order = order_from_webhook_payload(p)
    assert order is not None
    assert order.created_at is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/test_shopify_orders.py -k created_at -v`
Expected: FAIL — `order.created_at` is `None` for the first test (the field isn't populated) or `AttributeError` if Task 1 wasn't run first in this environment.

- [ ] **Step 3: Wire the parser**

In `backend/app/channels/shopify_orders.py`, in `order_from_webhook_payload` (around line 254), add the new field right after `updated_at`:

```python
        updated_at=_c(payload.get("updated_at")),
        created_at=_c(payload.get("created_at")),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_shopify_orders.py -v`
Expected: PASS (full file).

- [ ] **Step 5: Run the compliance gate**

```
python -m ruff check .
python -m mypy app
```
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/channels/shopify_orders.py backend/tests/test_shopify_orders.py
git commit -m "feat(shopify): populate Order.created_at from the orders webhook payload"
```

---

### Task 3: Postgres mirror read/write wiring

**Files:**
- Modify: `backend/app/store/postgres.py` (`_MIRROR_ORDER_SELECT` line 62-73, `_order_from_row` line 76-104, `upsert_order_mirror` line 503-586)
- Test: `backend/tests/store/test_order_mirror.py`

**Interfaces:**
- Consumes: `Order.created_at: str | None` (Task 1); `_parse_timestamp(raw: str | None) -> datetime | None` (already exists at `postgres.py:251`, reused as-is).
- Produces: `Order.created_at` now round-trips through `get_mirrored_order`/`find_mirrored_order_by_name`/`find_mirrored_orders_by_phone` and `upsert_order_mirror` — consumed by Task 4/5 (`estimate_delivery` needs a real value on an order read from the mirror, which is the normal path for the WhatsApp Q&A agent per `app/core/mirror_order_source.py`).

**No schema migration in this task** — `orders.order_created_at timestamptz` already exists in `schema.sql` (and, per the design doc, is already deployed); this task only wires code to read/write it. `InMemoryIngestStore` needs no code change (it stores whole `Order` objects), but gets one new test for parity assurance.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/store/test_order_mirror.py`:

1. Update the `_fake_order_row` helper (around line 549-557) to add the new key so `_order_from_row` doesn't `KeyError`:

```python
def _fake_order_row(gid: str, name: str = "tavas1") -> dict[str, object]:
    """A minimal `orders LEFT JOIN customers` row (customer absent) for `_order_from_row`."""
    return {
        "gid": gid, "name": name, "email": None, "phone": None,
        "shipping_phone": None, "billing_phone": None, "financial_status": None,
        "fulfillment_status": None, "cancelled_at": None, "tags": None,
        "payment_gateway_names": None, "total_amount": None, "total_currency": None,
        "customer_locale": None, "updated_at": None, "order_created_at": None,
        "c_gid": None,
    }
```

2. Add two new unit tests near `test_pg_mirror_upserts_carry_an_out_of_order_delivery_guard`:

```python
async def test_pg_mirror_upsert_writes_created_at() -> None:
    conn = _RecordingConn()
    await _pg(conn).upsert_order_mirror(
        _order(created_at="2026-07-28T03:14:46-04:00")
    )
    orders_sql = [
        (sql, args) for sql, args in conn.executed if sql.startswith("INSERT INTO orders")
    ]
    assert len(orders_sql) == 1
    sql, args = orders_sql[0]
    assert "order_created_at" in sql
    assert args[17] == datetime.fromisoformat("2026-07-28T03:14:46-04:00")  # $18


async def test_pg_mirror_select_reads_order_created_at() -> None:
    # The read query must SELECT the column, not just the write side populate it.
    # PostgresIngestStore.get_mirrored_order uses conn.fetchrow, but find_mirrored_orders_by_phone
    # uses conn.fetch, which _FakeReadConn implements -- use that path to inspect the query text.
    conn = _FakeReadConn([_fake_order_row("gid://a")], [])
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]
    await store.find_mirrored_orders_by_phone("+919876500000")
    assert "o.order_created_at" in conn.fetch_calls[0][0]
```

3. Add an in-memory parity test near `test_get_mirrored_order_returns_stored_order`:

```python
async def test_upsert_order_mirror_memory_round_trips_created_at() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order(created_at="2026-07-28T03:14:46-04:00"))
    result = await store.get_mirrored_order("gid://shopify/Order/1")
    assert result is not None
    assert result.created_at == "2026-07-28T03:14:46-04:00"
```

4. Add a gated Postgres round-trip test near the other `@pytest.mark.skipif(not DSN, ...)` tests:

```python
@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_upsert_order_mirror_pg_created_at_round_trips(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(
        _order(gid=gid, customer=None, created_at="2026-07-28T03:14:46-04:00")
    )
    result = await store.get_mirrored_order(gid)
    assert result is not None
    assert result.created_at == "2026-07-28T03:14:46-04:00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/store/test_order_mirror.py -k created_at -v`
Expected: the `_RecordingConn`/`_FakeReadConn`-based tests FAIL (`order_created_at`/`o.order_created_at` not in the SQL yet); the in-memory test FAILS (`result.created_at is None`, mismatched assertion) only once Task 1 has landed `Order.created_at` — otherwise it errors on `_order(created_at=...)` being an unexpected kwarg, which is also a legitimate RED. The gated PG test SKIPS (`TEST_DATABASE_URL` unset in this sandbox, per `error_learnings.md`) — that's expected, not a failure to chase here.

- [ ] **Step 3: Wire `_MIRROR_ORDER_SELECT` and `_order_from_row`**

In `backend/app/store/postgres.py`, change `_MIRROR_ORDER_SELECT` (lines 62-73) from:

```python
_MIRROR_ORDER_SELECT = (
    "SELECT o.gid, o.name, o.email, o.phone, o.shipping_phone, o.billing_phone, "
    "o.financial_status, o.fulfillment_status, o.cancelled_at, o.tags, "
    "o.payment_gateway_names, o.total_amount, o.total_currency, o.customer_locale, "
    "o.updated_at, "
    "c.gid AS c_gid, c.first_name AS c_first_name, c.last_name AS c_last_name, "
    "c.email AS c_email, c.phone AS c_phone, c.address_line1 AS c_address_line1, "
    "c.address_line2 AS c_address_line2, c.city AS c_city, c.state AS c_state, "
    "c.postal_code AS c_postal_code, c.country AS c_country, "
    "c.updated_at AS c_updated_at "
    "FROM orders o LEFT JOIN customers c ON c.gid = o.customer_gid "
)
```

to:

```python
_MIRROR_ORDER_SELECT = (
    "SELECT o.gid, o.name, o.email, o.phone, o.shipping_phone, o.billing_phone, "
    "o.financial_status, o.fulfillment_status, o.cancelled_at, o.tags, "
    "o.payment_gateway_names, o.total_amount, o.total_currency, o.customer_locale, "
    "o.updated_at, o.order_created_at, "
    "c.gid AS c_gid, c.first_name AS c_first_name, c.last_name AS c_last_name, "
    "c.email AS c_email, c.phone AS c_phone, c.address_line1 AS c_address_line1, "
    "c.address_line2 AS c_address_line2, c.city AS c_city, c.state AS c_state, "
    "c.postal_code AS c_postal_code, c.country AS c_country, "
    "c.updated_at AS c_updated_at "
    "FROM orders o LEFT JOIN customers c ON c.gid = o.customer_gid "
)
```

In `_order_from_row` (around line 102), add the new field right after `updated_at`:

```python
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        created_at=row["order_created_at"].isoformat() if row["order_created_at"] else None,
        fulfillments=tuple(fulfillments or ()),
```

- [ ] **Step 4: Wire `upsert_order_mirror`**

In `backend/app/store/postgres.py`, change the `INSERT INTO orders` statement (lines 523-550) from:

```python
                applied = await conn.fetchval(
                    "INSERT INTO orders (gid, name, order_number, customer_gid, email, "
                    "phone, shipping_phone, billing_phone, financial_status, "
                    "fulfillment_status, cancelled_at, tags, payment_gateway_names, "
                    "total_amount, total_currency, customer_locale, updated_at, synced_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, "
                    "$16, $17, now()) ON CONFLICT (gid) DO UPDATE SET name = $2, "
                    "order_number = $3, "
                    "customer_gid = $4, email = $5, phone = $6, shipping_phone = $7, "
                    "billing_phone = $8, financial_status = $9, fulfillment_status = $10, "
                    "cancelled_at = $11, tags = $12, payment_gateway_names = $13, "
                    "total_amount = $14, total_currency = $15, customer_locale = $16, "
                    "updated_at = $17, synced_at = now() "
                    # Out-of-order-delivery guard (see _upsert_customer_on_conn): a late RETRY
                    # of an older orders/updated must not revert newer state -- on a terminal
                    # order (cancelled/fulfilled) nothing would ever correct it again.
                    "WHERE orders.updated_at IS NULL OR EXCLUDED.updated_at IS NULL "
                    "OR EXCLUDED.updated_at >= orders.updated_at "
                    "RETURNING gid",
                    order.gid, order.name, _order_number_from_name(order.name),
                    customer_gid, order.email, _e164(order.phone),
                    _e164(order.shipping_phone), _e164(order.billing_phone),
                    order.financial_status,
                    order.fulfillment_status, _parse_timestamp(order.cancelled_at),
                    list(order.tags),
                    list(order.payment_gateway_names), total_amount, total_currency,
                    order.customer_locale, _parse_timestamp(order.updated_at),
                )
```

to (new `order_created_at` column appended at the end of every clause — `$18` — so no existing positional index shifts, keeping every pre-existing index-based test assertion valid):

```python
                applied = await conn.fetchval(
                    "INSERT INTO orders (gid, name, order_number, customer_gid, email, "
                    "phone, shipping_phone, billing_phone, financial_status, "
                    "fulfillment_status, cancelled_at, tags, payment_gateway_names, "
                    "total_amount, total_currency, customer_locale, updated_at, "
                    "order_created_at, synced_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, "
                    "$16, $17, $18, now()) ON CONFLICT (gid) DO UPDATE SET name = $2, "
                    "order_number = $3, "
                    "customer_gid = $4, email = $5, phone = $6, shipping_phone = $7, "
                    "billing_phone = $8, financial_status = $9, fulfillment_status = $10, "
                    "cancelled_at = $11, tags = $12, payment_gateway_names = $13, "
                    "total_amount = $14, total_currency = $15, customer_locale = $16, "
                    "updated_at = $17, order_created_at = $18, synced_at = now() "
                    # Out-of-order-delivery guard (see _upsert_customer_on_conn): a late RETRY
                    # of an older orders/updated must not revert newer state -- on a terminal
                    # order (cancelled/fulfilled) nothing would ever correct it again.
                    "WHERE orders.updated_at IS NULL OR EXCLUDED.updated_at IS NULL "
                    "OR EXCLUDED.updated_at >= orders.updated_at "
                    "RETURNING gid",
                    order.gid, order.name, _order_number_from_name(order.name),
                    customer_gid, order.email, _e164(order.phone),
                    _e164(order.shipping_phone), _e164(order.billing_phone),
                    order.financial_status,
                    order.fulfillment_status, _parse_timestamp(order.cancelled_at),
                    list(order.tags),
                    list(order.payment_gateway_names), total_amount, total_currency,
                    order.customer_locale, _parse_timestamp(order.updated_at),
                    _parse_timestamp(order.created_at),
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/store/test_order_mirror.py -v`
Expected: PASS (full file). The gated PG test still SKIPS unless `TEST_DATABASE_URL` is set in this environment — that is expected and matches every other gated test in this file; if you do have a scratch Postgres available, run with it set and confirm that test PASSes too, but do not treat a skip as a blocker.

- [ ] **Step 6: Run the compliance gate**

```
python -m ruff check .
python -m mypy app
```
Expected: both clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/store/postgres.py backend/tests/store/test_order_mirror.py
git commit -m "feat(store): wire Order.created_at through the Postgres order mirror"
```

---

### Task 4: `estimate_delivery` — zone mapping + formula

**Files:**
- Create: `backend/app/core/delivery_estimate.py`
- Test: `backend/tests/core/test_delivery_estimate.py` (new file)

**Interfaces:**
- Consumes: `Order` (`app.shopify.models`) — specifically `.created_at`, `.fulfillment_status`, `.fulfillments` (each a `Fulfillment` with `.delivered_at`), `.customer` (a `Customer | None` with `.state`).
- Produces:
  ```python
  @dataclass(frozen=True)
  class DeliveryEstimate:
      expected_date: date

  def estimate_delivery(order: Order, today: date) -> DeliveryEstimate | None: ...
  ```
  Consumed by Task 5 (`order_tracking.py`).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/core/test_delivery_estimate.py`:

```python
from dataclasses import replace
from datetime import date

from app.core.delivery_estimate import estimate_delivery
from app.shopify.models import Customer, Fulfillment, Order


def _customer(state: str | None) -> Customer:
    return Customer(
        gid="gid://c/1", first_name=None, last_name=None, email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=state,
        postal_code=None, country=None,
    )


def _order(
    created_at: str | None = "2026-08-10T00:00:00+00:00",
    state: str | None = "Gujarat",
    fulfillment_status: str | None = None,
    fulfillments: tuple[Fulfillment, ...] = (),
) -> Order:
    return Order(
        gid="gid://o/1", name="tavas1", email=None, phone=None, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=fulfillment_status,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, customer=_customer(state), created_at=created_at,
        fulfillments=fulfillments,
    )


def test_west_zone_adds_two_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    # 2 days prep + 2 days west transit = 4 days from order-created
    assert result.expected_date == date(2026, 8, 14)


def test_north_zone_adds_three_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Delhi")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 15)


def test_east_zone_adds_five_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="West Bengal")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)


def test_south_zone_adds_five_transit_days() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Tamil Nadu")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)


def test_zone_match_is_case_insensitive() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 14)


def test_unknown_state_defaults_to_the_longest_zone() -> None:
    order = _order(created_at="2026-08-10T00:00:00+00:00", state="Atlantis")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)  # 2 + 5 (south/east default)


def test_missing_customer_defaults_to_the_longest_zone() -> None:
    order = replace(_order(created_at="2026-08-10T00:00:00+00:00"), customer=None)
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 17)


def test_late_ship_exception_adds_two_more_days() -> None:
    # >3 days since order-created AND still not dispatched.
    order = _order(created_at="2026-08-01T00:00:00+00:00", state="Gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    # 2 prep + 2 west + 2 late-ship = 6 days from order-created
    assert result.expected_date == date(2026, 8, 7)


def test_late_ship_exception_does_not_fire_within_three_days() -> None:
    order = _order(created_at="2026-08-08T00:00:00+00:00", state="Gujarat")
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 12)  # no +2, still within the window


def test_late_ship_exception_does_not_fire_once_dispatched() -> None:
    order = _order(
        created_at="2026-08-01T00:00:00+00:00", state="Gujarat",
        fulfillment_status="FULFILLED",
    )
    result = estimate_delivery(order, today=date(2026, 8, 10))
    assert result is not None
    assert result.expected_date == date(2026, 8, 5)  # no late-ship +2, dispatched already


def test_already_delivered_returns_none() -> None:
    order = _order(
        fulfillments=(
            Fulfillment(
                gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
                tracking_number="AWB1", tracking_url="https://track/AWB1",
                delivered_at="2026-08-12T00:00:00+00:00",
            ),
        ),
    )
    assert estimate_delivery(order, today=date(2026, 8, 13)) is None


def test_missing_created_at_returns_none() -> None:
    order = _order(created_at=None)
    assert estimate_delivery(order, today=date(2026, 8, 10)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/core/test_delivery_estimate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.delivery_estimate'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/core/delivery_estimate.py`:

```python
"""Formula-based delivery-date estimate for the order-tracking Q&A agent.

Deliberately does NOT read or scrape any courier tracking page for a real ETA (see
docs/superpowers/specs/2026-08-20-delivery-date-estimation-design.md -- that would reopen
client-decisions-all.md Q10, which already closed "no live courier integration"). This is a
pure function of (Order, today): prep buffer + a fixed regional zone transit time, with a
late-ship exception. Never invents a date beyond what this formula computes -- the caller
(app/agents/order_tracking.py) renders the result as plain text for the LLM to relay verbatim.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.shopify.models import Order

_PREP_DAYS = 2
_LATE_SHIP_THRESHOLD_DAYS = 3
_LATE_SHIP_EXTRA_DAYS = 2

_ZONE_DAYS: dict[str, int] = {"west": 2, "north": 3, "east": 5, "south": 5}

# No official 4-bucket zone covers every Indian state/UT. Madhya Pradesh and Chhattisgarh are
# grouped into "west" (nearest geographic fit); the Northeast states are grouped into "east" --
# both call-outs are documented in the design doc for the owner to revisit if wanted.
_STATE_ZONE: dict[str, str] = {
    "jammu and kashmir": "north", "ladakh": "north", "himachal pradesh": "north",
    "punjab": "north", "chandigarh": "north", "uttarakhand": "north",
    "haryana": "north", "delhi": "north", "uttar pradesh": "north", "rajasthan": "north",
    "gujarat": "west", "maharashtra": "west", "goa": "west",
    "dadra and nagar haveli and daman and diu": "west",
    "madhya pradesh": "west", "chhattisgarh": "west",
    "west bengal": "east", "odisha": "east", "bihar": "east", "jharkhand": "east",
    "assam": "east", "sikkim": "east", "arunachal pradesh": "east", "nagaland": "east",
    "manipur": "east", "mizoram": "east", "tripura": "east", "meghalaya": "east",
    "karnataka": "south", "andhra pradesh": "south", "telangana": "south",
    "tamil nadu": "south", "kerala": "south", "puducherry": "south",
    "andaman and nicobar islands": "south", "lakshadweep": "south",
}
# Unknown/missing state -> longest transit. Safer to slightly over-promise than under.
_DEFAULT_ZONE = "south"

# Matches app.agents.order_tracking._is_cancel_eligible's fulfillment-status predicate: these
# two values are the only ones that mean "not yet dispatched" in this codebase.
_UNDISPATCHED_STATUSES = (None, "UNFULFILLED")


@dataclass(frozen=True)
class DeliveryEstimate:
    expected_date: date


def _zone_for(order: Order) -> str:
    state = order.customer.state if order.customer is not None else None
    if not state:
        return _DEFAULT_ZONE
    return _STATE_ZONE.get(state.strip().lower(), _DEFAULT_ZONE)


def _is_delivered(order: Order) -> bool:
    return any(f.delivered_at is not None for f in order.fulfillments)


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def estimate_delivery(order: Order, today: date) -> DeliveryEstimate | None:
    """Compute a formula-based delivery estimate, or None if one cannot/should not be given.

    Returns None when the order is already delivered (nothing to estimate -- the caller shows
    the real delivery info instead) or when order.created_at is missing (a legacy order synced
    before this field existed; guessing from an unknown start point would be worse than no
    estimate).
    """
    if _is_delivered(order):
        return None
    if order.created_at is None:
        return None
    created = _parse_date(order.created_at)
    if created is None:
        return None

    zone_days = _ZONE_DAYS[_zone_for(order)]
    total_days = _PREP_DAYS + zone_days

    undispatched = order.fulfillment_status in _UNDISPATCHED_STATUSES
    if undispatched and (today - created).days > _LATE_SHIP_THRESHOLD_DAYS:
        total_days += _LATE_SHIP_EXTRA_DAYS

    return DeliveryEstimate(expected_date=created + timedelta(days=total_days))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/core/test_delivery_estimate.py -v`
Expected: PASS (all 12 tests).

- [ ] **Step 5: Run the compliance gate**

```
python -m ruff check .
python -m mypy app
```
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/delivery_estimate.py backend/tests/core/test_delivery_estimate.py
git commit -m "feat(core): add formula-based delivery-date estimate"
```

---

### Task 5: Wire into the order-tracking agent

**Files:**
- Modify: `backend/app/agents/order_tracking.py` (system prompt template lines 16-57, `_order_line` lines 107-139)
- Test: `backend/tests/agents/test_order_tracking.py`

**Interfaces:**
- Consumes: `estimate_delivery(order: Order, today: date) -> DeliveryEstimate | None` (Task 4).
- Produces: the order-tracking system prompt now includes an `Estimated delivery: ...` line (when applicable and `"status"` is in `reveal_fields`) — no change to `AgentReply`/`AgentContext` shapes, so no other caller is affected.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/agents/test_order_tracking.py`. First, extend the `_order` test helper (around line 7-23) to accept `customer` and `created_at`:

```python
def _order(
    name: str,
    phone: str,
    fulfillment_status: str | None = None,
    cancelled_at: str | None = None,
    line_items: tuple[LineItem, ...] = (),
    payment_gateway_names: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    fulfillments: tuple[Fulfillment, ...] = (),
    customer: Customer | None = None,
    created_at: str | None = None,
) -> Order:
    return Order(
        gid=f"gid://{name}", name=name, email="c@example.com", phone=phone,
        shipping_phone=None, billing_phone=None, financial_status="paid",
        fulfillment_status=fulfillment_status, cancelled_at=cancelled_at, tags=tags,
        payment_gateway_names=payment_gateway_names, total=None, customer_locale=None,
        line_items=line_items, fulfillments=fulfillments, customer=customer,
        created_at=created_at,
    )
```

Add `Customer` to the existing `from app.shopify.models import ...` import at the top of the file (it currently imports `AuthorizedOrder, Fulfillment, LineItem, Money, Order`).

Then add these tests near the bottom of the file:

```python
def _customer(state: str = "Gujarat") -> Customer:
    return Customer(
        gid="gid://c/1", first_name=None, last_name=None, email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=state,
        postal_code=None, country=None,
    )


async def test_order_line_includes_estimated_delivery_when_computable() -> None:
    provider = _CapturingProvider('{"reply": "ok"}')
    order = _order(
        "tavas1", "+919999999999",
        customer=_customer("Gujarat"), created_at="2026-08-10T00:00:00+00:00",
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "when will my order arrive", [authorized]))
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" in prompt
    assert "estimate" in prompt.lower() and "1-2 days" in prompt.replace("1–2 days", "1-2 days")


async def test_order_line_omits_estimated_delivery_without_created_at() -> None:
    provider = _CapturingProvider('{"reply": "ok"}')
    order = _order("tavas1", "+919999999999", customer=_customer("Gujarat"), created_at=None)
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "when will my order arrive", [authorized]))
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" not in prompt


async def test_order_line_omits_estimated_delivery_when_status_not_revealed() -> None:
    provider = _CapturingProvider('{"reply": "ok"}')
    order = _order(
        "tavas1", "+919999999999",
        customer=_customer("Gujarat"), created_at="2026-08-10T00:00:00+00:00",
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(
        _context(
            provider, "when will my order arrive", [authorized],
            reveal_fields=("order_number",),
        )
    )
    prompt = _system_prompt(provider)
    assert "Estimated delivery:" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/agents/test_order_tracking.py -k estimated_delivery -v`
Expected: FAIL — `"Estimated delivery:"` never appears in the prompt yet.

- [ ] **Step 3: Wire `_order_line`**

In `backend/app/agents/order_tracking.py`, add the import (top of file, alongside the existing `app.shopify.models` import):

```python
from app.core.delivery_estimate import estimate_delivery
```

Also add `from datetime import UTC, datetime` to the existing imports at the top.

Add a small formatting helper right after `_tracking_line` (after line 104):

```python
def _delivery_estimate_line(order: AuthorizedOrder) -> str | None:
    result = estimate_delivery(order.order, today=datetime.now(UTC).date())
    if result is None:
        return None
    return (
        f"  - Estimated delivery: {result.expected_date.isoformat()} "
        "(this is an estimate and may vary by 1-2 days)"
    )
```

In `_order_line` (around lines 133-138), change:

```python
    if "tracking" in reveal_fields:
        # Only fulfillments that actually carry tracking -- never fabricate a line for an
        # unshipped order or a label-only fulfillment with no tracking yet.
        lines.extend(
            _tracking_line(f) for f in order.order.fulfillments if f.has_tracking()
        )
    return "\n".join(lines)
```

to:

```python
    if "tracking" in reveal_fields:
        # Only fulfillments that actually carry tracking -- never fabricate a line for an
        # unshipped order or a label-only fulfillment with no tracking yet.
        lines.extend(
            _tracking_line(f) for f in order.order.fulfillments if f.has_tracking()
        )
    estimate_line = _delivery_estimate_line(order)
    if estimate_line is not None:
        lines.append(estimate_line)
    return "\n".join(lines)
```

(This sits inside the `if "status" not in reveal_fields: return ...` early-return branch already in `_order_line` -- delivery timing is part of the status picture, so it is naturally gated the same way as fulfillment/cancellation state without any extra condition needed.)

- [ ] **Step 4: Update the system prompt's instruction text**

In the `_SYSTEM_TEMPLATE` string (lines 28-33), change:

```
If an order has shipped and tracking details are shown above, share the courier name, tracking
number, and the tracking link exactly as given so the customer can track it. Never invent a
tracking number, courier, or delivery date, and do not estimate an arrival time beyond what the
tracking data states. If no tracking details are available for an order, do not claim it has not
shipped -- go by the fulfillment status field above instead, tell the customer the tracking
details are not available yet, and offer to have the team check.
```

to:

```
If an order has shipped and tracking details are shown above, share the courier name, tracking
number, and the tracking link exactly as given so the customer can track it. Never invent a
tracking number or courier. If no tracking details are available for an order, do not claim it
has not shipped -- go by the fulfillment status field above instead, tell the customer the
tracking details are not available yet, and offer to have the team check.

If an "Estimated delivery" line is shown above for an order, relay it to the customer exactly
as given, including the caveat that it is an estimate and may vary -- never state it as a firm
promised date, and never compute or guess a different delivery date yourself. If no estimated
delivery line is shown for an order, do not invent one.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/agents/test_order_tracking.py -v`
Expected: PASS (full file).

- [ ] **Step 6: Run the full backend test suite + compliance gate**

```
python -m pytest backend -v
python -m ruff check .
python -m mypy app
```
Expected: full suite green, both linters clean.

- [ ] **Step 7: Run the no-secrets compliance grep on every file touched this session**

```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/shopify/models.py backend/app/shopify/client.py backend/app/channels/shopify_orders.py backend/app/store/postgres.py backend/app/core/delivery_estimate.py backend/app/agents/order_tracking.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/agents/order_tracking.py backend/tests/agents/test_order_tracking.py
git commit -m "feat(agents): surface the delivery-date estimate in order-tracking replies"
```

---

## After all tasks: registries + status update

This is documentation, not app code — do it directly, not via the `developer` agent.

- [ ] Update `docs/memory/component_registry.md` and `docs/memory/api_registry.md` with the new `app/core/delivery_estimate.py` module and the `Order.created_at` field addition (follow the existing entry format in each file).
- [ ] Update the "Delivery-date estimation for order-status Q&A" row in `docs/FR/_pipeline_status.md` from "DESIGN APPROVED" to "BUILT" (or "REVIEW" if code-review is run first — this feature touches no sensitive surface per the routing table, so `code-reviewer` is optional but recommended; `security-reviewer` is not required, no credentials/webhooks/mutations/auth/CORS touched).
- [ ] Do NOT mark this pushed — per CLAUDE.md, push only after explicit owner approval.
