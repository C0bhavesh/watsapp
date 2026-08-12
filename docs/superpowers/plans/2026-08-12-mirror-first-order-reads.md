# Database-First Order Reads for Chat Q&A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the WhatsApp bot's order-tracking chat Q&A read from our own database (the
`customers`/`orders`/`order_items` mirror tables) instead of live Shopify calls, with a live
Shopify fallback on a miss or database error, while the Confirm/Cancel mutation path keeps
re-fetching from Shopify exclusively.

**Architecture:** A new adapter, `MirrorOrderSource`, implements the existing `OrderSource`
structural interface that `app/core/order_resolver.py` already depends on. It tries our database
first (three new `IngestStore` read methods) and falls through to the wrapped live Shopify
`OrderSource` on a miss or any exception. It is wired in at exactly one place —
`app/core/conversation.py`'s `_agent_reply` — which is the only file whose behavior changes.
`app/core/order_actions.py` (the Confirm/Cancel dispatcher) is not touched.

**Tech Stack:** Python 3.12+, FastAPI, asyncpg (Postgres), pytest + pytest-asyncio, mypy strict,
ruff.

## Global Constraints

- Critical Rule 2 (LLM never mutates) — untouched; no new Shopify write paths.
- Critical Rule 3 (always re-fetch live from Shopify before any mutation; ownership check before
  revealing anything) — the Confirm/Cancel mutation path (`app/core/order_actions.py`,
  `resolve_by_gid`) is NOT modified by any task in this plan and keeps receiving the real
  Shopify client directly.
- No new secrets, no new admin-panel config, no schema/migration changes — the mirror tables
  already exist (`app/store/schema.sql`) and are already populated.
- Full type hints on every function signature; `mypy app` strict must stay clean.
- `ruff check .` must stay clean (whole project, including `tests/`).
- No `print()` in app code — use `logging`.
- No bare `except:` — catch specific exceptions (or `Exception` only where this codebase already
  has that precedent for a documented "degrade, never break the caller" boundary, as in
  `app/channels/shopify_webhook.py`'s `_mirror_order`/`_mirror_customer`).
- Compliance grep after writing any file in `app/` (must return EMPTY):
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>`
- Interpreter for this machine (per `docs/memory/error_learnings.md`, 2026-08-06 entry):
  `C:\Users\cbbha\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` —
  run everything as `"$PY" -m pytest|ruff|mypy` from `backend/`. Verify `slowapi`/`litellm` are
  installed in it before starting (`"$PY" -m pip show slowapi litellm`); if missing, install per
  that error_learnings entry.

---

### Task 1: `IngestStore` mirror-read methods — Protocol + in-memory implementation

**Files:**
- Modify: `backend/app/store/base.py` (add 3 method signatures to the `IngestStore` `Protocol`)
- Modify: `backend/app/store/memory.py` (implement them on `InMemoryIngestStore`)
- Test: `backend/tests/store/test_order_mirror.py` (append; this file already holds the
  `_customer`/`_order` fixtures and the mirror-table test suite)

**Interfaces:**
- Consumes: `app.shopify.models.Order`, `Customer`, `normalize_order_name` (existing).
- Produces: `IngestStore.get_mirrored_order(gid: str) -> Order | None`,
  `IngestStore.find_mirrored_order_by_name(raw_name: str) -> Order | None`,
  `IngestStore.find_mirrored_orders_by_phone(phone_e164: str) -> list[Order]` — Task 2
  (Postgres) and Task 3 (`MirrorOrderSource`) both consume these exact names/signatures.

- [ ] **Step 1: Read current state**

Read `backend/app/store/base.py` in full (the `IngestStore` `Protocol`, right after
`customer_exists`) and `backend/app/store/memory.py`'s `InMemoryIngestStore` class (particularly
`__init__`'s `self.orders`/`self.customers`/`self.order_items` dicts, `upsert_order_mirror`, and
`delete_by_phone`'s existing phone-matching predicate
`phone_e164 in (o.phone, o.shipping_phone, o.billing_phone)` — reuse that exact predicate for
consistency).

- [ ] **Step 2: Write the failing tests**

Append to `backend/tests/store/test_order_mirror.py` (uses the file's existing `_order`/
`_customer` fixture helpers and `InMemoryIngestStore` import already present):

```python
async def test_get_mirrored_order_returns_stored_order() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order())
    result = await store.get_mirrored_order("gid://shopify/Order/1")
    assert result is not None
    assert result.name == "tavas3733"


