# Exchange Guide Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a customer asks to exchange an item, a new `exchange` specialist agent checks eligibility (delivered within 48 hours, order not cancelled), collects the size they want (never color/product), creates a tracked exchange request, replies with the store's 4-step process, and can later answer "where is my exchange" from that record — visible and editable in the admin chat page's order panel.

**Architecture:** New `app/agents/exchange.py` specialist, routed to by a new `exchange` intent in `app/agents/router.py` (split out of the existing `policy` intent, same way delivery-timing was split out of `policy` earlier). Eligibility is computed by a pure function (`app/core/exchange_eligibility.py`), never by the LLM. A new `ExchangeStore` port (Postgres + in-memory) persists requests in a new `exchange_requests` table. The agent's structured JSON reply carries an optional `create_exchange` field — deterministic Python code in `exchange.py` validates it against the real eligibility fact before writing to the store, mirroring exactly how `handoff: true` already triggers a real side effect without the LLM touching anything directly.

**Tech Stack:** Python 3.12+, dataclasses, pytest/pytest-asyncio, asyncpg (Postgres), FastAPI (admin endpoint), vanilla JS (admin frontend, no test runner — matches this codebase's existing accepted gap for admin JS).

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean.
- No bare `except:`; catch specific exceptions only.
- `ruff check .` (whole project, including `tests/`) must stay clean.
- No `print()` — use `logging` if logging is ever needed.
- Eligibility is computed in Python and handed to the agent as a fact (`eligible: bool` + `reason: str`) — the LLM must never compute or guess the 48-hour window itself.
- The database write for a new exchange request happens ONLY in deterministic Python code reading a validated `create_exchange` field from the model's own structured JSON reply — never a direct model-triggered mutation, and only for an order that is BOTH named in `context.orders` AND independently re-checked as eligible by `check_exchange_eligibility` (never trust the model's own eligibility claim).
- Scope is SIZE exchange only. A damaged/incorrect-item report is NOT this agent's job — it still routes to `customer_support`, unchanged, since photo/video proof handling does not exist in this codebase yet.
- No live stock check for the requested size (owner-confirmed — resolved manually later in the process).
- No button-tap confirmation step (owner-confirmed deviation from the order Confirm/Cancel pattern — see the design doc's "Mutation-safety note").
- Additive schema only: new `CREATE TABLE IF NOT EXISTS exchange_requests`, no change to any existing table.
- Every new/changed file must pass the no-secrets compliance grep (this feature touches no credentials, but run it anyway per the developer agent's standard checklist).

---

## Reference: existing code this plan builds on

- `app/shopify/models.py` — `Order` (`.is_cancelled()`, `.fulfillments: tuple[Fulfillment, ...]`), `Fulfillment.delivered_at: str | None` (raw ISO-8601, populated only on a live/mirrored Shopify read — reliable for this purpose per this session's own investigation).
- `app/core/delivery_estimate.py` — the existing pattern this plan's eligibility function mirrors: a pure `(Order, now) -> result` function, no I/O, computed once and handed to the agent as text.
- `app/agents/base.py` — `AgentContext` (frozen dataclass all agents receive), `HANDOFF_JSON_CONTRACT` (shared handoff wording — **do NOT use it in `exchange.py`**; per this session's own `error_learnings.md` entry on the `policy.py` contradiction bug, an agent needing extra structured JSON fields beyond `reply`/`handoff` must write its own self-contained final JSON-contract instruction, the same way `customer_support.py` already does, rather than risk a shared fragment silently overriding a local instruction), `extract_json_blob`, `extract_reply_text`, `personality_for`.
- `app/agents/order_tracking.py` — the closest sibling agent: same shape (`_SYSTEM_TEMPLATE` + Python-rendered context block + `run(context)`), same "hand the model a full order list and let it ask which one" disambiguation pattern this plan reuses for `exchange.py`.
- `app/agents/router.py` — `_ROUTER_PROMPT`, `Intent` (currently 5 values), `classify_intent`.
- `app/core/conversation.py` — `_agent_reply` (classifies intent, conditionally resolves `orders` only for `order_tracking` today — this plan widens that condition), `_run_agent` (intent -> agent dispatch), `AgentContext` construction.
- `app/store/base.py` — `ConversationStore` Protocol, the pattern `ExchangeStore` mirrors.
- `app/store/postgres.py` / `app/store/memory.py` — `PostgresConversationStore` / in-memory sibling, the implementation pattern `PostgresExchangeStore` / `InMemoryExchangeStore` mirror.
- `app/deps.py` — `Container` dataclass + `get_container()`, where `exchanges: ExchangeStore` gets wired in.
- `app/admin/router.py` — `_order_summary(order: Order) -> dict[str, object]` (already backs the chat page's order-details panel), `get_conversation_thread` (calls it), `ManualReplyRequest`/`send_manual_reply` (the admin-mutation-endpoint pattern this plan's new endpoint mirrors).
- `app/admin/static/chats.js` — `renderOrderDetail(order)` (renders the order panel this plan adds an "Exchange" section to).

---

### Task 1: Exchange data model + eligibility function

**Files:**
- Create: `backend/app/core/exchange_models.py`
- Create: `backend/app/core/exchange_eligibility.py`
- Test: `backend/tests/core/test_exchange_eligibility.py` (new file)

**Interfaces:**
- Produces:
  ```python
  # app/core/exchange_models.py
  ExchangeStatus = Literal[
      "requested", "return_picked_up", "qc_passed", "qc_failed",
      "replacement_dispatched", "delivered",
  ]

  @dataclass(frozen=True)
  class ExchangeRequest:
      id: int
      order_gid: str
      order_name: str
      phone_e164: str
      requested_size: str
      status: ExchangeStatus
      requested_at: str  # ISO-8601
      return_tracking_url: str | None
      updated_at: str  # ISO-8601

  # app/core/exchange_eligibility.py
  @dataclass(frozen=True)
  class ExchangeEligibility:
      eligible: bool
      reason: str  # always set -- the agent relays it verbatim either way

  def check_exchange_eligibility(order: Order, now: datetime) -> ExchangeEligibility: ...
  ```
  Consumed by Task 2 (`ExchangeStore` uses `ExchangeRequest`/`ExchangeStatus`), Task 4 (`exchange.py` uses both).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/core/test_exchange_eligibility.py`:

```python
from datetime import UTC, datetime

from app.core.exchange_eligibility import check_exchange_eligibility
from app.shopify.models import Fulfillment, Order


def _order(
    cancelled_at: str | None = None,
    fulfillments: tuple[Fulfillment, ...] = (),
) -> Order:
    return Order(
        gid="gid://o/1", name="tavas1", email=None, phone=None, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, fulfillments=fulfillments,
    )


def _delivered(at: str) -> Fulfillment:
    return Fulfillment(
        gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
        tracking_number="AWB1", tracking_url="https://track/AWB1", delivered_at=at,
    )


def test_cancelled_order_is_not_eligible() -> None:
    order = _order(cancelled_at="2026-08-10T00:00:00+00:00")
    result = check_exchange_eligibility(order, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert result.eligible is False
    assert "cancelled" in result.reason


def test_undelivered_order_is_not_eligible() -> None:
    order = _order()
    result = check_exchange_eligibility(order, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert result.eligible is False
    assert "not been delivered" in result.reason


def test_delivered_within_window_is_eligible() -> None:
    order = _order(fulfillments=(_delivered("2026-08-10T00:00:00+00:00"),))
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)  # 36 hours later
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is True
    assert "2026-08-10" in result.reason


def test_delivered_exactly_at_the_48_hour_boundary_is_still_eligible() -> None:
    order = _order(fulfillments=(_delivered("2026-08-10T00:00:00+00:00"),))
    now = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)  # exactly 48h later
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is True


def test_delivered_just_past_the_48_hour_boundary_is_not_eligible() -> None:
    order = _order(fulfillments=(_delivered("2026-08-10T00:00:00+00:00"),))
    now = datetime(2026, 8, 12, 0, 1, tzinfo=UTC)  # 48h1m later
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is False
    assert "outside the 48-hour" in result.reason


def test_multiple_fulfillments_uses_the_latest_delivered_at() -> None:
    order = _order(
        fulfillments=(
            _delivered("2026-08-05T00:00:00+00:00"),  # older, would be ineligible alone
            _delivered("2026-08-10T00:00:00+00:00"),  # latest -- this one governs
        ),
    )
    now = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    result = check_exchange_eligibility(order, now=now)
    assert result.eligible is True


def test_unparsable_delivered_at_is_treated_as_not_delivered() -> None:
    order = _order(fulfillments=(_delivered("not-a-real-timestamp"),))
    result = check_exchange_eligibility(order, now=datetime(2026, 8, 10, tzinfo=UTC))
    assert result.eligible is False
    assert "not been delivered" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/core/test_exchange_eligibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.exchange_eligibility'`.

- [ ] **Step 3: Write the data model**

Create `backend/app/core/exchange_models.py`:

```python
"""App-owned exchange-request state -- kept out of app/shopify/models.py, which holds only
Shopify-sourced data. An exchange request has no Shopify counterpart; it is this app's own
record of a size-exchange conversation, tracked through to admin-driven fulfillment (see
docs/superpowers/specs/2026-08-20-exchange-guide-agent-design.md).
"""

from dataclasses import dataclass
from typing import Literal

ExchangeStatus = Literal[
    "requested", "return_picked_up", "qc_passed", "qc_failed",
    "replacement_dispatched", "delivered",
]


@dataclass(frozen=True)
class ExchangeRequest:
    id: int
    order_gid: str
    order_name: str
    phone_e164: str
    requested_size: str
    status: ExchangeStatus
    requested_at: str  # raw ISO-8601, same convention as Order.created_at
    return_tracking_url: str | None
    updated_at: str  # raw ISO-8601
```

- [ ] **Step 4: Write the eligibility function**

Create `backend/app/core/exchange_eligibility.py`:

```python
"""Deterministic size-exchange eligibility check.

Mirrors app/core/delivery_estimate.py's discipline: a pure function of (Order, now), no I/O,
computed once and handed to the agent (app/agents/exchange.py) as an already-decided fact --
the LLM only relays ExchangeEligibility.reason verbatim, it never computes or guesses the
48-hour window itself.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.shopify.models import Order

_EXCHANGE_WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class ExchangeEligibility:
    eligible: bool
    reason: str


def _parse_datetime(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _latest_delivery(order: Order) -> datetime | None:
    parsed = [
        dt for f in order.fulfillments
        if f.delivered_at is not None and (dt := _parse_datetime(f.delivered_at)) is not None
    ]
    return max(parsed) if parsed else None


def check_exchange_eligibility(order: Order, now: datetime) -> ExchangeEligibility:
    """Eligible only for a non-cancelled order delivered within the last 48 hours.

    ``reason`` is always populated, both when eligible and not -- app/agents/exchange.py
    relays it to the customer verbatim rather than composing its own explanation, so the
    exact wording here is what the customer sees.
    """
    if order.is_cancelled():
        return ExchangeEligibility(eligible=False, reason="this order is cancelled.")

    delivered_at = _latest_delivery(order)
    if delivered_at is None:
        return ExchangeEligibility(
            eligible=False, reason="this order has not been delivered yet."
        )

    delivered_date = delivered_at.date().isoformat()
    if now - delivered_at > _EXCHANGE_WINDOW:
        return ExchangeEligibility(
            eligible=False,
            reason=(
                f"delivered on {delivered_date}, which is outside the 48-hour exchange "
                "window."
            ),
        )
    return ExchangeEligibility(
        eligible=True,
        reason=f"delivered on {delivered_date}, within the 48-hour exchange window.",
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/core/test_exchange_eligibility.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 6: Run the compliance gate**

```
python -m ruff check .
python -m mypy app
```
Expected: both clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/exchange_models.py backend/app/core/exchange_eligibility.py backend/tests/core/test_exchange_eligibility.py
git commit -m "feat(core): add exchange data model and 48h eligibility check"
```

---

### Task 2: `ExchangeStore` — schema, protocol, Postgres + in-memory implementations, Container wiring

**Files:**
- Modify: `backend/app/store/schema.sql`
- Modify: `backend/app/store/base.py`
- Modify: `backend/app/store/postgres.py`
- Modify: `backend/app/store/memory.py`
- Modify: `backend/app/deps.py`
- Test: `backend/tests/store/test_exchange_store.py` (new file)

**Interfaces:**
- Consumes: `ExchangeRequest`, `ExchangeStatus` (Task 1).
- Produces:
  ```python
  # app/store/base.py
  class ExchangeStore(Protocol):
      async def create(
          self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
      ) -> ExchangeRequest: ...
      async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]: ...
      async def get(self, id: int) -> ExchangeRequest | None: ...
      async def set_status(self, id: int, status: ExchangeStatus) -> None: ...
      async def set_return_tracking_url(self, id: int, url: str) -> None: ...
  ```
  `PostgresExchangeStore`, `InMemoryExchangeStore` (both implement it). `Container.exchanges: ExchangeStore`. Consumed by Task 4 (`exchange.py`), Task 5 (`conversation.py`), Task 6 (admin endpoints).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/store/test_exchange_store.py`:

```python
import os

import pytest

from app.store.memory import InMemoryExchangeStore
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresExchangeStore

DSN = os.environ.get("TEST_DATABASE_URL", "")


async def test_memory_create_returns_a_requested_row() -> None:
    store = InMemoryExchangeStore()
    row = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    assert row.order_gid == "gid://o/1"
    assert row.order_name == "tavas1"
    assert row.phone_e164 == "+919999999999"
    assert row.requested_size == "M"
    assert row.status == "requested"
    assert row.return_tracking_url is None


async def test_memory_list_for_phone_returns_only_that_phones_requests() -> None:
    store = InMemoryExchangeStore()
    await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.create("gid://o/2", "tavas2", "+918888888888", "L")
    result = await store.list_for_phone("+919999999999")
    assert len(result) == 1
    assert result[0].order_gid == "gid://o/1"


async def test_memory_get_returns_none_for_unknown_id() -> None:
    store = InMemoryExchangeStore()
    assert await store.get(999) is None


async def test_memory_set_status_updates_the_row() -> None:
    store = InMemoryExchangeStore()
    created = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.set_status(created.id, "return_picked_up")
    updated = await store.get(created.id)
    assert updated is not None
    assert updated.status == "return_picked_up"


async def test_memory_set_return_tracking_url_updates_the_row() -> None:
    store = InMemoryExchangeStore()
    created = await store.create("gid://o/1", "tavas1", "+919999999999", "M")
    await store.set_return_tracking_url(created.id, "https://track/abc")
    updated = await store.get(created.id)
    assert updated is not None
    assert updated.return_tracking_url == "https://track/abc"


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_create_and_get_round_trip(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    created = await store.create("gid://o/pg1", "tavas9001", "+919000000001", "S")
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.order_gid == "gid://o/pg1"
    assert fetched.status == "requested"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_list_for_phone_round_trips(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    await store.create("gid://o/pg2", "tavas9002", "+919000000002", "L")
    result = await store.list_for_phone("+919000000002")
    assert len(result) == 1
    assert result[0].order_name == "tavas9002"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
async def test_pg_set_status_and_return_tracking_url_round_trip(pool: LazyPool) -> None:
    store = PostgresExchangeStore(pool)
    created = await store.create("gid://o/pg3", "tavas9003", "+919000000003", "M")
    await store.set_status(created.id, "qc_passed")
    await store.set_return_tracking_url(created.id, "https://track/pg3")
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.status == "qc_passed"
    assert fetched.return_tracking_url == "https://track/pg3"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/store/test_exchange_store.py -v`
Expected: FAIL — `ImportError` (`InMemoryExchangeStore`/`PostgresExchangeStore` don't exist yet). The gated PG tests SKIP if `TEST_DATABASE_URL` is unset in this environment, matching every other gated test in this codebase — that is not a failure to chase.

- [ ] **Step 3: Add the schema**

In `backend/app/store/schema.sql`, append at the end of the file:

```sql
-- Size-exchange requests (2026-08-20, exchange guide agent). App-owned state, no Shopify
-- counterpart. status is a fixed set matching the store's 4-step process message, advanced
-- by the admin panel -- no courier/QC integration exists to auto-advance it.
CREATE TABLE IF NOT EXISTS exchange_requests (
    id                  bigserial PRIMARY KEY,
    order_gid           text NOT NULL,
    order_name          text NOT NULL,
    phone_e164          text NOT NULL,
    requested_size      text NOT NULL,
    status              text NOT NULL DEFAULT 'requested',
    requested_at        timestamptz NOT NULL DEFAULT now(),
    return_tracking_url text,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_requests_order ON exchange_requests (order_gid);
CREATE INDEX IF NOT EXISTS idx_exchange_requests_phone ON exchange_requests (phone_e164);
```

- [ ] **Step 4: Add the `ExchangeStore` protocol**

In `backend/app/store/base.py`, add the import at the top (alongside the other model imports):

```python
from app.core.exchange_models import ExchangeRequest, ExchangeStatus
```

Then add the protocol near the end of the file, after the existing `ConversationStore` protocol:

```python
class ExchangeStore(Protocol):
    """Size-exchange requests: create, look up by customer phone, advance status/tracking.

    See app/core/exchange_eligibility.py for the eligibility check that gates a create() call
    (enforced in app/agents/exchange.py, not here -- this store trusts its caller, same as
    every other store in this codebase).
    """

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest: ...

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]: ...

    async def get(self, id: int) -> ExchangeRequest | None: ...

    async def set_status(self, id: int, status: ExchangeStatus) -> None: ...

    async def set_return_tracking_url(self, id: int, url: str) -> None: ...
```

- [ ] **Step 5: Implement `InMemoryExchangeStore`**

In `backend/app/store/memory.py`, add near the other `InMemory*Store` classes:

```python
class InMemoryExchangeStore:
    def __init__(self) -> None:
        self._rows: dict[int, ExchangeRequest] = {}
        self._next_id = 1

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest:
        now = datetime.now(UTC).isoformat()
        row = ExchangeRequest(
            id=self._next_id, order_gid=order_gid, order_name=order_name,
            phone_e164=phone_e164, requested_size=requested_size, status="requested",
            requested_at=now, return_tracking_url=None, updated_at=now,
        )
        self._rows[row.id] = row
        self._next_id += 1
        return row

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
        return [r for r in self._rows.values() if r.phone_e164 == phone_e164]

    async def get(self, id: int) -> ExchangeRequest | None:
        return self._rows.get(id)

    async def set_status(self, id: int, status: ExchangeStatus) -> None:
        row = self._rows.get(id)
        if row is None:
            return
        self._rows[id] = replace(row, status=status, updated_at=datetime.now(UTC).isoformat())

    async def set_return_tracking_url(self, id: int, url: str) -> None:
        row = self._rows.get(id)
        if row is None:
            return
        self._rows[id] = replace(
            row, return_tracking_url=url, updated_at=datetime.now(UTC).isoformat()
        )
```

Add `ExchangeRequest, ExchangeStatus` to the existing `from app.core.exchange_models import ...` — this is a NEW import, add the line near the top of the file alongside the other `app.*` imports:

```python
from app.core.exchange_models import ExchangeRequest, ExchangeStatus
```

Check the top of `memory.py` for an existing `from dataclasses import ..., replace` and `from datetime import UTC, datetime` import — both are very likely already present (used by other stores in this file); if either is missing, add it rather than re-importing a duplicate name.

- [ ] **Step 6: Implement `PostgresExchangeStore`**

In `backend/app/store/postgres.py`, add near the other `Postgres*Store` classes:

```python
class PostgresExchangeStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO exchange_requests "
                "(order_gid, order_name, phone_e164, requested_size) "
                "VALUES ($1, $2, $3, $4) "
                "RETURNING id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, updated_at",
                order_gid, order_name, phone_e164, requested_size,
            )
        return _exchange_from_row(row)

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, updated_at "
                "FROM exchange_requests WHERE phone_e164 = $1 ORDER BY requested_at DESC",
                phone_e164,
            )
        return [_exchange_from_row(r) for r in rows]

    async def get(self, id: int) -> ExchangeRequest | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, order_gid, order_name, phone_e164, requested_size, status, "
                "requested_at, return_tracking_url, updated_at "
                "FROM exchange_requests WHERE id = $1",
                id,
            )
        return _exchange_from_row(row) if row else None

    async def set_status(self, id: int, status: ExchangeStatus) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE exchange_requests SET status = $1, updated_at = now() WHERE id = $2",
                status, id,
            )

    async def set_return_tracking_url(self, id: int, url: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE exchange_requests SET return_tracking_url = $1, updated_at = now() "
                "WHERE id = $2",
                url, id,
            )
```

Add a module-level row-mapping helper near the other `_x_from_row` helpers in this file:

```python
def _exchange_from_row(row: object) -> ExchangeRequest:
    return ExchangeRequest(
        id=row["id"], order_gid=row["order_gid"], order_name=row["order_name"],
        phone_e164=row["phone_e164"], requested_size=row["requested_size"],
        status=row["status"], requested_at=row["requested_at"].isoformat(),
        return_tracking_url=row["return_tracking_url"],
        updated_at=row["updated_at"].isoformat(),
    )
```

Add the import near the top of `postgres.py`:

```python
from app.core.exchange_models import ExchangeRequest, ExchangeStatus
```

- [ ] **Step 7: Wire `ExchangeStore` into the Container**

In `backend/app/deps.py`, add `ExchangeStore` to the existing `from app.store.base import ...` line:
```python
from app.store.base import ConfigRepo, ConversationStore, ExchangeStore, IngestStore, MessageStore
```

Add to the existing `from app.store.memory import (...)` block:
```python
from app.store.memory import (
    InMemoryConfigRepo,
    InMemoryConversationStore,
    InMemoryExchangeStore,
    InMemoryIngestStore,
    InMemoryMessageStore,
)
```

Add to the existing `from app.store.postgres import (...)` block:
```python
from app.store.postgres import (
    PostgresConfigRepo,
    PostgresConversationStore,
    PostgresExchangeStore,
    PostgresIngestStore,
    PostgresMessageStore,
)
```

Add the field to `Container`:
```python
@dataclass
class Container:
    settings: Settings
    vault: SecretVault
    config_repo: ConfigRepo
    config: ConfigService
    http: httpx.AsyncClient
    tokens: TokenManager
    shopify: ShopifyClient
    ingest: IngestStore
    messages: MessageStore
    conversations: ConversationStore
    exchanges: ExchangeStore
```

In `get_container()`, add to both branches and the final construction:
```python
        if settings.database_url:
            pool = LazyPool(settings.database_url)
            config_repo: ConfigRepo = PostgresConfigRepo(pool)
            ingest: IngestStore = PostgresIngestStore(pool)
            messages: MessageStore = PostgresMessageStore(pool)
            conversations: ConversationStore = PostgresConversationStore(pool)
            exchanges: ExchangeStore = PostgresExchangeStore(pool)
        else:
            config_repo = InMemoryConfigRepo()
            ingest = InMemoryIngestStore()
            messages = InMemoryMessageStore()
            conversations = InMemoryConversationStore()
            exchanges = InMemoryExchangeStore()
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient(follow_redirects=False)  # never replay the token to a redirect
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(
            settings, vault, config_repo, config, http, tokens, shopify, ingest, messages,
            conversations, exchanges,
        )
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest backend/tests/store/test_exchange_store.py -v`
Expected: PASS (5 in-memory tests; the 3 gated PG tests PASS if `TEST_DATABASE_URL` is set in this environment, else SKIP — matching every other gated test in this codebase).

- [ ] **Step 9: Run the full backend test suite + compliance gate**

```
python -m pytest backend -v
python -m ruff check .
python -m mypy app
```
Expected: full suite green (the `deps.py`/`Container` change touches a widely-imported module — watch for any test that constructs a bare `Container(...)` positionally and now needs the new argument; fix forward if so, do not work around it), both linters clean.

- [ ] **Step 10: Run the no-secrets compliance grep**

```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/store/schema.sql backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/app/deps.py
```
Expected: EMPTY output.

- [ ] **Step 11: Commit**

```bash
git add backend/app/store/schema.sql backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/app/deps.py backend/tests/store/test_exchange_store.py
git commit -m "feat(store): add ExchangeStore (Postgres + in-memory) and wire into Container"
```

---

### Task 3: Router — add the `exchange` intent

**Files:**
- Modify: `backend/app/agents/router.py`
- Test: `backend/tests/agents/test_router.py`

**Interfaces:**
- Produces: `Intent` gains a 6th value `"exchange"`. Consumed by Task 5 (`conversation.py`'s dispatch).

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/agents/test_router.py` (check the existing file's imports/fixtures first — mirror whatever provider-stub pattern the file already uses for asserting prompt content, the same style used for the 2026-08-20 `order_tracking`/`policy` split tests already in this file):

```python
def test_router_prompt_routes_exchange_requests_to_the_exchange_intent() -> None:
    assert "exchange:" in _ROUTER_PROMPT
    assert "different size" in _ROUTER_PROMPT


def test_router_prompt_excludes_damaged_incorrect_items_from_exchange() -> None:
    assert "damaged" in _ROUTER_PROMPT.lower()
    assert "customer_support" in _ROUTER_PROMPT


def test_router_prompt_policy_no_longer_claims_actual_exchange_requests() -> None:
    # policy still owns the ABSTRACT exchange-policy question; it must explicitly exclude a
    # customer actually wanting to exchange their own order now that `exchange` exists.
    policy_bullet_start = _ROUTER_PROMPT.index("- policy:")
    exchange_intent_mention = _ROUTER_PROMPT.index("that is `exchange`")
    assert policy_bullet_start < exchange_intent_mention
```

(Import `_ROUTER_PROMPT` from `app.agents.router` at the top of the test file if not already imported — check first, since `test_router_prompt_assigns_order_delivery_timing_to_order_tracking` in this same file already imports and asserts on it per this session's earlier work.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/agents/test_router.py -k exchange -v`
Expected: FAIL — the `exchange:` bullet, the damaged-item exclusion, and the `that is \`exchange\`` phrase don't exist in `_ROUTER_PROMPT` yet.

- [ ] **Step 3: Add the intent**

In `backend/app/agents/router.py`, change:

```python
Intent = Literal[
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
]

_INTENTS: tuple[Intent, ...] = (
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
)
```

to:

```python
Intent = Literal[
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
    "exchange",
]

_INTENTS: tuple[Intent, ...] = (
    "order_tracking",
    "product_search",
    "policy",
    "recommendations",
    "customer_support",
    "exchange",
)
```

Then in `_ROUTER_PROMPT`, change the `policy` bullet from:

```python
- policy: asking about GENERAL store policy in the abstract -- return, exchange, or refund \
rules, COD availability, shipping charges, or whether the store ships to a place. This is for \
policy questions with no specific order in mind -- never the delivery timing of an order the \
customer has already placed (that is order_tracking).
```

to:

```python
- policy: asking about GENERAL store policy in the abstract -- return, exchange, or refund \
rules, COD availability, shipping charges, or whether the store ships to a place. This is for \
policy questions with no specific order in mind -- never the delivery timing of an order the \
customer has already placed (that is order_tracking), and never a customer who actually wants \
to exchange something from an order they placed (that is `exchange`).
```

Add a new bullet right after the `recommendations` bullet, before `customer_support`:

```python
- exchange: the customer wants to actually exchange an item from THEIR OWN order for a \
different size -- not just asking about the exchange policy in the abstract (that is policy). \
A report that an item arrived damaged, defective, or wrong is NOT this -- route those to \
customer_support instead, since checking that needs photo/video proof this bot cannot yet \
collect.
```

Update the final line's count from "five" to "six" if the prompt text literally says a number (check `_ROUTER_PROMPT`'s closing JSON-format line — the existing text reads `"<one of the five categories above>"`); if present, change it to `"<one of the six categories above>"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/agents/test_router.py -v`
Expected: PASS (full file).

- [ ] **Step 5: Run the compliance gate**

```
python -m ruff check .
python -m mypy app
```
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agents/router.py backend/tests/agents/test_router.py
git commit -m "feat(agents): add exchange intent, narrow policy to exclude actual exchange requests"
```

---

### Task 4: `AgentContext.exchange_requests` field + the `exchange` agent

**Files:**
- Modify: `backend/app/agents/base.py`
- Create: `backend/app/agents/exchange.py`
- Test: `backend/tests/agents/test_exchange.py` (new file)

**Interfaces:**
- Consumes: `ExchangeEligibility`/`check_exchange_eligibility` (Task 1), `ExchangeRequest`/`ExchangeStatus` (Task 1), `ExchangeStore` (Task 2), `AgentContext`/`extract_json_blob`/`extract_reply_text`/`personality_for`/`AgentReply` (existing `base.py`).
- Produces:
  ```python
  # app/agents/base.py -- AgentContext gains:
  exchange_requests: list[ExchangeRequest] = field(default_factory=list)

  # app/agents/exchange.py
  async def run(context: AgentContext, exchanges: ExchangeStore) -> AgentReply: ...
  ```
  Consumed by Task 5 (`conversation.py`'s `_run_agent` dispatch and `AgentContext` construction).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/agents/test_exchange.py`:

```python
from app.agents.base import DEFAULT_REVEAL_FIELDS, AgentContext
from app.agents.exchange import run
from app.core.exchange_models import ExchangeRequest
from app.providers.base import CompletionResult, Message
from app.shopify.models import AuthorizedOrder, Fulfillment, Order


class _FakeExchangeStore:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []
        self._next_id = 1

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest:
        self.created.append((order_gid, order_name, phone_e164, requested_size))
        row = ExchangeRequest(
            id=self._next_id, order_gid=order_gid, order_name=order_name,
            phone_e164=phone_e164, requested_size=requested_size, status="requested",
            requested_at="2026-08-20T00:00:00+00:00", return_tracking_url=None,
            updated_at="2026-08-20T00:00:00+00:00",
        )
        self._next_id += 1
        return row

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
        return []

    async def get(self, id: int) -> ExchangeRequest | None:
        return None

    async def set_status(self, id: int, status: str) -> None:
        pass

    async def set_return_tracking_url(self, id: int, url: str) -> None:
        pass


class _CapturingProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.captured_messages: list[Message] | None = None

    async def complete(
        self, model: str, messages: list[Message], api_key: str, timeout: float, *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        self.captured_messages = messages
        return CompletionResult(text=self._text, model=model)


def _order(
    gid: str = "gid://o/1", name: str = "tavas1", phone: str = "+919999999999",
    cancelled_at: str | None = None,
    fulfillments: tuple[Fulfillment, ...] = (
        Fulfillment(
            gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
            tracking_number="AWB1", tracking_url="https://track/AWB1",
            delivered_at="2026-08-19T00:00:00+00:00",
        ),
    ),
) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status="paid", fulfillment_status="FULFILLED",
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, fulfillments=fulfillments,
    )


def _context(
    provider: _CapturingProvider, user_text: str, orders: list[AuthorizedOrder],
    exchange_requests: list[ExchangeRequest] | None = None,
) -> AgentContext:
    return AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text=user_text, history=[],
        orders=orders, is_vip=False, knowledge={}, provider=provider, model="m", api_key="k",
        extra_params=None, reveal_fields=DEFAULT_REVEAL_FIELDS,
        exchange_requests=exchange_requests or [],
    )


async def test_prompt_includes_eligible_fact_for_a_recently_delivered_order() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "I want to exchange this", [authorized]), _FakeExchangeStore())
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "within the 48-hour exchange window" in prompt


async def test_prompt_includes_ineligible_fact_for_an_old_order() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order(
        fulfillments=(
            Fulfillment(
                gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
                tracking_number="AWB1", tracking_url="https://track/AWB1",
                delivered_at="2026-01-01T00:00:00+00:00",
            ),
        ),
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "I want to exchange this", [authorized]), _FakeExchangeStore())
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "outside the 48-hour exchange window" in prompt


async def test_prompt_states_size_only_no_color_or_product() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    await run(_context(provider, "I want to exchange this", [authorized]), _FakeExchangeStore())
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "size" in prompt.lower()
    assert "color" in prompt.lower() or "colour" in prompt.lower()


async def test_create_exchange_for_an_eligible_known_order_creates_a_record() -> None:
    store = _FakeExchangeStore()
    order = _order(gid="gid://o/42", name="tavas42")
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    reply = (
        '{"reply": "Done!", "handoff": false, '
        '"create_exchange": {"order_gid": "gid://o/42", "size": "M"}}'
    )
    provider = _CapturingProvider(reply)
    result = await run(_context(provider, "size M please", [authorized]), store)
    assert store.created == [("gid://o/42", "tavas42", "+919999999999", "M")]
    assert result.text == "Done!"


async def test_create_exchange_for_an_ineligible_order_is_silently_ignored() -> None:
    store = _FakeExchangeStore()
    order = _order(
        gid="gid://o/42", name="tavas42",
        fulfillments=(
            Fulfillment(
                gid="gid://f/1", status="SUCCESS", tracking_company="Delhivery",
                tracking_number="AWB1", tracking_url="https://track/AWB1",
                delivered_at="2026-01-01T00:00:00+00:00",  # long past the 48h window
            ),
        ),
    )
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    reply = (
        '{"reply": "Done!", "handoff": false, '
        '"create_exchange": {"order_gid": "gid://o/42", "size": "M"}}'
    )
    provider = _CapturingProvider(reply)
    await run(_context(provider, "size M please", [authorized]), store)
    assert store.created == []


async def test_create_exchange_for_an_unknown_order_gid_is_silently_ignored() -> None:
    store = _FakeExchangeStore()
    order = _order(gid="gid://o/42", name="tavas42")
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    reply = (
        '{"reply": "Done!", "handoff": false, '
        '"create_exchange": {"order_gid": "gid://o/DOES-NOT-EXIST", "size": "M"}}'
    )
    provider = _CapturingProvider(reply)
    await run(_context(provider, "size M please", [authorized]), store)
    assert store.created == []


async def test_no_create_exchange_field_does_not_create_a_record() -> None:
    store = _FakeExchangeStore()
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    provider = _CapturingProvider('{"reply": "Which size?", "handoff": false}')
    await run(_context(provider, "I want to exchange this", [authorized]), store)
    assert store.created == []


async def test_existing_exchange_requests_are_rendered_for_status_questions() -> None:
    provider = _CapturingProvider('{"reply": "ok", "handoff": false, "create_exchange": null}')
    order = _order()
    authorized = AuthorizedOrder(order=order, verified_phone="+919999999999")
    existing = ExchangeRequest(
        id=7, order_gid="gid://o/1", order_name="tavas1", phone_e164="+919999999999",
        requested_size="M", status="return_picked_up", requested_at="2026-08-19T00:00:00+00:00",
        return_tracking_url="https://track/return1", updated_at="2026-08-19T12:00:00+00:00",
    )
    await run(
        _context(provider, "where is my exchange", [authorized], exchange_requests=[existing]),
        _FakeExchangeStore(),
    )
    assert provider.captured_messages is not None
    prompt = provider.captured_messages[0].content
    assert "return_picked_up" in prompt
    assert "https://track/return1" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/agents/test_exchange.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.exchange'`, and `AgentContext(...)` rejects the unexpected `exchange_requests` keyword.

- [ ] **Step 3: Add the `AgentContext` field**

In `backend/app/agents/base.py`, add the import at the top:

```python
from app.core.exchange_models import ExchangeRequest
```

Add the field to `AgentContext`, right after `orders` (order matters for mypy on a frozen dataclass with defaults — this must go after every field WITHOUT a default, so placing it right before `order_number_format_hint`, which already has a default, is correct; do not place it before `orders`/`is_vip`/etc., which have no default):

```python
    order_number_format_hint: str | None = None
    # This customer's existing exchange requests (across all their orders), so the exchange
    # agent can answer "where is my exchange" from real data. Empty for every other agent --
    # only fetched by conversation.py when intent == "exchange" (Task 5).
    exchange_requests: list[ExchangeRequest] = field(default_factory=list)
```

- [ ] **Step 4: Write the agent**

Create `backend/app/agents/exchange.py`:

```python
"""Size-exchange guide agent.

Checks eligibility (app/core/exchange_eligibility.py, computed in Python -- never by the
model), collects the requested size, and creates the exchange record itself once an eligible
order + size are established in conversation (owner-directed: no button-tap confirmation step,
see docs/superpowers/specs/2026-08-20-exchange-guide-agent-design.md's "Mutation-safety note").

Deliberately does NOT use the shared HANDOFF_JSON_CONTRACT from app/agents/base.py: this
agent's JSON reply needs a THIRD field (create_exchange) beyond reply/handoff, and this
session's own error_learnings.md documents exactly the bug class that risks -- a shared
fragment appended after a local instruction can silently override it. customer_support.py
already sets the precedent of writing its own self-contained handoff wording instead of the
shared contract; this agent follows the same precedent, one level further.
"""

from app.agents.base import (
    AgentContext,
    AgentReply,
    extract_json_blob,
    extract_reply_text,
    model_asked_for_handoff,
    personality_for,
)
from app.channels.copy import copy_for
from app.core.exchange_eligibility import check_exchange_eligibility
from app.core.exchange_models import ExchangeRequest
from app.providers.base import Message, ProviderError
from app.shopify.models import AuthorizedOrder
from app.store.base import ExchangeStore
from datetime import UTC, datetime

_SYSTEM_TEMPLATE = """{personality}

The customer wants to exchange an item from their own order for a DIFFERENT SIZE. This store
does NOT offer a color or product exchange -- only a size exchange. If the customer asks for
anything other than a size change, tell them plainly that only size exchanges are available.

Below is each of the customer's orders and whether Thetavas' automated eligibility check
allows an exchange for it right now (delivered within the last 48 hours, order not
cancelled). This fact is already decided -- relay the reason given exactly as stated, never
recompute or guess the window yourself, and never offer an exchange for an order marked not
eligible.

{order_context}

Once you know BOTH which specific order the customer means AND the exact size they want,
confirm it back to them and describe this process in your own natural words (do not copy this
verbatim, keep your own warm tone):

1. We will initiate the return pickup -- our courier partner collects it within 1-2 business
   days.
2. Our Quality Check team inspects it: it must be unused, undamaged, with all original
   components. A used, torn, damaged, or incomplete item may not be eligible.
3. Once it passes inspection, we dispatch the replacement the SAME DAY.
4. The replacement arrives within 4-7 business days depending on location and courier -- you
   will get a tracking link once it ships.

{existing_requests}

Respond with STRICT JSON only, no other text:
{{"reply": "<your reply to the customer>", "handoff": <true or false>, "create_exchange": \
{{"order_gid": "<the exact order gid from the list above>", "size": "<the exact size>"}} or \
null}}

Only set "handoff" to true if the customer explicitly asks to speak with a person, or you
genuinely cannot help them with what you know -- never merely because one detail is still
missing. Set "create_exchange" ONLY on the turn where you have confirmed BOTH a specific
ELIGIBLE order and the exact size the customer wants -- otherwise it must be null. Never set
both "handoff" and "create_exchange" together.
"""


def _order_context_line(order: AuthorizedOrder, now: datetime) -> str:
    eligibility = check_exchange_eligibility(order.order, now)
    status = "eligible" if eligibility.eligible else "NOT eligible"
    return f"- order {order.order.name} (gid {order.order.gid}): {status} -- {eligibility.reason}"


def _order_context(orders: list[AuthorizedOrder], now: datetime) -> str:
    if not orders:
        return "No order is linked to this WhatsApp number yet. Ask for their order number."
    return "\n".join(_order_context_line(o, now) for o in orders)


def _existing_request_line(request: ExchangeRequest) -> str:
    tracking = (
        f", return tracking: {request.return_tracking_url}"
        if request.return_tracking_url else ""
    )
    return (
        f"- order {request.order_name}: exchange to size {request.requested_size}, "
        f"status: {request.status}, requested {request.requested_at}{tracking}"
    )


def _existing_requests_block(requests: list[ExchangeRequest]) -> str:
    if not requests:
        return ""
    lines = "\n".join(_existing_request_line(r) for r in requests)
    return f"This customer's existing exchange requests, for status questions:\n{lines}"


def _validated_create_exchange(
    data: dict[str, object] | None, orders: list[AuthorizedOrder], now: datetime,
) -> tuple[str, str, str] | None:
    """Re-derive (order_gid, order_name, size) ONLY if the model's claim checks out against
    real data -- never trust the model's own claim of eligibility or of which order/gid it
    means. Returns None (silently, the caller logs) on any mismatch."""
    if data is None:
        return None
    raw = data.get("create_exchange")
    if not isinstance(raw, dict):
        return None
    order_gid = raw.get("order_gid")
    size = raw.get("size")
    if not isinstance(order_gid, str) or not isinstance(size, str) or not size.strip():
        return None
    matching = next((o for o in orders if o.order.gid == order_gid), None)
    if matching is None:
        return None
    if not check_exchange_eligibility(matching.order, now).eligible:
        return None
    return matching.order.gid, matching.order.name, size.strip()


async def run(context: AgentContext, exchanges: ExchangeStore) -> AgentReply:
    """Handle a size-exchange conversation: relay eligibility, collect the size, create the
    request once both are confirmed, and answer status questions from existing requests."""
    fallback = copy_for("error_fallback", context.language)
    now = datetime.now(UTC)
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        order_context=_order_context(context.orders, now),
        existing_requests=_existing_requests_block(context.exchange_requests),
    )
    messages = [
        Message(role="system", content=system_prompt),
        *context.history,
        Message(role="user", content=context.user_text),
    ]
    try:
        result = await context.provider.complete(
            context.model, messages, context.api_key, context.timeout,
            extra_params=context.extra_params,
        )
    except ProviderError:
        return AgentReply(text=fallback)

    data = extract_json_blob(result.text)
    reply_text = extract_reply_text(result.text, fallback)
    handoff = model_asked_for_handoff(data)

    validated = _validated_create_exchange(data, context.orders, now)
    if validated is not None:
        order_gid, order_name, size = validated
        await exchanges.create(order_gid, order_name, context.phone_e164, size)

    return AgentReply(text=reply_text, handoff=handoff)
```

Move the `from datetime import UTC, datetime` import to the top of the file with the other imports (Python import-ordering convention already followed elsewhere in this codebase — check `ruff check` catches this if left where drafted above; place it alphabetically among the standard-library imports before the `app.*` imports).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/agents/test_exchange.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 6: Run the full backend test suite + compliance gate**

```
python -m pytest backend -v
python -m ruff check .
python -m mypy app
```
Expected: full suite green (watch for any existing test that constructs a bare `AgentContext(...)` positionally with every field — the new `exchange_requests` field has a default, so this should not break anything, but confirm), both linters clean.

- [ ] **Step 7: Run the no-secrets compliance grep**

```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/agents/base.py backend/app/agents/exchange.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/agents/base.py backend/app/agents/exchange.py backend/tests/agents/test_exchange.py
git commit -m "feat(agents): add the exchange guide agent"
```

---

### Task 5: Wire the `exchange` agent into `core/conversation.py`

**Files:**
- Modify: `backend/app/core/conversation.py`
- Test: `backend/tests/core/test_conversation.py`

**Interfaces:**
- Consumes: `exchange.run(context, exchanges)` (Task 4), `Container.exchanges` (Task 2), `Intent` including `"exchange"` (Task 3).
- Produces: a customer whose message classifies as `exchange` now gets `context.orders` and `context.exchange_requests` populated and is routed to `exchange.run`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/core/test_conversation.py`, near `test_agent_reply_order_tracking_reads_from_mirror_not_shopify` (reuse its `_FakeMirrorIngestFull`/`_PoisonedShopify` helpers and its `master_key` fixture — check the top of the file for how `master_key` is provided, it is already used by the sibling test):

```python
async def test_agent_reply_exchange_intent_resolves_orders_and_exchange_requests(
    monkeypatch, master_key: str
) -> None:
    order = _order("gid://1", "tavas3733", "+919999999999")
    mapping = MappingView(
        order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        status="pending", is_cod=False, created_at=None,
    )
    ingest = _FakeMirrorIngestFull(mappings=[mapping], mirrored_order=order)
    shopify = _PoisonedShopify()

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    monkeypatch.setattr(c, "ingest", ingest)
    monkeypatch.setattr(c, "shopify", shopify)

    from app.core.exchange_models import ExchangeRequest

    existing = ExchangeRequest(
        id=1, order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        requested_size="M", status="requested", requested_at="2026-08-20T00:00:00+00:00",
        return_tracking_url=None, updated_at="2026-08-20T00:00:00+00:00",
    )

    class _FakeExchanges:
        async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
            return [existing] if phone_e164 == "+919999999999" else []

    monkeypatch.setattr(c, "exchanges", _FakeExchanges())

    captured: dict[str, object] = {}

    async def fake_classify_intent(*args: object, **kwargs: object) -> str:
        return "exchange"

    async def fake_assemble_all(self: object) -> dict[str, str]:
        return {}

    async def fake_run_agent(context: object, intent: str, container: object) -> AgentReply:
        captured["orders"] = context.orders  # type: ignore[attr-defined]
        captured["exchange_requests"] = context.exchange_requests  # type: ignore[attr-defined]
        return AgentReply(text="ok", handoff=False)

    monkeypatch.setattr("app.core.conversation.classify_intent", fake_classify_intent)
    monkeypatch.setattr(
        "app.core.conversation.KnowledgeLoader.assemble_all", fake_assemble_all
    )
    monkeypatch.setattr("app.core.conversation._run_agent", fake_run_agent)

    from app.channels.whatsapp_inbound import InboundText
    from app.core.conversation import _agent_reply
    from app.deps import active_llm

    llm = await active_llm(c.settings, c.config)
    assert llm is not None
    event = InboundText(wa_id="919999999999", text="I want to exchange this")
    from app.admin.controls import AdminControls

    await _agent_reply(c, event, [], "+919999999999", False, llm, AdminControls())

    assert len(captured["orders"]) == 1  # type: ignore[arg-type]
    assert captured["orders"][0].order.name == "tavas3733"  # type: ignore[index]
    assert captured["exchange_requests"] == [existing]
```

Check the actual test file for `_order`, `master_key`, `MappingView`, `AgentReply`, `reset_container`/`get_container` imports and adjust this new test's imports to match whatever is already imported at the top of `test_conversation.py` rather than re-importing duplicates — the sibling `test_agent_reply_order_tracking_reads_from_mirror_not_shopify` test right above it already shows the exact working pattern; also verify `active_llm` needs a configured provider in this test's container the same way the sibling test does (check whether the sibling test seeds `llm:active_provider`/`llm:api_key:*` config, and mirror it — the fake `_run_agent` never actually calls the LLM, so a stub value is enough).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/core/test_conversation.py -k exchange_intent -v`
Expected: FAIL — `captured["orders"]` is empty (the `exchange` intent doesn't trigger order resolution yet) and `captured["exchange_requests"]` doesn't exist as a populated field.

- [ ] **Step 3: Widen the order-resolution condition and add exchange-request resolution**

In `backend/app/core/conversation.py`'s `_agent_reply`, change:

```python
    orders: list[AuthorizedOrder] = []
    order_number_format_hint: str | None = None
    if intent == "order_tracking":
```

to:

```python
    orders: list[AuthorizedOrder] = []
    order_number_format_hint: str | None = None
    exchange_requests: list[ExchangeRequest] = []
    if intent in ("order_tracking", "exchange"):
```

Immediately after the existing `orders`-building block inside that `if` (right after the `for extra in extra_orders:` loop that appends to `orders`), add:

```python
        if intent == "exchange":
            exchange_requests = await c.exchanges.list_for_phone(phone or event.wa_id)
```

Add the field to the `AgentContext(...)` construction:

```python
    context = AgentContext(
        wa_id=event.wa_id,
        phone_e164=phone or event.wa_id,
        user_text=event.text,
        history=history,
        orders=orders,
        is_vip=is_vip,
        knowledge=knowledge,
        provider=provider,
        model=model,
        api_key=api_key,
        extra_params=extra_params,
        reveal_fields=tuple(controls.reveal_fields),
        language=controls.default_language,
        order_number_format_hint=order_number_format_hint,
        exchange_requests=exchange_requests,
    )
```

Add the import at the top of the file, alongside the existing `from app.shopify.models import ...` line:

```python
from app.core.exchange_models import ExchangeRequest
```

- [ ] **Step 4: Wire the dispatch**

In `backend/app/core/conversation.py`, change `_run_agent`:

```python
async def _run_agent(context: AgentContext, intent: Intent, c: Container) -> AgentReply:
    if intent == "order_tracking":
        return await order_tracking.run(context)
    if intent == "product_search":
        return await product_search.run(context, c.shopify)
    if intent == "policy":
        return await policy.run(context)
    if intent == "recommendations":
        return await recommendations.run(context, c.shopify)
    return await customer_support.run(context)
```

to:

```python
async def _run_agent(context: AgentContext, intent: Intent, c: Container) -> AgentReply:
    if intent == "order_tracking":
        return await order_tracking.run(context)
    if intent == "product_search":
        return await product_search.run(context, c.shopify)
    if intent == "policy":
        return await policy.run(context)
    if intent == "recommendations":
        return await recommendations.run(context, c.shopify)
    if intent == "exchange":
        return await exchange.run(context, c.exchanges)
    return await customer_support.run(context)
```

Add `exchange` to the existing agents import line at the top of the file:

```python
from app.agents import (
    customer_support,
    exchange,
    order_tracking,
    policy,
    product_search,
    recommendations,
)
```

(This replaces the existing single-line `from app.agents import customer_support, order_tracking, policy, product_search, recommendations` — check `ruff format`'s line-length preference here; either the multi-line form above or a single line under 100 columns is fine, whichever `ruff check .` accepts without complaint.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/core/test_conversation.py -v`
Expected: PASS (full file).

- [ ] **Step 6: Run the full backend test suite + compliance gate**

```
python -m pytest backend -v
python -m ruff check .
python -m mypy app
```
Expected: full suite green, both linters clean.

- [ ] **Step 7: Run the no-secrets compliance grep**

```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/core/conversation.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/conversation.py backend/tests/core/test_conversation.py
git commit -m "feat(core): route the exchange intent to the exchange agent with order/request context"
```

---

### Task 6: Admin backend — exchange details in the order panel + status/tracking update endpoint

**Files:**
- Modify: `backend/app/admin/router.py`
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `ExchangeStore.list_for_phone`/`get`/`set_status`/`set_return_tracking_url` (Task 2), `ExchangeStatus` (Task 1).
- Produces: `_order_summary(order, exchange)` gains an optional `exchange` key. New `POST /admin/exchanges/{id}` endpoint.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/admin/test_views.py`, following the exact seeding pattern already used by
`test_conversation_thread_order_summary_includes_line_items` a little above this insertion
point (`order_from_webhook_payload({...})` -> `ingest.upsert_order_mirror(order)` ->
`conversations.get_or_create(phone)` -> GET the thread -> assert on `resp.json()["orders"][0]`):

```python
def test_conversation_thread_includes_exchange_details_when_a_request_exists(
    client: TestClient,
) -> None:
    login(client)
    phone = "+919876500099"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/exchange1",
        "name": "tavas9901",
        "phone": phone,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "999.00",
        "currency": "INR",
        "updated_at": "2026-08-20T10:00:00+05:30",
        "line_items": [],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))
    created = asyncio.run(
        get_container().exchanges.create("gid://shopify/Order/exchange1", "tavas9901", phone, "M")
    )

    thread_id = asyncio.run(get_container().conversations.get_or_create(phone))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    assert orders[0]["exchange"] == {
        "id": created.id, "requested_size": "M", "status": "requested",
        "return_tracking_url": None,
    }


def test_conversation_thread_order_has_no_exchange_key_when_none_exists(
    client: TestClient,
) -> None:
    login(client)
    phone = "+919876500098"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/no-exchange1",
        "name": "tavas9902",
        "phone": phone,
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "499.00",
        "currency": "INR",
        "updated_at": "2026-08-20T10:00:00+05:30",
        "line_items": [],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(phone))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    assert "exchange" not in orders[0]


def test_update_exchange_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/exchanges/1", json={"status": "return_picked_up"})
    assert resp.status_code == 401


def test_update_exchange_unknown_id_returns_404(client: TestClient) -> None:
    login(client)
    resp = client.post("/admin/exchanges/999999", json={"status": "return_picked_up"})
    assert resp.status_code == 404


def test_update_exchange_sets_status(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/1", "tavas1", "+919999999999", "M")
    )
    resp = client.post(f"/admin/exchanges/{created.id}", json={"status": "qc_passed"})
    assert resp.status_code == 200
    updated = asyncio.run(get_container().exchanges.get(created.id))
    assert updated is not None
    assert updated.status == "qc_passed"


def test_update_exchange_sets_return_tracking_url(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/2", "tavas2", "+919999999999", "L")
    )
    resp = client.post(
        f"/admin/exchanges/{created.id}", json={"return_tracking_url": "https://track/xyz"}
    )
    assert resp.status_code == 200
    updated = asyncio.run(get_container().exchanges.get(created.id))
    assert updated is not None
    assert updated.return_tracking_url == "https://track/xyz"


def test_update_exchange_rejects_invalid_status(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/3", "tavas3", "+919999999999", "S")
    )
    resp = client.post(f"/admin/exchanges/{created.id}", json={"status": "not_a_real_status"})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/admin/test_views.py -k "exchange" -v`
Expected: FAIL — `orders[0]["exchange"]` KeyError, `POST /admin/exchanges/{id}` 404s (route doesn't exist), `get_container().exchanges` may not exist as an attribute yet if Task 2 wasn't run first in this environment.

- [ ] **Step 3: Widen `_order_summary` and its call site**

In `backend/app/admin/router.py`, change `_order_summary`'s signature from:

```python
def _order_summary(order: Order) -> dict[str, object]:
```

to:

```python
def _order_summary(
    order: Order, exchange: ExchangeRequest | None = None,
) -> dict[str, object]:
```

The function currently ends with a single `return {...}` dict literal. Change that final `return {...}` into a `summary = {...}` assignment (every existing key kept byte-for-byte identical) followed by a conditional key and a `return summary`:

```python
    summary: dict[str, object] = {
        "order_name": order.name,
        "financial_status": order.financial_status,
        "fulfillment_status": order.fulfillment_status,
        "cancelled_at": order.cancelled_at,
        "is_cod": order.is_cod(),
        "total_amount": order.total.amount if order.total else None,
        "total_currency": order.total.currency if order.total else None,
        "tags": list(order.tags),
        "tracking_company": tracking_company,
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "customer_name": customer_name,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "line_items": [
            {
                "title": li.title,
                "quantity": li.quantity,
                "variant_title": li.variant_title,
                "price_amount": li.price.amount if li.price else None,
                "price_currency": li.price.currency if li.price else None,
            }
            for li in order.line_items
        ],
    }
    if exchange is not None:
        summary["exchange"] = {
            "id": exchange.id,
            "requested_size": exchange.requested_size,
            "status": exchange.status,
            "return_tracking_url": exchange.return_tracking_url,
        }
    return summary
```

(This is the exact same key set the function already returns — `financial_status` through `line_items` — with only the trailing `return {...}` -> `summary = {...}` / conditional `exchange` key / `return summary` changed. Every value expression above must match what the current function body already computes for that key; do not alter any existing key's value expression while making this edit.)

In `get_conversation_thread`, change:

```python
    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    orders_sorted = sorted(orders, key=lambda o: str(o.updated_at or ""), reverse=True)
    order_summaries = [_order_summary(o) for o in orders_sorted]
```

to:

```python
    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    orders_sorted = sorted(orders, key=lambda o: str(o.updated_at or ""), reverse=True)
    exchanges_by_order_gid = {
        e.order_gid: e for e in await c.exchanges.list_for_phone(user_id)
    }
    order_summaries = [
        _order_summary(o, exchanges_by_order_gid.get(o.gid)) for o in orders_sorted
    ]
```

(A phone can have more than one exchange request across different orders, but at most one per `order_gid` in practice for this build — `exchanges_by_order_gid` naturally keeps the LAST one seen per gid if that assumption is ever violated, which is an acceptable, documented simplification, not a bug to guard against here.)

Add the import at the top of `router.py`:

```python
from app.core.exchange_models import ExchangeRequest, ExchangeStatus
```

- [ ] **Step 4: Add the update endpoint**

In `backend/app/admin/router.py`, add a new request model near `ManualReplyRequest`:

```python
class ExchangeUpdateRequest(BaseModel):
    """Admin-driven advance of an exchange request's status and/or return-tracking link.

    Both fields optional -- a single call can set either, both, or (rejected below) neither.
    """

    status: ExchangeStatus | None = None
    return_tracking_url: str | None = Field(default=None, max_length=2048)
```

Add the endpoint near `resume_conversation`/`send_manual_reply`:

```python
@admin_router.post("/exchanges/{exchange_id}", dependencies=[Depends(require_admin)])
async def update_exchange(exchange_id: int, body: ExchangeUpdateRequest) -> dict[str, object]:
    """Advance an exchange request's status and/or set its return-tracking URL.

    No courier/QC integration exists (design doc) -- this is the only way either field ever
    changes after the exchange agent creates the request.
    """
    c = get_container()
    existing = await c.exchanges.get(exchange_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="exchange request not found")
    if body.status is not None:
        await c.exchanges.set_status(exchange_id, body.status)
    if body.return_tracking_url is not None:
        await c.exchanges.set_return_tracking_url(exchange_id, body.return_tracking_url)
    return {"ok": True}
```

(`status: ExchangeStatus | None` on the pydantic model already rejects an invalid status string with a 422 via pydantic's own `Literal` validation — no separate manual check needed, since `ExchangeStatus` is a `Literal[...]` type alias from Task 1.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/admin/test_views.py -k "exchange" -v`
Expected: PASS.

- [ ] **Step 6: Run the full backend test suite + compliance gate**

```
python -m pytest backend -v
python -m ruff check .
python -m mypy app
```
Expected: full suite green, both linters clean.

- [ ] **Step 7: Run the no-secrets compliance grep**

```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/admin/router.py
```
Expected: EMPTY output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): surface exchange details in the order panel, add status/tracking update endpoint"
```

---

### Task 7: Admin frontend — Exchange section in the order panel

**Files:**
- Modify: `backend/app/admin/static/chats.js`
- Modify: `backend/app/admin/static/chats.html`

**Interfaces:**
- Consumes: `order.exchange` (`{requested_size, status, return_tracking_url} | undefined`, Task 6), `POST /admin/exchanges/{id}` (Task 6).

No test file — this codebase has no browser/JS test runner (documented, accepted gap for every prior admin-frontend change in this project's `_pipeline_status.md`). This task ends with a manual browser-verification note instead of an automated test step.

- [ ] **Step 1: Add the render function**

In `backend/app/admin/static/chats.js`, add a new function right after `renderOrderDetail` (after its closing `}` around line 413), and call it from inside `renderOrderDetail` at the very end (before its closing brace):

```javascript
function renderExchangeDetail(order) {
  const container = el("order-exchange");
  container.innerHTML = "";
  if (!order.exchange) {
    container.style.display = "none";
    return;
  }
  container.style.display = "block";
  const heading = document.createElement("h4");
  heading.textContent = "Exchange";
  container.appendChild(heading);

  const sizeRow = document.createElement("div");
  sizeRow.className = "order-field";
  sizeRow.innerHTML = "<span class='label'>Requested size:</span> ";
  sizeRow.appendChild(document.createTextNode(order.exchange.requested_size));
  container.appendChild(sizeRow);

  const statusSelect = document.createElement("select");
  statusSelect.className = "exchange-status-select";
  const statuses = [
    "requested", "return_picked_up", "qc_passed", "qc_failed",
    "replacement_dispatched", "delivered",
  ];
  for (const s of statuses) {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s.replace(/_/g, " ");
    if (s === order.exchange.status) opt.selected = true;
    statusSelect.appendChild(opt);
  }
  container.appendChild(statusSelect);

  const trackingInput = document.createElement("input");
  trackingInput.type = "text";
  trackingInput.className = "exchange-tracking-input";
  trackingInput.placeholder = "Return tracking URL";
  trackingInput.value = order.exchange.return_tracking_url || "";
  container.appendChild(trackingInput);

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      await api(`/admin/exchanges/${order.exchange.id}`, {
        method: "POST",
        body: JSON.stringify({
          status: statusSelect.value,
          return_tracking_url: trackingInput.value || null,
        }),
      });
    } finally {
      saveBtn.disabled = false;
    }
  });
  container.appendChild(saveBtn);
}
```

Check the existing `api(...)` helper's exact call signature (used elsewhere in this file, e.g. for `send_manual_reply`) and match its argument shape exactly — the sketch above assumes `api(path, {method, body})`; adjust to whatever this file's `api()` actually expects if different.

At the very end of `renderOrderDetail` (right before its closing `}`), add:

```javascript
  renderExchangeDetail(order);
