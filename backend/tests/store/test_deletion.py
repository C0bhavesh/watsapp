"""DPDP erasure/retention on the in-memory ingest store (delete_by_phone / purge_older_than).

Also exercises the Postgres SQL surface via a fake connection that records executed
statements — proving delete_by_phone / purge_older_than reach every phone-bearing table
(order_mappings, outbound_messages, conversations, messages, pending_actions, order_actions,
plus the age-only processed_messages purge) without needing a live database.
"""

from datetime import UTC, datetime

from app.store.base import DeletionResult, MappingUpsert, OutboundDraft
from app.store.memory import InMemoryIngestStore
from app.store.postgres import PostgresIngestStore


def _mapping(gid: str, phone: str) -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid,
        order_name="tavas1",
        order_number_int=1,
        phone_e164=phone,
        customer_name="A",
        email="a@b.c",
        language="en",
        financial_status_at_create="PENDING",
        is_cod=True,
    )


def _draft(gid: str, phone: str) -> OutboundDraft:
    return OutboundDraft(
        dedupe_key=f"order_created:{gid}",
        kind="order_confirmation",
        phone_e164=phone,
        payload_json="{}",
    )


async def _seed(store: InMemoryIngestStore, gid: str, phone: str) -> None:
    await store.ingest_order_created(
        f"wh-{gid}", "orders/create", _mapping(gid, phone), _draft(gid, phone)
    )


async def test_delete_by_phone_removes_only_that_number() -> None:
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")
    await _seed(store, "gid://shopify/Order/2", "+919111111111")
    await _seed(store, "gid://shopify/Order/3", "+919222222222")

    result = await store.delete_by_phone("+919111111111")

    assert isinstance(result, DeletionResult)
    assert result.order_mappings == 2
    assert result.outbound_messages == 2
    # No conversation/action layer writes to the in-memory store yet.
    assert result.conversations == 0
    assert result.messages == 0
    assert result.pending_actions == 0
    assert result.order_actions == 0
    # The other number's rows survive.
    assert set(store.mappings) == {"gid://shopify/Order/3"}
    assert set(store.outbound) == {"order_created:gid://shopify/Order/3"}


async def test_delete_by_phone_no_match_returns_zeros() -> None:
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")

    result = await store.delete_by_phone("+910000000000")

    assert result == DeletionResult(
        order_mappings=0,
        outbound_messages=0,
        conversations=0,
        messages=0,
        pending_actions=0,
        order_actions=0,
    )
    assert set(store.mappings) == {"gid://shopify/Order/1"}


async def test_purge_older_than_inmemory_is_noop_without_timestamps() -> None:
    # In-memory rows carry no created_at, so age-based purge deletes nothing; the real
    # age filter is exercised against Postgres. It must still honour the Protocol cleanly.
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")

    result = await store.purge_older_than(datetime.now(UTC))

    assert result == DeletionResult(
        order_mappings=0,
        outbound_messages=0,
        conversations=0,
        messages=0,
        pending_actions=0,
        order_actions=0,
    )
    assert set(store.mappings) == {"gid://shopify/Order/1"}


class _FakeTx:
    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeConn:
    """Records executed SQL so we can assert which tables a purge touches (no live DB)."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, sql: str, *_: object) -> str:
        self.executed.append(" ".join(sql.split()))
        return "DELETE 0"


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _targets(conn: _FakeConn) -> set[str]:
    return {sql.split("DELETE FROM ", 1)[1].split(" ", 1)[0] for sql in conn.executed}


async def test_pg_delete_by_phone_targets_every_phone_bearing_table() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.delete_by_phone("+919111111111")

    tables = _targets(conn)
    assert "order_mappings" in tables
    assert "outbound_messages" in tables
    assert "conversations" in tables
    assert "messages" in tables
    assert "pending_actions" in tables
    assert "order_actions" in tables
    # pending_actions is phone-scoped on wa_id; order_actions on actor_wa_id.
    joined = " ".join(conn.executed)
    assert "DELETE FROM pending_actions WHERE wa_id = $1" in joined
    assert "DELETE FROM order_actions WHERE actor_wa_id = $1" in joined


async def test_pg_delete_by_phone_returns_new_counts() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    result = await store.delete_by_phone("+919111111111")

    assert result.pending_actions == 0
    assert result.order_actions == 0


async def test_pg_purge_older_than_ages_actions_and_dedupe_tables() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.purge_older_than(datetime.now(UTC))

    joined = " ".join(conn.executed)
    assert "DELETE FROM pending_actions WHERE created_at < $1" in joined
    assert "DELETE FROM order_actions WHERE created_at < $1" in joined
    # processed_messages is a dedupe table: not phone-scoped, aged on its own received_at.
    assert "DELETE FROM processed_messages WHERE received_at < $1" in joined