async def test_get_mirrored_order_missing_returns_none() -> None:
    store = InMemoryIngestStore()
    result = await store.get_mirrored_order("gid://shopify/Order/missing")
    assert result is None


async def test_find_mirrored_order_by_name_normalizes_bare_digits() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_order(name="tavas3733"))
    result = await store.find_mirrored_order_by_name("3733")
    assert result is not None
    assert result.gid == "gid://shopify/Order/1"


async def test_find_mirrored_order_by_name_miss_returns_none() -> None:
    store = InMemoryIngestStore()
    result = await store.find_mirrored_order_by_name("tavas000000000")
    assert result is None


async def test_find_mirrored_orders_by_phone_matches_any_of_three_columns() -> None:
    store = InMemoryIngestStore()
    phone = "+919876500000"
    await store.upsert_order_mirror(
        _order(gid="gid://a", phone=None, shipping_phone=phone, billing_phone=None)
    )
    await store.upsert_order_mirror(
        _order(gid="gid://b", phone=None, shipping_phone=None, billing_phone=phone)
    )
    results = await store.find_mirrored_orders_by_phone(phone)
    assert {o.gid for o in results} == {"gid://a", "gid://b"}


async def test_find_mirrored_orders_by_phone_no_match_returns_empty() -> None:
    store = InMemoryIngestStore()
    results = await store.find_mirrored_orders_by_phone("+919000000000")
    assert results == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && "$PY" -m pytest tests/store/test_order_mirror.py -k mirrored_order -v`
Expected: FAIL with `AttributeError: 'InMemoryIngestStore' object has no attribute
'get_mirrored_order'` (and similarly for the other two new methods).

- [ ] **Step 4: Add the Protocol signatures**

In `backend/app/store/base.py`, inside the `IngestStore` `Protocol`, immediately after
`async def customer_exists(self, gid: str) -> bool: ...`, add:

```python
    async def get_mirrored_order(self, gid: str) -> Order | None: ...

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]: ...
```

- [ ] **Step 5: Implement on `InMemoryIngestStore`**

In `backend/app/store/memory.py`, change the import line (currently
`from app.shopify.models import Customer, LineItem, Order`) to also import
`normalize_order_name`:

```python
from app.shopify.models import Customer, LineItem, Order, normalize_order_name
```

Then, in `InMemoryIngestStore`, immediately after `customer_exists`, add:

```python
    async def get_mirrored_order(self, gid: str) -> Order | None:
        return self.orders.get(gid)

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        for order in self.orders.values():
            if order.name == name:
                return order
        return None

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if phone_e164 in (o.phone, o.shipping_phone, o.billing_phone)
        ]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && "$PY" -m pytest tests/store/test_order_mirror.py -v`
Expected: PASS, all tests in the file (existing + 6 new).

- [ ] **Step 7: Run full suite, ruff, mypy**

Run: `cd backend && "$PY" -m pytest -q`
Expected: same pass/skip counts as before this task, plus 6 new passes.

Run: `cd backend && "$PY" -m ruff check .`
Expected: All checks passed!

Run: `cd backend && "$PY" -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 8: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/store/base.py app/store/memory.py
```
Expected: EMPTY output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/store/base.py backend/app/store/memory.py backend/tests/store/test_order_mirror.py
git commit -m "feat(store): add mirror-read methods to IngestStore + in-memory impl"
```

---

### Task 2: `IngestStore` mirror-read methods — Postgres implementation

**Files:**
- Modify: `backend/app/store/postgres.py` (implement the 3 methods on `PostgresIngestStore`,
  plus shared row-mapping helpers)
- Test: `backend/tests/store/test_order_mirror.py` (append; Postgres-gated, same file as Task 1)

**Interfaces:**
- Consumes: `IngestStore.get_mirrored_order`/`find_mirrored_order_by_name`/
  `find_mirrored_orders_by_phone` signatures from Task 1. The `orders`/`customers`/`order_items`
  schema (`backend/app/store/schema.sql`) and the exact column set `upsert_order_mirror`/
  `_upsert_customer_on_conn` already write (verify by reading them — do not re-derive from
  scratch).
