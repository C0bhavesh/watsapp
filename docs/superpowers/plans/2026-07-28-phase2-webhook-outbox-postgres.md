# Phase 2 — Shopify Webhook Receiver + Durable Outbox + Postgres Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot learns about every new order: `orders/create` webhook → HMAC verify → one atomic ingest (dedupe + phone→order mapping + outbox row per ADR-001) → 200 in <5s, plus the Postgres layer, subscription self-heal, and the authenticated jobs dispatcher.

**Architecture:** New `app/channels/` (Shopify webhook transport: signature, payload parsing, endpoint), new `app/jobs/` (single dispatcher per review F11), `app/shopify/subscriptions.py` (self-heal), and the store layer grows an `IngestStore` port whose implementations own the ADR-001 transaction (in-memory for tests, asyncpg for production). No WhatsApp sending in this phase — the outbox is filled here, drained in Phase 3.

**Tech Stack:** As Phase 1 + `asyncpg>=0.29`. Postgres tests are gated on `TEST_DATABASE_URL` (skip when absent — suite stays green offline).

## Global Constraints

- All Phase 1 Global Constraints still apply (secrets, API version only from Settings, ruff+mypy clean per task, secrets grep before each commit, Co-Authored-By trailer, NEVER `git push`).
- Shopify webhook HMAC: header `X-Shopify-Hmac-Sha256`, **base64** HMAC-SHA256 of the RAW body with the app **client secret** (config key `shopify:client_secret`) — constant-time compare (CLAUDE.md Critical Rule 3; error_learnings entry "Two different webhook HMAC schemes").
- The webhook handler performs NO network calls (no Meta, no Shopify, no LLM) — one DB transaction then respond (ADR-001; review F1/F10). Failures → 5xx so Shopify retries.
- Outbox `dedupe_key` for order pushes: `order_created:{order_gid}` — UNIQUE = one push per order ever (ADR-001/F2).
- Eligibility: config `push_policy` (default `cod_only`) + staleness guard config `push_staleness_hours` (default `6`) — backfill/sweep paths call ingest with `outbound=None` (F2). Config values via `ConfigService.get_plain` (ADR-005).
- Language for the queued template: order `customer_locale` first two letters if in `{en, hi, gu}`, else `en` (F15 rule; learned-preference tier arrives with Phase 3+).
- Use the payload's `admin_graphql_api_id` as the order GID — never reconstruct from numeric id (F20).
- Phones normalize to E.164 with `+91` default country (order data verified live: already `+91…`, but WATI-era orders and wa_ids vary).
- `pending_actions`, `order_actions`, `processed_messages`, `conversations`, `messages` tables are CREATED in the schema now (Level 4) but get no repo code until their phases.
- Jobs endpoint refuses to run when `cron_secret` is unset (503) — never an open endpoint (F11).

## File Structure (Phase 2 additions)

```
backend/
  app/core/__init__.py
  app/core/phone.py                  # normalize_phone (E.164)
  app/channels/__init__.py
  app/channels/shopify_signature.py  # verify_shopify_hmac (base64, constant-time)
  app/channels/shopify_orders.py     # payload parse + language + eligibility (pure)
  app/channels/shopify_webhook.py    # POST /webhooks/shopify router
  app/jobs/__init__.py
  app/jobs/router.py                 # POST|GET /internal/jobs/{name} + registry
  app/shopify/subscriptions.py       # ensure_subscription self-heal
  app/store/base.py                  # + IngestStore Protocol + dataclasses (modify)
  app/store/memory.py                # + InMemoryIngestStore (modify)
  app/store/schema.sql               # full Level 4 schema
  app/store/pg_factory.py            # lazy asyncpg pool
  app/store/postgres.py              # PostgresConfigRepo + PostgresIngestStore
  app/deps.py                        # wire ingest + postgres switch (modify)
  app/main.py                        # include routers (modify)
  app/config/settings.py             # + cron_secret (modify)
  scripts/apply_schema.py            # applies schema.sql to DATABASE_URL
  tests/test_phone.py
  tests/test_shopify_signature.py
  tests/test_shopify_orders.py
  tests/test_ingest_store.py
  tests/test_shopify_webhook.py
  tests/test_subscriptions.py
  tests/test_jobs_router.py
  tests/test_postgres_store.py       # skipif no TEST_DATABASE_URL
```

---

### Task 1: Phone normalization (E.164)

**Files:**
- Create: `backend/app/core/__init__.py` (empty), `backend/app/core/phone.py`
- Test: `backend/tests/test_phone.py`

**Interfaces:**
- Produces: `normalize_phone(raw: str | None) -> str | None` — E.164 (`+91…`) or None.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_phone.py`:
```python
import pytest