```

- [ ] **Step 2: Add the container element**

In `backend/app/admin/static/chats.html`, find the order-detail panel's existing structure (look for `id="order-detail"` and its sibling `id="order-products"`/`id="order-customer"` containers) and add a new sibling container right after `order-detail`:

```html
<div id="order-exchange" class="order-panel-section" style="display: none;"></div>
```

- [ ] **Step 3: Add minimal CSS**

In `chats.html`'s existing `<style>` block, add rules for the new elements, matching the existing `.order-field`/`h4` styling already used by the fulfillment/customer sections (copy their exact property values rather than inventing new ones, so the new section looks native, not bolted on):

```css
.exchange-status-select, .exchange-tracking-input {
  display: block;
  width: 100%;
  margin: 0.25rem 0;
  font-size: 0.8rem;
}
```

- [ ] **Step 4: Manual browser verification (cannot be automated in this sandbox)**

This step cannot be executed by the `developer` agent in this sandbox (no browser available) — leave it as an explicit note in the task's completion report for the owner:

> Start the dev server, open `/admin/ui/chats.html`, open a thread whose order has an exchange request (create one via the chat flow or directly through `POST /admin/exchanges` in this task's tests), confirm the Exchange section renders with the right size/status/tracking link, change the status dropdown and tracking URL, click Save, reload the thread, and confirm the change persisted.

- [ ] **Step 5: Run the compliance gate on the rest of the touched files this session**

```
python -m ruff check .
python -m mypy app
```
Expected: both clean (this task's own files are JS/HTML, outside `ruff`/`mypy`'s scope, but re-running the gate catches any accidental drift from earlier tasks in the same session).

- [ ] **Step 6: Run the no-secrets compliance grep**

```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/admin/static/chats.js backend/app/admin/static/chats.html
```
Expected: EMPTY output.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.js backend/app/admin/static/chats.html
git commit -m "feat(admin): render exchange details and status/tracking controls in the order panel"
```