- Produces: same three method names as Task 1, now also implemented by
  `PostgresIngestStore` — Task 3 does not care which store implementation it talks to (both
  satisfy the same structural shape).

- [ ] **Step 1: Read current state**

Read `backend/app/store/postgres.py` in full — particularly its imports, `_e164`,
`_parse_timestamp`, `_order_number_from_name`, `MAX_MIRROR_LINE_ITEMS`, `_upsert_customer_on_conn`,
and `upsert_order_mirror` (the exact column list/order it writes — the new read queries must
select the same columns). Read `backend/app/store/schema.sql`'s `customers`/`orders`/
`order_items` table definitions (lines ~116–170) to confirm column names and types (`total_amount`/
`total_currency`/`price_amount`/`price_currency` are all `text`; `cancelled_at`/`updated_at` are
`timestamptz`, so asyncpg returns `datetime` objects that must be converted back to ISO strings
for the `Order`/`Customer` dataclasses, which store them as `str | None`).

- [ ] **Step 2: Write the failing tests**

Append to `backend/tests/store/test_order_mirror.py` (uses the existing `pool` fixture,
`PostgresIngestStore` import already present, and `uuid` already imported):

```python
@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_get_mirrored_order_pg_returns_full_order_with_items_and_customer(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    customer_gid = f"gid://shopify/Customer/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, customer=_customer(gid=customer_gid)))

    result = await store.get_mirrored_order(gid)

    assert result is not None
    assert result.gid == gid
    assert result.name == "tavas3733"
    assert len(result.line_items) == 1
    assert result.line_items[0].title == "Blue Kurti"
    assert result.customer is not None
    assert result.customer.first_name == "Suman"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_get_mirrored_order_pg_missing_gid_returns_none(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    result = await store.get_mirrored_order(f"gid://shopify/Order/{uuid.uuid4()}")
    assert result is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_get_mirrored_order_pg_no_customer_returns_none_customer(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, customer=None))

    result = await store.get_mirrored_order(gid)

    assert result is not None
    assert result.customer is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_order_by_name_pg_normalizes_bare_digits(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(_order(gid=gid, name="tavas3733", customer=None))

    result = await store.find_mirrored_order_by_name("3733")

    assert result is not None
    assert result.gid == gid


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_order_by_name_pg_miss_returns_none(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    result = await store.find_mirrored_order_by_name("tavas000000000")
    assert result is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_orders_by_phone_pg_matches_any_of_three_columns(
    pool: LazyPool,
) -> None:
    store = PostgresIngestStore(pool)
    phone = "+919876500000"
    gid_ship = f"gid://shopify/Order/{uuid.uuid4()}"
    gid_bill = f"gid://shopify/Order/{uuid.uuid4()}"
    await store.upsert_order_mirror(
        _order(gid=gid_ship, phone=None, shipping_phone=phone, billing_phone=None, customer=None)
    )
    await store.upsert_order_mirror(
        _order(gid=gid_bill, phone=None, shipping_phone=None, billing_phone=phone, customer=None)
    )

    results = await store.find_mirrored_orders_by_phone(phone)

    assert {o.gid for o in results} == {gid_ship, gid_bill}


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_find_mirrored_orders_by_phone_pg_no_match_returns_empty(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    results = await store.find_mirrored_orders_by_phone("+919000000000")
    assert results == []
```

- [ ] **Step 3: Run tests to verify they fail (or skip cleanly)**

Run: `cd backend && "$PY" -m pytest tests/store/test_order_mirror.py -k mirrored_order_pg -v`
Expected: without `TEST_DATABASE_URL` set, all SKIPPED. With it set, FAIL with
`AttributeError: 'PostgresIngestStore' object has no attribute 'get_mirrored_order'`.

- [ ] **Step 4: Implement the shared row-mapping helpers and the 3 methods**

In `backend/app/store/postgres.py`, change the imports line (currently
`from app.shopify.models import Customer, Order`) to:

```python
from app.shopify.models import Customer, LineItem, Money, Order, normalize_order_name
```

Add these module-level items right after the `MAX_MIRROR_LINE_ITEMS` constant:

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