from app.core.phone import normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+919664290413", "+919664290413"),
        ("+91 96642 90413", "+919664290413"),
        ("9664290413", "+919664290413"),
        ("09664290413", "+919664290413"),
        ("919664290413", "+919664290413"),
        ("917575072795", "+917575072795"),        # wa_id form
        ("0091 9664290413", "+919664290413"),
        ("+1 555 651 8147", "+15556518147"),      # non-Indian stays as-is
        ("", None),
        (None, None),
        ("hello", None),
        ("123", None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected
```

- [ ] **Step 2: Run to verify FAIL** — `python -m pytest tests/test_phone.py -v` → `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/core/phone.py`:
```python
import re


def normalize_phone(raw: str | None) -> str | None:
    """Normalize to E.164. Default country: India (+91) for bare 10-digit numbers."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if 11 <= len(digits) <= 15:
        return f"+{digits}"
    return None
```

- [ ] **Step 4: Run to verify PASS** — tests green; `ruff check .`; `mypy app` clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: E.164 phone normalization with +91 default"`

---

### Task 2: Level 4 schema + apply script

**Files:**
- Create: `backend/app/store/schema.sql`, `backend/scripts/apply_schema.py`

**Interfaces:**
- Produces: idempotent DDL (`CREATE TABLE IF NOT EXISTS`) for the full Level 4 schema; `python -m scripts.apply_schema` applies it to `DATABASE_URL`.

- [ ] **Step 1: Write the schema**

`backend/app/store/schema.sql`:
```sql
-- Level 4 data model (architecture-plan v1.1). Idempotent.
CREATE TABLE IF NOT EXISTS app_config (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_mappings (
    order_gid                  text PRIMARY KEY,
    order_name                 text NOT NULL,
    order_number_int           bigint,
    phone_e164                 text,
    customer_name              text,
    email                      text,
    language                   text NOT NULL DEFAULT 'en',
    financial_status_at_create text,          -- creation-time SNAPSHOT, never authoritative
    is_cod                     boolean NOT NULL DEFAULT false,
    status                     text NOT NULL DEFAULT 'pending',
    store_id                   text NOT NULL DEFAULT 'thetavas',
    template_sent_at           timestamptz,
    responded_at               timestamptz,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_mappings_phone ON order_mappings (phone_e164);
CREATE INDEX IF NOT EXISTS idx_order_mappings_name  ON order_mappings (order_name);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id              bigserial PRIMARY KEY,
    dedupe_key      text NOT NULL UNIQUE,
    state           text NOT NULL DEFAULT 'queued',  -- queued|sent|suppressed|failed|undeliverable
    kind            text NOT NULL,
    phone_e164      text NOT NULL,
    payload_json    text NOT NULL,
    template_wamid  text,
    delivery_status text,
    attempts        int NOT NULL DEFAULT 0,
    last_error_code text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outbound_state ON outbound_messages (state, created_at);

CREATE TABLE IF NOT EXISTS processed_webhooks (
    webhook_id  text NOT NULL,
    topic       text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (webhook_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_processed_webhooks_received ON processed_webhooks (received_at);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  text PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          bigserial PRIMARY KEY,
    wa_id       text NOT NULL,
    order_gid   text NOT NULL,
    action      text NOT NULL,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_actions (
    id               bigserial PRIMARY KEY,
    order_gid        text NOT NULL,
    action           text NOT NULL,
    actor_wa_id      text,
    source_wamid     text,
    result           text NOT NULL,
    user_errors_json text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL,
    running_summary text,
    paused_until    timestamptz,
    last_active_at  timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, last_active_at);

CREATE TABLE IF NOT EXISTS messages (
    id              bigserial PRIMARY KEY,
    conversation_id bigint NOT NULL REFERENCES conversations (id),
    role            text NOT NULL,
    content         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, created_at);
```

`backend/scripts/apply_schema.py`:
```python
"""Apply app/store/schema.sql to DATABASE_URL. Run: python -m scripts.apply_schema"""

import asyncio
import pathlib

import asyncpg

from app.config.settings import Settings


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")
    sql = (pathlib.Path(__file__).parent.parent / "app" / "store" / "schema.sql").read_text()
    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(sql)
        print("schema applied OK")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify** — `python - <<'EOF'` sanity: read the file and assert every Level 4 table name appears:

```python
text = open("app/store/schema.sql").read()
for t in ["app_config", "order_mappings", "outbound_messages", "processed_webhooks",
          "processed_messages", "pending_actions", "order_actions", "conversations", "messages"]:
    assert f"CREATE TABLE IF NOT EXISTS {t}" in text, t
print("schema OK")
EOF
```
Also add `asyncpg>=0.29` to `backend/requirements.txt` and `pip install asyncpg`.

- [ ] **Step 3: Commit** — `git commit -m "feat: full Level 4 Postgres schema and apply script"`

---

### Task 3: IngestStore port + in-memory implementation

**Files:**
- Modify: `backend/app/store/base.py`, `backend/app/store/memory.py`
- Test: `backend/tests/test_ingest_store.py`

**Interfaces:**
- Produces (append to `base.py`):
  - `MappingUpsert` frozen dataclass: `order_gid: str, order_name: str, order_number_int: int | None, phone_e164: str | None, customer_name: str | None, email: str | None, language: str, financial_status_at_create: str | None, is_cod: bool`
  - `OutboundDraft` frozen dataclass: `dedupe_key: str, kind: str, phone_e164: str, payload_json: str`
  - `IngestResult` frozen dataclass: `duplicate: bool, queued: bool`
  - `IngestStore` Protocol: `async ingest_order_created(webhook_id: str, topic: str, mapping: MappingUpsert, outbound: OutboundDraft | None) -> IngestResult`
- Produces (`memory.py`): `InMemoryIngestStore()` with inspectable state: `.webhooks: set[tuple[str, str]]`, `.mappings: dict[str, MappingUpsert]`, `.outbound: dict[str, OutboundDraft]`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_ingest_store.py`:
```python
from app.store.base import IngestResult, MappingUpsert, OutboundDraft
from app.store.memory import InMemoryIngestStore


def mapping(gid: str = "gid://shopify/Order/1") -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164="+911111111111",
        customer_name="A B", email="a@b.c", language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )


def outbound(gid: str = "gid://shopify/Order/1") -> OutboundDraft:
    return OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164="+911111111111", payload_json="{}",
    )


async def test_first_ingest_maps_and_queues() -> None:
    store = InMemoryIngestStore()
    result = await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    assert result == IngestResult(duplicate=False, queued=True)
    assert ("wh1", "orders/create") in store.webhooks
    assert "gid://shopify/Order/1" in store.mappings
    assert "order_created:gid://shopify/Order/1" in store.outbound


async def test_duplicate_webhook_id_is_noop() -> None:
    store = InMemoryIngestStore()
    await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    result = await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    assert result == IngestResult(duplicate=True, queued=False)
    assert len(store.outbound) == 1


async def test_outbox_dedupe_key_unique_across_webhook_ids() -> None:
    store = InMemoryIngestStore()
    await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    result = await store.ingest_order_created("wh2", "orders/create", mapping(), outbound())
    assert result == IngestResult(duplicate=False, queued=False)  # mapping upserted, push already queued once
    assert len(store.outbound) == 1


async def test_ineligible_ingest_maps_without_queueing() -> None:
    store = InMemoryIngestStore()
    result = await store.ingest_order_created("wh1", "orders/create", mapping(), None)
    assert result == IngestResult(duplicate=False, queued=False)
    assert "gid://shopify/Order/1" in store.mappings and not store.outbound
```

- [ ] **Step 2: Run to verify FAIL** — `ImportError: MappingUpsert`

- [ ] **Step 3: Implement**

Append to `backend/app/store/base.py`:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class MappingUpsert:
    order_gid: str
    order_name: str
    order_number_int: int | None
    phone_e164: str | None
    customer_name: str | None
    email: str | None
    language: str
    financial_status_at_create: str | None
    is_cod: bool


@dataclass(frozen=True)
class OutboundDraft:
    dedupe_key: str
    kind: str
    phone_e164: str
    payload_json: str


@dataclass(frozen=True)
class IngestResult:
    duplicate: bool
    queued: bool


class IngestStore(Protocol):
    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult: ...
```

Append to `backend/app/store/memory.py`:
```python
from app.store.base import IngestResult, MappingUpsert, OutboundDraft


class InMemoryIngestStore:
    def __init__(self) -> None:
        self.webhooks: set[tuple[str, str]] = set()
        self.mappings: dict[str, MappingUpsert] = {}
        self.outbound: dict[str, OutboundDraft] = {}

    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult:
        key = (webhook_id, topic)
        if key in self.webhooks:
            return IngestResult(duplicate=True, queued=False)
        self.webhooks.add(key)
        self.mappings[mapping.order_gid] = mapping
        queued = False
        if outbound is not None and outbound.dedupe_key not in self.outbound:
            self.outbound[outbound.dedupe_key] = outbound
            queued = True
        return IngestResult(duplicate=False, queued=queued)
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: IngestStore port with in-memory impl (ADR-001 atomic ingest)"`

---

### Task 4: Order-created payload parsing, language rule, eligibility

**Files:**
- Create: `backend/app/channels/__init__.py` (empty), `backend/app/channels/shopify_orders.py`
- Test: `backend/tests/test_shopify_orders.py`

**Interfaces:**
- Consumes: `normalize_phone` (Task 1).
- Produces: `IncomingOrder` frozen dataclass: `gid: str, name: str, order_number: int | None, email: str | None, phone_e164: str | None, customer_name: str | None, tags: tuple[str, ...], gateways: tuple[str, ...], created_at: datetime | None, locale: str | None` with method `is_cod() -> bool`;
  `parse_order_created(payload: dict) -> IncomingOrder | None`;
  `choose_language(locale: str | None, default: str = "en") -> str` (supported `{"en","hi","gu"}`);
  `is_eligible_for_push(order: IncomingOrder, now: datetime, push_policy: str, staleness_hours: float) -> bool`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_shopify_orders.py`:
```python
from datetime import UTC, datetime, timedelta

from app.channels.shopify_orders import (
    IncomingOrder,
    choose_language,
    is_eligible_for_push,
    parse_order_created,
)

PAYLOAD = {
    "admin_graphql_api_id": "gid://shopify/Order/12187547894128",
    "name": "tavas3733",
    "order_number": 3733,
    "email": "c@example.com",
    "phone": None,
    "customer": {"first_name": "Suman", "last_name": "Bayala", "phone": None},
    "shipping_address": {"phone": "+91 9664290413"},
    "billing_address": {"phone": None},
    "tags": "COD, COD pending, Shopflo",
    "payment_gateway_names": ["Cash on Delivery (COD)"],
    "financial_status": "pending",
    "created_at": "2026-07-28T03:14:46-04:00",
    "customer_locale": "en-IN",
    "test": False,
}


def test_parse_full_payload() -> None:
    order = parse_order_created(PAYLOAD)
    assert order is not None
    assert order.gid == "gid://shopify/Order/12187547894128"
    assert order.name == "tavas3733"
    assert order.order_number == 3733
    assert order.phone_e164 == "+919664290413"     # shipping fallback, normalized
    assert order.customer_name == "Suman Bayala"
    assert order.is_cod()
    assert order.created_at is not None and order.created_at.tzinfo is not None
    assert order.locale == "en-IN"


def test_parse_missing_gid_returns_none() -> None:
    assert parse_order_created({"name": "x"}) is None


def test_parse_tolerates_missing_optional_fields() -> None:
    order = parse_order_created({"admin_graphql_api_id": "gid://shopify/Order/2", "name": "tavas9"})
    assert order is not None
    assert order.phone_e164 is None and order.tags == () and order.created_at is None


def test_choose_language() -> None:
    assert choose_language("en-IN") == "en"
    assert choose_language("hi") == "hi"
    assert choose_language("gu-IN") == "gu"
    assert choose_language("ta-IN") == "en"
    assert choose_language(None) == "en"


def make_order(created_delta_hours: float, cod: bool) -> IncomingOrder:
    parsed = parse_order_created(PAYLOAD)
    assert parsed is not None
    return IncomingOrder(
        **{**parsed.__dict__,
           "created_at": datetime.now(UTC) - timedelta(hours=created_delta_hours),
           "gateways": ("Cash on Delivery (COD)",) if cod else ("Razorpay",),
           "tags": () if not cod else parsed.tags}
    )


def test_eligibility_cod_only_policy() -> None:
    now = datetime.now(UTC)
    assert is_eligible_for_push(make_order(1, cod=True), now, "cod_only", 6.0)
    assert not is_eligible_for_push(make_order(1, cod=False), now, "cod_only", 6.0)
    assert is_eligible_for_push(make_order(1, cod=False), now, "all", 6.0)


def test_eligibility_staleness_guard() -> None:
    now = datetime.now(UTC)
    assert not is_eligible_for_push(make_order(7, cod=True), now, "cod_only", 6.0)


def test_eligibility_unparseable_created_at_is_ineligible() -> None:
    parsed = parse_order_created({"admin_graphql_api_id": "g", "name": "n"})
    assert parsed is not None
    assert not is_eligible_for_push(parsed, datetime.now(UTC), "all", 6.0)
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/shopify_orders.py`:
```python
from dataclasses import dataclass
from datetime import datetime

from app.core.phone import normalize_phone

SUPPORTED_LANGUAGES = frozenset({"en", "hi", "gu"})


@dataclass(frozen=True)
class IncomingOrder:
    gid: str
    name: str
    order_number: int | None
    email: str | None
    phone_e164: str | None
    customer_name: str | None
    tags: tuple[str, ...]
    gateways: tuple[str, ...]
    created_at: datetime | None
    locale: str | None

    def is_cod(self) -> bool:
        if any("cash on delivery" in g.lower() for g in self.gateways):
            return True
        return any(t.strip().lower() == "cod" for t in self.tags)


def _parse_created_at(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_order_created(payload: dict) -> IncomingOrder | None:  # type: ignore[type-arg]
    gid = payload.get("admin_graphql_api_id")
    name = payload.get("name")
    if not isinstance(gid, str) or not isinstance(name, str) or not gid or not name:
        return None
    customer = payload.get("customer") or {}
    shipping = payload.get("shipping_address") or {}
    billing = payload.get("billing_address") or {}
    phone = (
        normalize_phone(payload.get("phone"))
        or normalize_phone(customer.get("phone"))
        or normalize_phone(shipping.get("phone"))
        or normalize_phone(billing.get("phone"))
    )
    first = str(customer.get("first_name") or shipping.get("first_name") or "").strip()
    last = str(customer.get("last_name") or shipping.get("last_name") or "").strip()
    customer_name = f"{first} {last}".strip() or None
    raw_tags = payload.get("tags") or ""
    tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip()) if isinstance(raw_tags, str) else ()
    gateways = tuple(str(g) for g in payload.get("payment_gateway_names") or ())
    number = payload.get("order_number")
    return IncomingOrder(
        gid=gid,
        name=name,
        order_number=int(number) if isinstance(number, int) else None,
        email=payload.get("email"),
        phone_e164=phone,
        customer_name=customer_name,
        tags=tags,
        gateways=gateways,
        created_at=_parse_created_at(payload.get("created_at")),
        locale=payload.get("customer_locale"),
    )


def choose_language(locale: str | None, default: str = "en") -> str:
    if locale:
        code = locale[:2].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
    return default


def is_eligible_for_push(
    order: IncomingOrder, now: datetime, push_policy: str, staleness_hours: float
) -> bool:
    if order.created_at is None:
        return False
    if (now - order.created_at).total_seconds() > staleness_hours * 3600:
        return False
    if push_policy == "cod_only":
        return order.is_cod()
    return push_policy in ("all", "all_prepaid_no_buttons")
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: order-created payload parsing, language rule, push eligibility (ADR-005/F2)"`

---

### Task 5: Shopify HMAC verifier (base64, constant-time)

**Files:**
- Create: `backend/app/channels/shopify_signature.py`
- Test: `backend/tests/test_shopify_signature.py`

**Interfaces:**
- Produces: `verify_shopify_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_shopify_signature.py`:
```python
import base64
import hashlib
import hmac as hmac_lib

from app.channels.shopify_signature import verify_shopify_hmac

SECRET = "test-secret"
BODY = b'{"id": 1}'


def good_header() -> str:
    return base64.b64encode(hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).digest()).decode()


def test_valid_signature_passes() -> None:
    assert verify_shopify_hmac(BODY, good_header(), SECRET)


def test_tampered_body_fails() -> None:
    assert not verify_shopify_hmac(b'{"id": 2}', good_header(), SECRET)


def test_missing_header_fails() -> None:
    assert not verify_shopify_hmac(BODY, None, SECRET)
    assert not verify_shopify_hmac(BODY, "", SECRET)


def test_hex_encoding_is_rejected() -> None:
    hex_header = hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert not verify_shopify_hmac(BODY, hex_header, SECRET)
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/shopify_signature.py`:
```python
import base64
import hashlib
import hmac


def verify_shopify_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """Shopify webhook HMAC: base64(HMAC-SHA256(raw body, client secret)) — NOT hex (Meta is hex)."""
    if not header_value:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_value.strip())
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: Shopify base64 HMAC verifier (constant-time, raw body)"`

---

### Task 6: Webhook endpoint (POST /webhooks/shopify)

**Files:**
- Create: `backend/app/channels/shopify_webhook.py`
- Modify: `backend/app/main.py` (include router), `backend/app/deps.py` (add `ingest: IngestStore` to Container)
- Test: `backend/tests/test_shopify_webhook.py`

**Interfaces:**
- Consumes: `verify_shopify_hmac` (Task 5), `parse_order_created`/`choose_language`/`is_eligible_for_push` (Task 4), `IngestStore`/`MappingUpsert`/`OutboundDraft` (Task 3), `get_container()`.
- Produces: `router` with `POST /webhooks/shopify` → 403 bad HMAC; 200 `{"ok": true, "ignored": true}` for non-`orders/create` topics, missing webhook id, or unparseable payload; 200 `{"ok": true, "duplicate": bool, "queued": bool}` on ingest. Config keys read: `push_policy` (default `cod_only`), `push_staleness_hours` (default `6`). Outbox payload_json: `{"template": "order_confirmation_cod", "language": <lang>, "customer_name": ..., "order_name": ..., "amount": ...}` — amount from payload `total_price` (string) or `""`.
- Produces (deps): `Container.ingest` wired to `InMemoryIngestStore()` (Postgres switch in Task 9).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_shopify_webhook.py`:
```python
import base64
import hashlib
import hmac as hmac_lib
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.deps import get_container, reset_container

SECRET = "csec-webhook"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    await c.config.set_secret("shopify:client_secret", SECRET)
    yield
    reset_container()


def payload(gid: str = "gid://shopify/Order/1") -> dict:
    return {
        "admin_graphql_api_id": gid,
        "name": "tavas3733",
        "order_number": 3733,
        "email": "c@example.com",
        "customer": {"first_name": "Suman", "last_name": "B"},
        "shipping_address": {"phone": "+919664290413"},
        "tags": "COD",
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "949.00",
        "created_at": datetime.now(UTC).isoformat(),
        "customer_locale": "hi-IN",
    }


def sign(body: bytes) -> str:
    return base64.b64encode(hmac_lib.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


async def post(body: bytes, headers: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/shopify", content=body, headers=headers)


def headers(body: bytes, topic: str = "orders/create", webhook_id: str = "wh-1") -> dict:
    return {
        "X-Shopify-Hmac-Sha256": sign(body),
        "X-Shopify-Topic": topic,
        "X-Shopify-Webhook-Id": webhook_id,
        "Content-Type": "application/json",
    }


async def test_bad_hmac_403_and_nothing_stored() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, {**headers(body), "X-Shopify-Hmac-Sha256": "AAAA"})
    assert resp.status_code == 403
    assert not get_container().ingest.webhooks  # type: ignore[attr-defined]


async def test_orders_create_ingests_and_queues() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    store = get_container().ingest
    draft = store.outbound["order_created:gid://shopify/Order/1"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "order_confirmation_cod"
    assert params["language"] == "hi"
    assert draft.phone_e164 == "+919664290413"


async def test_duplicate_webhook_id_reports_duplicate() -> None:
    body = json.dumps(payload()).encode()
    await post(body, headers(body))
    resp = await post(body, headers(body))
    assert resp.json() == {"ok": True, "duplicate": True, "queued": False}


async def test_prepaid_order_maps_but_does_not_queue_under_cod_only() -> None:
    p = payload("gid://shopify/Order/2")
    p["payment_gateway_names"] = ["Razorpay"]
    p["tags"] = "online"
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, webhook_id="wh-2"))
    assert resp.json() == {"ok": True, "duplicate": False, "queued": False}
    store = get_container().ingest
    assert "gid://shopify/Order/2" in store.mappings  # type: ignore[attr-defined]


async def test_other_topic_ignored() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body, topic="orders/updated"))
    assert resp.json() == {"ok": True, "ignored": True}


async def test_garbage_body_with_valid_hmac_ignored() -> None:
    body = b"not-json"
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}
```

- [ ] **Step 2: Run to verify FAIL** — 404 (router not mounted) / `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/shopify_webhook.py`:
```python
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.shopify_orders import (
    choose_language,
    is_eligible_for_push,
    parse_order_created,
)
from app.channels.shopify_signature import verify_shopify_hmac
from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft

router = APIRouter()

TEMPLATE_NAME = "order_confirmation_cod"


@router.post("/webhooks/shopify")
async def shopify_webhook(request: Request) -> Response:
    raw = await request.body()
    c = get_container()
    secret = await c.config.get_secret("shopify:client_secret")
    if not secret or not verify_shopify_hmac(
        raw, request.headers.get("X-Shopify-Hmac-Sha256"), secret
    ):
        return PlainTextResponse("forbidden", status_code=403)

    topic = request.headers.get("X-Shopify-Topic", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "")
    if topic != "orders/create" or not webhook_id:
        return JSONResponse({"ok": True, "ignored": True})

    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"ok": True, "ignored": True})
    incoming = parse_order_created(payload) if isinstance(payload, dict) else None
    if incoming is None:
        return JSONResponse({"ok": True, "ignored": True})

    language = choose_language(incoming.locale)
    mapping = MappingUpsert(
        order_gid=incoming.gid,
        order_name=incoming.name,
        order_number_int=incoming.order_number,
        phone_e164=incoming.phone_e164,
        customer_name=incoming.customer_name,
        email=incoming.email,
        language=language,
        financial_status_at_create=payload.get("financial_status"),
        is_cod=incoming.is_cod(),
    )

    outbound: OutboundDraft | None = None
    push_policy = await c.config.get_plain("push_policy") or "cod_only"
    staleness_raw = await c.config.get_plain("push_staleness_hours")
    staleness_hours = float(staleness_raw) if staleness_raw else 6.0
    if incoming.phone_e164 and is_eligible_for_push(
        incoming, datetime.now(UTC), push_policy, staleness_hours
    ):
        outbound = OutboundDraft(
            dedupe_key=f"order_created:{incoming.gid}",
            kind="order_confirmation",
            phone_e164=incoming.phone_e164,
            payload_json=json.dumps(
                {
                    "template": TEMPLATE_NAME,
                    "language": language,
                    "customer_name": incoming.customer_name or "",
                    "order_name": incoming.name,
                    "amount": str(payload.get("total_price") or ""),
                }
            ),
        )

    result = await c.ingest.ingest_order_created(webhook_id, topic, mapping, outbound)
    return JSONResponse({"ok": True, "duplicate": result.duplicate, "queued": result.queued})
```

Modify `backend/app/deps.py`: add to imports `from app.store.base import ConfigRepo, IngestStore` and `from app.store.memory import InMemoryConfigRepo, InMemoryIngestStore`; add field `ingest: IngestStore` to `Container`; in `get_container()` build `ingest = InMemoryIngestStore()` and pass it.

Modify `backend/app/main.py`:
```python
from fastapi import FastAPI

from app.channels.shopify_webhook import router as shopify_webhook_router

app = FastAPI(title="Thetavas Order Bot")
app.include_router(shopify_webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "thetavas-order-bot"}
```

- [ ] **Step 4: Run to verify PASS** — full suite green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: orders/create webhook endpoint — HMAC, idempotent atomic ingest, outbox queueing"`

---

### Task 7: Subscription self-heal

**Files:**
- Create: `backend/app/shopify/subscriptions.py`
- Test: `backend/tests/test_subscriptions.py`

**Interfaces:**
- Consumes: `ShopifyClient._graphql` (Phase 1).
- Produces: `async ensure_subscription(client: ShopifyClient, callback_url: str) -> str` returning `"ok" | "created" | "updated"`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_subscriptions.py`:
```python
import json

import httpx

from app.shopify.subscriptions import ensure_subscription
from tests.test_client_graphql import grant_or, make_client, seed


def sub_edge(url: str) -> dict:
    return {"node": {"id": "gid://shopify/WebhookSubscription/5", "topic": "ORDERS_CREATE",
                     "endpoint": {"__typename": "WebhookHttpEndpoint", "callbackUrl": url}}}


async def test_existing_correct_subscription_is_ok(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify")]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "ok"


async def test_missing_subscription_is_created(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/9"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "created"
    create_call = captured[-1]
    assert create_call["variables"]["callbackUrl"] == "https://x.example/webhooks/shopify"


async def test_wrong_url_subscription_is_updated(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionUpdate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/5"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://old.example/hook")]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "updated"
    assert captured[-1]["variables"]["id"] == "gid://shopify/WebhookSubscription/5"
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/shopify/subscriptions.py`:
```python
from app.shopify.client import ShopifyClient
from app.shopify.errors import ShopifyGraphQLError

_LIST_QUERY = (
    "query { webhookSubscriptions(first: 20, topics: [ORDERS_CREATE]) { edges { node "
    "{ id topic endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } } }"
)

_CREATE_MUTATION = (
    "mutation($callbackUrl: URL!) { webhookSubscriptionCreate(topic: ORDERS_CREATE, "
    "webhookSubscription: {callbackUrl: $callbackUrl, format: JSON}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)

_UPDATE_MUTATION = (
    "mutation($id: ID!, $callbackUrl: URL!) { webhookSubscriptionUpdate(id: $id, "
    "webhookSubscription: {callbackUrl: $callbackUrl}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)


def _raise_on_user_errors(node: dict) -> None:  # type: ignore[type-arg]
    errors = node.get("userErrors") or []
    if errors:
        raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])


async def ensure_subscription(client: ShopifyClient, callback_url: str) -> str:
    data = await client._graphql(_LIST_QUERY)
    edges = (data.get("webhookSubscriptions") or {}).get("edges") or []
    for edge in edges:
        node = edge["node"]
        endpoint = node.get("endpoint") or {}
        if endpoint.get("callbackUrl") == callback_url:
            return "ok"
        result = await client._graphql(
            _UPDATE_MUTATION, {"id": node["id"], "callbackUrl": callback_url}
        )
        _raise_on_user_errors(result.get("webhookSubscriptionUpdate") or {})
        return "updated"
    result = await client._graphql(_CREATE_MUTATION, {"callbackUrl": callback_url})
    _raise_on_user_errors(result.get("webhookSubscriptionCreate") or {})
    return "created"
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: orders/create subscription self-heal (create/update on drift)"`

---

### Task 8: Jobs dispatcher (authenticated)

**Files:**
- Create: `backend/app/jobs/__init__.py` (empty), `backend/app/jobs/router.py`
- Modify: `backend/app/config/settings.py` (add `cron_secret: str = ""`), `backend/app/main.py` (include router)
- Test: `backend/tests/test_jobs_router.py`

**Interfaces:**
- Consumes: `ensure_subscription` (Task 7), `get_container()`, config key `public_base_url`.
- Produces: `POST|GET /internal/jobs/{name}` — 503 if `settings.cron_secret` empty; 403 if header `X-Cron-Secret` ≠ secret; 404 unknown job; 200 `{"job": name, "result": ...}`. Registered jobs: `ensure_subscription` (reads `public_base_url` from config; returns `{"error": "public_base_url not configured"}` if unset).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_jobs_router.py`:
```python
import httpx
import pytest

from app.deps import get_container, reset_container


@pytest.fixture(autouse=True)
def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", "topsecret")
    reset_container()
    yield
    reset_container()


async def call(name: str, secret: str | None) -> httpx.Response:
    from app.main import app as fastapi_app

    headers = {} if secret is None else {"X-Cron-Secret": secret}
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/internal/jobs/{name}", headers=headers)


async def test_wrong_secret_403() -> None:
    assert (await call("ensure_subscription", "nope")).status_code == 403
    assert (await call("ensure_subscription", None)).status_code == 403


async def test_unset_secret_503(monkeypatch: pytest.MonkeyPatch, master_key: str) -> None:
    monkeypatch.setenv("CRON_SECRET", "")
    reset_container()
    assert (await call("ensure_subscription", "")).status_code == 503


async def test_unknown_job_404() -> None:
    assert (await call("nope", "topsecret")).status_code == 404


async def test_ensure_subscription_without_base_url_reports_error() -> None:
    resp = await call("ensure_subscription", "topsecret")
    assert resp.status_code == 200
    assert resp.json() == {
        "job": "ensure_subscription",
        "result": {"error": "public_base_url not configured"},
    }
```

- [ ] **Step 2: Run to verify FAIL** — 404 (router missing)

- [ ] **Step 3: Implement**

Add to `Settings` in `backend/app/config/settings.py`: `cron_secret: str = ""`.

`backend/app/jobs/router.py`:
```python
import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import Container, get_container
from app.shopify.subscriptions import ensure_subscription

router = APIRouter()

JobFn = Callable[[Container], Awaitable[dict[str, Any]]]


async def _job_ensure_subscription(c: Container) -> dict[str, Any]:
    base_url = await c.config.get_plain("public_base_url")
    if not base_url:
        return {"error": "public_base_url not configured"}
    status = await ensure_subscription(c.shopify, f"{base_url.rstrip('/')}/webhooks/shopify")
    return {"status": status}


JOBS: dict[str, JobFn] = {
    "ensure_subscription": _job_ensure_subscription,
}


@router.api_route("/internal/jobs/{name}", methods=["GET", "POST"])
async def run_job(name: str, request: Request) -> JSONResponse:
    c = get_container()
    secret = c.settings.cron_secret
    if not secret:
        return JSONResponse({"error": "jobs disabled"}, status_code=503)
    provided = request.headers.get("X-Cron-Secret", "")
    if not hmac.compare_digest(provided, secret):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    job = JOBS.get(name)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    result = await job(c)
    return JSONResponse({"job": name, "result": result})
```

Modify `backend/app/main.py`: add `from app.jobs.router import router as jobs_router` and `app.include_router(jobs_router)`.

- [ ] **Step 4: Run to verify PASS** — full suite green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: authenticated jobs dispatcher with ensure_subscription job (F11)"`

---

### Task 9: Postgres implementations (asyncpg, gated tests)

**Files:**
- Create: `backend/app/store/pg_factory.py`, `backend/app/store/postgres.py`
- Test: `backend/tests/test_postgres_store.py`

**Interfaces:**
- Produces: `LazyPool(dsn: str)` with `async acquire()` context manager (pool created on first use — serverless cold-start rule); `PostgresConfigRepo(pool: LazyPool)` implementing `ConfigRepo` (UPSERT on set); `PostgresIngestStore(pool: LazyPool)` implementing `IngestStore` — ONE transaction: `INSERT processed_webhooks ON CONFLICT DO NOTHING` (0 rows → duplicate, short-circuit), `INSERT order_mappings ... ON CONFLICT (order_gid) DO UPDATE` (phone/status snapshot fields + `updated_at=now()`), `INSERT outbound_messages ON CONFLICT (dedupe_key) DO NOTHING` (rowcount → queued).

- [ ] **Step 1: Write the tests (gated — they SKIP without TEST_DATABASE_URL)**

`backend/tests/test_postgres_store.py`:
```python
import os
import uuid

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresConfigRepo, PostgresIngestStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


def mapping(gid: str) -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1,
        phone_e164="+911111111111", customer_name="A", email="a@b.c",
        language="en", financial_status_at_create="PENDING", is_cod=True,
    )


async def test_config_repo_roundtrip_upsert(pool: LazyPool) -> None:
    repo = PostgresConfigRepo(pool)
    key = f"test:{uuid.uuid4()}"
    assert await repo.get(key) is None
    await repo.set(key, "v1")
    await repo.set(key, "v2")
    assert await repo.get(key) == "v2"


async def test_ingest_atomic_and_idempotent(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    wh = f"wh-{uuid.uuid4()}"
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164="+911111111111", payload_json="{}",
    )
    first = await store.ingest_order_created(wh, "orders/create", mapping(gid), draft)
    assert (first.duplicate, first.queued) == (False, True)
    again = await store.ingest_order_created(wh, "orders/create", mapping(gid), draft)
    assert (again.duplicate, again.queued) == (True, False)
    other = await store.ingest_order_created(f"wh-{uuid.uuid4()}", "orders/create", mapping(gid), draft)
    assert (other.duplicate, other.queued) == (False, False)  # dedupe_key already used
```

- [ ] **Step 2: Run to verify SKIP offline** — `python -m pytest tests/test_postgres_store.py -v` → all SKIPPED (no TEST_DATABASE_URL).

- [ ] **Step 3: Implement**

`backend/app/store/pg_factory.py`:
```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg


class LazyPool:
    """Pool created on first acquire — never at import time (serverless cold-start rule)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(self._dsn, min_size=0, max_size=5)
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            yield conn

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
```

`backend/app/store/postgres.py`:
```python
from app.store.base import IngestResult, MappingUpsert, OutboundDraft
from app.store.pg_factory import LazyPool


class PostgresConfigRepo:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def get(self, key: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM app_config WHERE key = $1", key)
        return None if row is None else str(row["value"])

    async def set(self, key: str, value: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES ($1, $2, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
                key,
                value,
            )


class PostgresIngestStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.execute(
                    "INSERT INTO processed_webhooks (webhook_id, topic) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    webhook_id,
                    topic,
                )
                if inserted.endswith("0"):
                    return IngestResult(duplicate=True, queued=False)
                await conn.execute(
                    "INSERT INTO order_mappings (order_gid, order_name, order_number_int, "
                    "phone_e164, customer_name, email, language, financial_status_at_create, "
                    "is_cod) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (order_gid) DO UPDATE SET phone_e164 = $4, "
                    "customer_name = $5, email = $6, language = $7, updated_at = now()",
                    mapping.order_gid,
                    mapping.order_name,
                    mapping.order_number_int,
                    mapping.phone_e164,
                    mapping.customer_name,
                    mapping.email,
                    mapping.language,
                    mapping.financial_status_at_create,
                    mapping.is_cod,
                )
                queued = False
                if outbound is not None:
                    result = await conn.execute(
                        "INSERT INTO outbound_messages (dedupe_key, kind, phone_e164, "
                        "payload_json) VALUES ($1, $2, $3, $4) ON CONFLICT (dedupe_key) "
                        "DO NOTHING",
                        outbound.dedupe_key,
                        outbound.kind,
                        outbound.phone_e164,
                        outbound.payload_json,
                    )
                    queued = not result.endswith("0")
                return IngestResult(duplicate=False, queued=queued)
```

- [ ] **Step 4: Verify** — full suite still green offline (Postgres tests SKIP); ruff clean; `mypy app` clean. If a live `TEST_DATABASE_URL` is available (after Supabase arrives): apply schema (`python -m scripts.apply_schema` with `DATABASE_URL` set) then run `TEST_DATABASE_URL=... python -m pytest tests/test_postgres_store.py -v` → 2 passed.

- [ ] **Step 5: Commit** — `git commit -m "feat: asyncpg lazy pool, Postgres config repo and atomic IngestStore"`

---

### Task 10: Deps switch (Postgres when DATABASE_URL set) + final sweep + registries

**Files:**
- Modify: `backend/app/deps.py`
- Test: append to `backend/tests/test_health.py`
- Modify: `docs/memory/component_registry.md`, `docs/memory/api_registry.md`

**Interfaces:**
- Produces: `get_container()` chooses `PostgresConfigRepo` + `PostgresIngestStore` over a shared `LazyPool` when `settings.database_url` is non-empty, else in-memory (unchanged behavior for tests).

- [ ] **Step 1: Write the failing test (append to `backend/tests/test_health.py`)**

```python
def test_container_uses_postgres_when_database_url_set(
    master_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.store.postgres import PostgresConfigRepo, PostgresIngestStore

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    reset_container()
    c = get_container()
    assert isinstance(c.config_repo, PostgresConfigRepo)
    assert isinstance(c.ingest, PostgresIngestStore)
    reset_container()
```

(No connection is made — `LazyPool` connects only on first acquire.)

- [ ] **Step 2: Run to verify FAIL** — container still builds in-memory repos.

- [ ] **Step 3: Implement — modify `get_container()` in `backend/app/deps.py`**

```python
        settings = Settings()  # type: ignore[call-arg]
        vault = SecretVault(settings.app_master_key)
        if settings.database_url:
            pool = LazyPool(settings.database_url)
            config_repo: ConfigRepo = PostgresConfigRepo(pool)
            ingest: IngestStore = PostgresIngestStore(pool)
        else:
            config_repo = InMemoryConfigRepo()
            ingest = InMemoryIngestStore()
```
(with imports `from app.store.pg_factory import LazyPool` and `from app.store.postgres import PostgresConfigRepo, PostgresIngestStore`; rest of the container construction unchanged.)

- [ ] **Step 4: Full verification sweep**

`python -m pytest -q` (all green, Postgres tests SKIP offline) · `ruff check .` · `mypy app` · secrets grep (EMPTY).

- [ ] **Step 5: Update registries** — add to `docs/memory/component_registry.md`: phone.normalize_phone, shopify_signature.verify_shopify_hmac, shopify_orders (parser/language/eligibility), IngestStore + both impls, LazyPool, subscriptions.ensure_subscription, jobs router. Add to `docs/memory/api_registry.md`: `POST /webhooks/shopify`, `POST|GET /internal/jobs/{name}`; external: `webhookSubscriptionCreate/Update`.

- [ ] **Step 6: Commit** — `git commit -m "feat: Postgres/in-memory container switch; phase 2 complete + registry updates"`

---

## Self-Review (done at plan time)

- **Coverage:** ADR-001 (atomic ingest + outbox + 5xx-retry semantics) → Tasks 3/6/9; F2 (eligibility + staleness + dedupe-forever) → Tasks 4/6; F11 (jobs auth) → Task 8; F20 (payload gid + self-heal incl. URL drift) → Tasks 4/7; F15 (language rule) → Task 4; Level 4 schema (incl. future tables + `paused_until` F23 + `store_id` F22) → Task 2; serverless lazy pool → Task 9. Outbox DRAIN deliberately absent (Phase 3, needs the WhatsApp sender). Reconciliation sweep/backfill deliberately absent (Phase 3 jobs — they reuse `ingest_order_created(..., outbound=None)` which exists now).
- **Placeholders:** none — full code every step.
- **Type consistency:** `MappingUpsert`/`OutboundDraft`/`IngestResult` fields identical across Tasks 3/6/9; `ingest_order_created(webhook_id, topic, mapping, outbound)` signature identical in Protocol/memory/Postgres; test helpers `make_client`/`seed`/`grant_or` reused from Phase 1's `tests/test_client_graphql.py` (unchanged interfaces); `Container.ingest` used by Tasks 6/8/10.