---

## After all tasks: registries, status update, and review routing

This is documentation, not app code — do it directly (or via `doc-updater`), not via the `developer` agent.

- [ ] Update `docs/memory/component_registry.md` with: `app/core/exchange_models.py`, `app/core/exchange_eligibility.py`, `app/agents/exchange.py`, `ExchangeStore` (+ both implementations), the new `exchange_requests` table, and the `Intent`/`AgentContext` widenings.
- [ ] Update `docs/memory/api_registry.md` with `POST /admin/exchanges/{id}` and the widened `GET /admin/conversations/{thread_id}` response shape (`orders[].exchange`).
- [ ] Add a new row to `docs/FR/_pipeline_status.md` for this feature, status `REVIEW` (or `BUILT` before review starts), summarizing what shipped across the 7 tasks and noting Task 7's manual-browser-verification gap explicitly (matches this project's existing documented pattern for every prior admin-frontend change).
- [ ] Route to `code-reviewer` per `.claude/rules/common/agents.md` (scoped to all files touched across the 7 tasks). This feature does NOT touch credentials, webhooks, auth, or CORS — it does write to the database via `ExchangeStore.create`/`set_status`/`set_return_tracking_url`, gated by the deterministic re-validation in `exchange.py`, so use judgment on whether `security-reviewer` is warranted (recommend running it, given the mutation path, even though it is not a Shopify/webhook/credential surface — the owner can confirm or skip based on the code-reviewer's findings).
- [ ] Do NOT mark this pushed — per CLAUDE.md, push only after explicit owner approval.