def _order_from_row(row: asyncpg.Record, items: list[LineItem]) -> Order:
    customer = None
    if row["c_gid"] is not None:
        customer = Customer(
            gid=row["c_gid"], first_name=row["c_first_name"], last_name=row["c_last_name"],
            email=row["c_email"], phone=row["c_phone"], address_line1=row["c_address_line1"],
            address_line2=row["c_address_line2"], city=row["c_city"], state=row["c_state"],
            postal_code=row["c_postal_code"], country=row["c_country"],
            updated_at=row["c_updated_at"].isoformat() if row["c_updated_at"] else None,
        )
    total = None
    if row["total_amount"] is not None:
        total = Money(amount=row["total_amount"], currency=row["total_currency"])
    return Order(
        gid=row["gid"], name=row["name"], email=row["email"], phone=row["phone"],
        shipping_phone=row["shipping_phone"], billing_phone=row["billing_phone"],
        financial_status=row["financial_status"], fulfillment_status=row["fulfillment_status"],
        cancelled_at=row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
        tags=tuple(row["tags"] or ()),
        payment_gateway_names=tuple(row["payment_gateway_names"] or ()),
        total=total, customer_locale=row["customer_locale"],
        line_items=tuple(items), customer=customer,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )


async def _fetch_mirror_line_items(conn: asyncpg.Connection, order_gid: str) -> list[LineItem]:
    rows = await conn.fetch(
        "SELECT title, sku, quantity, variant_title, price_amount, price_currency "
        "FROM order_items WHERE order_gid = $1",
        order_gid,
    )
    return [
        LineItem(
            title=r["title"], quantity=r["quantity"], variant_title=r["variant_title"],
            price=Money(r["price_amount"], r["price_currency"]) if r["price_amount"] else None,
            sku=r["sku"],
        )
        for r in rows
    ]
```

Then, in `PostgresIngestStore`, immediately after `customer_exists`, add:

```python
    async def get_mirrored_order(self, gid: str) -> Order | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_MIRROR_ORDER_SELECT + "WHERE o.gid = $1", gid)
            if row is None:
                return None
            items = await _fetch_mirror_line_items(conn, gid)
        return _order_from_row(row, items)

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_MIRROR_ORDER_SELECT + "WHERE o.name = $1", name)
            if row is None:
                return None
            items = await _fetch_mirror_line_items(conn, str(row["gid"]))
        return _order_from_row(row, items)

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                _MIRROR_ORDER_SELECT
                + "WHERE o.phone = $1 OR o.shipping_phone = $1 OR o.billing_phone = $1",
                phone_e164,
            )
            orders = []
            for row in rows:
                items = await _fetch_mirror_line_items(conn, str(row["gid"]))
                orders.append(_order_from_row(row, items))
        return orders
```

- [ ] **Step 5: Confirm the tests pass**

Run (requires `TEST_DATABASE_URL` set to a real Postgres instance with the schema applied via
`python -m scripts.apply_schema`):
`cd backend && TEST_DATABASE_URL=<dsn> "$PY" -m pytest tests/store/test_order_mirror.py -v`
Expected: PASS, all tests including the 7 new ones. Without `TEST_DATABASE_URL` set, they SKIP
cleanly (confirm with `cd backend && "$PY" -m pytest tests/store/test_order_mirror.py -v` and
check for `SKIPPED` markers, not errors).

- [ ] **Step 6: Run full suite, ruff, mypy**

Run: `cd backend && "$PY" -m pytest -q`
Expected: same pass count as after Task 1, plus 7 more passes (or skips, if no
`TEST_DATABASE_URL`).

Run: `cd backend && "$PY" -m ruff check .`
Expected: All checks passed!

Run: `cd backend && "$PY" -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 7: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/store/postgres.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/store/postgres.py backend/tests/store/test_order_mirror.py
git commit -m "feat(store): implement mirror-read methods on PostgresIngestStore"
```

---

### Task 3: `MirrorOrderSource` adapter

**Files:**
- Create: `backend/app/core/mirror_order_source.py`
- Test: `backend/tests/core/test_mirror_order_source.py` (new file)

**Interfaces:**
- Consumes: `app.core.order_resolver.OrderSource` (existing `Protocol`), the three
  `get_mirrored_order`/`find_mirrored_order_by_name`/`find_mirrored_orders_by_phone` method
  names from Tasks 1–2 (via a new narrow `Protocol` defined in this file, NOT the full
  `IngestStore` — see Step 4 for why).
- Produces: `MirrorOrderSource(ingest, shopify)` — a class satisfying `OrderSource` structurally.
  Task 4 constructs one per turn as `MirrorOrderSource(c.ingest, c.shopify)`.

- [ ] **Step 1: Read current state**

Read `backend/app/core/order_resolver.py` in full — particularly the `OrderSource` `Protocol`
definition (3 methods: `get_order`, `find_order_by_name`, `find_customer_orders_by_phone`) and
how `resolve_by_phone`/`resolve_by_gid`/`resolve_by_order_name` consume it. Read
`backend/tests/core/test_order_resolver.py`'s `_FakeShopify` class (the existing test-double
pattern for `OrderSource`) to match its style.

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/core/test_mirror_order_source.py`:

```python
from app.core.mirror_order_source import MirrorOrderSource
from app.shopify.models import Order


def _order(gid: str, name: str) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=None, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeMirrorIngest:
    def __init__(
        self,
        order_by_gid: Order | None = None,
        order_by_name: Order | None = None,
        orders_by_phone: list[Order] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.order_by_gid = order_by_gid
        self.order_by_name = order_by_name
        self.orders_by_phone = orders_by_phone or []
        self.raises = raises

    async def get_mirrored_order(self, gid: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.order_by_gid

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.order_by_name

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        if self.raises:
            raise self.raises
        return self.orders_by_phone


class _FakeShopify:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        self.calls.append("get_order")
        return _order(gid, "tavas-from-shopify")

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        self.calls.append("find_order_by_name")
        return _order("gid://shopify-fallback", raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        self.calls.append("find_customer_orders_by_phone")
        return [_order("gid://shopify-fallback", "tavas-fallback")]


async def test_get_order_hit_never_calls_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_gid=_order("gid://1", "tavas1"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas1"
    assert shopify.calls == []


async def test_get_order_miss_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_gid=None)
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas-from-shopify"
    assert shopify.calls == ["get_order"]


async def test_get_order_db_error_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(raises=RuntimeError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas-from-shopify"
    assert shopify.calls == ["get_order"]


async def test_find_order_by_name_hit_never_calls_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_name=_order("gid://2", "tavas2"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_order_by_name("tavas2")

    assert result is not None
    assert result.gid == "gid://2"
    assert shopify.calls == []


async def test_find_order_by_name_miss_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_name=None)
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_order_by_name("tavas2")

    assert result is not None
    assert shopify.calls == ["find_order_by_name"]


async def test_find_order_by_name_db_error_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(raises=RuntimeError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_order_by_name("tavas2")

    assert result is not None
    assert shopify.calls == ["find_order_by_name"]


async def test_find_customer_orders_by_phone_hit_never_calls_shopify() -> None:
    ingest = _FakeMirrorIngest(orders_by_phone=[_order("gid://3", "tavas3")])
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_customer_orders_by_phone("+919999999999")

    assert len(result) == 1
    assert result[0].gid == "gid://3"
    assert shopify.calls == []


async def test_find_customer_orders_by_phone_empty_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(orders_by_phone=[])
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_customer_orders_by_phone("+919999999999")

    assert len(result) == 1
    assert shopify.calls == ["find_customer_orders_by_phone"]


async def test_find_customer_orders_by_phone_db_error_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(raises=RuntimeError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_customer_orders_by_phone("+919999999999")

    assert len(result) == 1
    assert shopify.calls == ["find_customer_orders_by_phone"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && "$PY" -m pytest tests/core/test_mirror_order_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.mirror_order_source'`.

- [ ] **Step 4: Implement `MirrorOrderSource`**

Create `backend/app/core/mirror_order_source.py`:

```python
"""Adapter over OrderSource that answers order-tracking chat Q&A from our own database mirror
(customers/orders/order_items), falling back to a live Shopify call on a miss or any database
error.

Used ONLY by the Q&A pipeline (core/conversation.py). The Confirm/Cancel mutation path
(core/order_actions.py, resolve_by_gid) keeps talking to the real Shopify client directly and
never sees this class -- Critical Rule 3 (always re-fetch live before any mutation) applies only
to that path, and this module does not touch it.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from app.core.order_resolver import OrderSource
from app.shopify.models import Order

logger = logging.getLogger(__name__)

T = TypeVar("T")


class MirrorReadSource(Protocol):
    """The narrow slice of IngestStore this adapter needs -- not the full Protocol, so a test
    double only has to implement these three methods to be usable here. The real IngestStore
    (Postgres or in-memory) already satisfies this structurally once Tasks 1-2 land."""

    async def get_mirrored_order(self, gid: str) -> Order | None: ...

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]: ...


class MirrorOrderSource:
    """Structurally satisfies OrderSource. Database first, live Shopify as the fallback."""

    def __init__(self, ingest: MirrorReadSource, shopify: OrderSource) -> None:
        self._ingest = ingest
        self._shopify = shopify

    async def get_order(self, gid: str) -> Order | None:
        order = await self._safe(self._ingest.get_mirrored_order, gid)
        if order is not None:
            return order
        return await self._shopify.get_order(gid)

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        order = await self._safe(self._ingest.find_mirrored_order_by_name, raw_name)
        if order is not None:
            return order
        return await self._shopify.find_order_by_name(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        orders = await self._safe(self._ingest.find_mirrored_orders_by_phone, phone_e164)
        if orders:
            return orders
        return await self._shopify.find_customer_orders_by_phone(phone_e164)

    async def _safe(self, fn: Callable[..., Awaitable[T]], *args: object) -> T | None:
        # Any database error degrades to "treat it as a miss" -- same posture as
        # _mirror_order/_mirror_customer in channels/shopify_webhook.py: an infra hiccup on this
        # read path must never break the customer's turn, it just costs one extra Shopify
        # round-trip.
        try:
            return await fn(*args)
        except Exception:
            logger.exception("mirror order-source read failed; falling back to Shopify")
            return None
```

- [ ] **Step 5: Confirm the tests pass**

Run: `cd backend && "$PY" -m pytest tests/core/test_mirror_order_source.py -v`
Expected: PASS, all 9 tests.

- [ ] **Step 6: Run full suite, ruff, mypy**

Run: `cd backend && "$PY" -m pytest -q`
Expected: same pass count as after Task 2, plus 9 more passes.

Run: `cd backend && "$PY" -m ruff check .`
Expected: All checks passed!

Run: `cd backend && "$PY" -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 7: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/core/mirror_order_source.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/mirror_order_source.py backend/tests/core/test_mirror_order_source.py
git commit -m "feat(core): add MirrorOrderSource adapter (database-first, Shopify fallback)"
```

---

### Task 4: Wire `MirrorOrderSource` into the Q&A pipeline

**Files:**
- Modify: `backend/app/core/conversation.py:297-326` (`_agent_reply`)
- Test: `backend/tests/core/test_conversation.py` (append)

**Interfaces:**
- Consumes: `MirrorOrderSource(ingest, shopify)` from Task 3.
- Produces: no new public interface — this is the behavior-change task. After this task,
  `resolve_by_phone` and `_recover_order_by_name` (via `resolve_by_order_name`) receive a
  `MirrorOrderSource` instead of `c.shopify` directly, for `order_tracking` turns only.

- [ ] **Step 1: Read current state**

Read `backend/app/core/conversation.py` in full, particularly `_agent_reply` (currently lines
297-326) — the two call sites that pass `c.shopify` (`resolve_by_phone(c.shopify, c.ingest,
event.wa_id)` and `_recover_order_by_name(c.shopify, event.wa_id, event.text)`). Confirm
`backend/app/core/order_actions.py:136`'s `resolve_by_gid(c.shopify, event.wa_id, gid)` call is
the ONLY other `OrderSource` call site in the codebase and is not part of this task — it must
stay exactly as-is. Read `backend/app/deps.py`'s `Container` class and `get_container`/
`reset_container` functions (the test in Step 2 monkeypatches attributes on the real container
singleton, matching the existing pattern used in `backend/tests/test_whatsapp_webhook.py`, e.g.
`monkeypatch.setattr(get_container(), "shopify", fake)`).

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/core/test_conversation.py` (this file already imports `Order` from
`app.shopify.models` and defines a local `_order` helper — reuse both, do not redefine).

First, change the file's existing import line (currently
`from app.core.conversation import _extract_order_number_candidate, _recover_order_by_name`) to
also import `_agent_reply`:

```python
from app.core.conversation import (
    _agent_reply,
    _extract_order_number_candidate,
    _recover_order_by_name,
)
```

Then add these new imports alongside the file's existing ones (`import logging`, `import
pytest`, the `app.core.conversation`/`app.shopify.models` lines already there):

```python
from app.admin.controls import AdminControls
from app.agents.base import AgentReply
from app.channels.whatsapp_inbound import InboundText
from app.deps import get_container, reset_container
from app.store.base import MappingView


class _FakeMirrorIngestFull:
    """Implements every IngestStore method _agent_reply's own code path touches directly
    (count_orders_by_phone) plus what resolve_by_phone/MirrorOrderSource need."""

    def __init__(
        self,
        mappings: list[MappingView] | None = None,
        mirrored_order: Order | None = None,
    ) -> None:
        self.mappings = mappings or []
        self.mirrored_order = mirrored_order
        self.mirror_calls: list[str] = []

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        return [m for m in self.mappings if m.phone_e164 == phone_e164][:limit]

    async def count_orders_by_phone(self, phone_e164: str) -> int:
        return len([m for m in self.mappings if m.phone_e164 == phone_e164])

    async def get_mirrored_order(self, gid: str) -> Order | None:
        self.mirror_calls.append("get_mirrored_order")
        return self.mirrored_order

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        self.mirror_calls.append("find_mirrored_order_by_name")
        return None

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        self.mirror_calls.append("find_mirrored_orders_by_phone")
        return []


class _PoisonedShopify:
    """Raises if the Q&A path ever falls through to it -- proves the mirror served the hit
    without touching Shopify at all."""

    async def get_order(self, gid: str) -> Order | None:
        raise AssertionError("Shopify.get_order must not be called on a mirror hit")

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        return None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


async def test_agent_reply_order_tracking_reads_from_mirror_not_shopify(monkeypatch) -> None:
    order = _order("gid://1", "tavas3733", "+919999999999")
    mapping = MappingView(
        order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        status="pending", is_cod=False, created_at=None,
    )
    ingest = _FakeMirrorIngestFull(mappings=[mapping], mirrored_order=order)
    shopify = _PoisonedShopify()

    reset_container()
    c = get_container()
    monkeypatch.setattr(c, "ingest", ingest)
    monkeypatch.setattr(c, "shopify", shopify)

    captured: dict[str, object] = {}

    async def fake_classify_intent(*args: object, **kwargs: object) -> str:
        return "order_tracking"

    async def fake_assemble_all(self: object) -> dict[str, str]:
        return {}

    async def fake_run_agent(context: object, intent: str, container: object) -> AgentReply:
        captured["orders"] = context.orders  # type: ignore[attr-defined]
        return AgentReply(text="ok", handoff=False)

    monkeypatch.setattr("app.core.conversation.classify_intent", fake_classify_intent)
    monkeypatch.setattr(
        "app.core.conversation.KnowledgeLoader.assemble_all", fake_assemble_all
    )
    monkeypatch.setattr("app.core.conversation._run_agent", fake_run_agent)

    event = InboundText(
        message_id="wamid.1", wa_id="919999999999", text="where is my order",
        timestamp="1699999999",
    )

    await _agent_reply(
        c, event, [], "+919999999999", False, (object(), "model", "key", None), AdminControls()
    )

    resolved = captured["orders"]
    assert len(resolved) == 1  # type: ignore[arg-type]
    assert resolved[0].order.gid == "gid://1"  # type: ignore[index]
    assert ingest.mirror_calls == ["get_mirrored_order", "find_mirrored_order_by_name"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && "$PY" -m pytest tests/core/test_conversation.py -k reads_from_mirror -v`
Expected: FAIL — `_PoisonedShopify.get_order` raises `AssertionError`, because `_agent_reply`
currently passes `c.shopify` (the poisoned fake) directly into `resolve_by_phone`.

- [ ] **Step 4: Wire in `MirrorOrderSource`**

In `backend/app/core/conversation.py`, add to the imports (alongside the existing
`from app.core.order_resolver import ...` line):

```python
from app.core.mirror_order_source import MirrorOrderSource
```

In `_agent_reply`, change:

```python
    orders: list[AuthorizedOrder] = []
    order_number_format_hint: str | None = None
    if intent == "order_tracking":
        orders = await resolve_by_phone(c.shopify, c.ingest, event.wa_id)
        # Always attempted, not only when the phone path found nothing: a customer can own
        # more than one order, or ask about one placed under different contact info.
        # Ownership re-checked, live-refetched, non-enumerable inside resolve_by_order_name.
        extra_orders, order_number_format_hint = await _recover_order_by_name(
            c.shopify, event.wa_id, event.text
        )
```

to:

```python
    orders: list[AuthorizedOrder] = []
    order_number_format_hint: str | None = None
    if intent == "order_tracking":
        # Q&A reads from our database first (near-real-time via the orders/create,
        # orders/updated, customers/update webhooks), falling back to live Shopify on a miss
        # or database error. This does NOT apply to the Confirm/Cancel mutation path
        # (order_actions.py's resolve_by_gid) -- that keeps re-fetching from Shopify directly,
        # per Critical Rule 3.
        order_source = MirrorOrderSource(c.ingest, c.shopify)
        orders = await resolve_by_phone(order_source, c.ingest, event.wa_id)
        # Always attempted, not only when the phone path found nothing: a customer can own
        # more than one order, or ask about one placed under different contact info.
        # Ownership re-checked, live-refetched, non-enumerable inside resolve_by_order_name.
        extra_orders, order_number_format_hint = await _recover_order_by_name(
            order_source, event.wa_id, event.text
        )
```

- [ ] **Step 5: Confirm the test passes**

Run: `cd backend && "$PY" -m pytest tests/core/test_conversation.py -v`
Expected: PASS, all tests in the file (existing `_recover_order_by_name`/
`_extract_order_number_candidate` tests + the new one).

- [ ] **Step 6: Confirm the mutation path is unaffected**

Run: `cd backend && "$PY" -m pytest tests/core/test_button_dispatch.py -v`
Expected: PASS, unchanged — this is the regression guard proving `order_actions.py`'s
`resolve_by_gid` still only ever receives the real Shopify client (this task made no changes to
`order_actions.py`, so a pass here confirms nothing leaked).

- [ ] **Step 7: Run full suite, ruff, mypy**

Run: `cd backend && "$PY" -m pytest -q`
Expected: same pass count as after Task 3, plus 1 more pass. No new failures anywhere else in
the suite (any of the 76 pre-existing unrelated failures noted in earlier work stay exactly as
they were — this task does not touch their files).

Run: `cd backend && "$PY" -m ruff check .`
Expected: All checks passed!

Run: `cd backend && "$PY" -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 8: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/core/conversation.py
```
Expected: EMPTY output.

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/conversation.py backend/tests/core/test_conversation.py
git commit -m "feat(core): read order-tracking Q&A from the database mirror, Shopify as fallback"
```

---

### Task 5: Update memory/registries

**Files:**
- Modify: `docs/memory/component_registry.md`
- Modify: `docs/memory/error_learnings.md` (only if Tasks 1-4 surfaced a non-obvious issue not
  already captured — check first, do not add a redundant entry)

- [ ] **Step 1: Read current state**

Read `docs/memory/component_registry.md` in full to find where `order_resolver.py`,
`IngestStore`, and `conversation.py` are documented, and match the existing entry style/format.

- [ ] **Step 2: Add registry entries**

Add an entry for `app/core/mirror_order_source.py` (new module: `MirrorOrderSource`, database-
first `OrderSource` adapter with live-Shopify fallback, used only by the Q&A pipeline). Update
the existing `IngestStore` entry to mention the 3 new mirror-read methods
(`get_mirrored_order`/`find_mirrored_order_by_name`/`find_mirrored_orders_by_phone`). Update the
existing `conversation.py`/`_agent_reply` entry to note it now resolves `order_tracking` orders
via `MirrorOrderSource`, not the raw Shopify client directly.

- [ ] **Step 3: Commit**

```bash
git add docs/memory/component_registry.md docs/memory/error_learnings.md
git commit -m "docs: update registries for MirrorOrderSource"
```
