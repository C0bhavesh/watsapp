"""Fast unit test: the Postgres atomic-claim SQL shape (no real DB needed).

`claim_queued_outbound` must, in ONE round trip inside an explicit transaction, both select the
oldest queued rows AND flip them to 'processing' — with `FOR UPDATE SKIP LOCKED` so two overlapping
cron runs can never both claim the same row. A real-DB concurrency proof lives in
`tests/store/test_outbox_drain_pg.py`; this asserts the exact SQL the code sends so a review can
diff it against Postgres' semantics without a live database (cf. the 2026-08-12 "has tests !=
matches the real schema" learning — mock-backed tests validate request shape, not the live engine).
"""

from typing import Any

from app.store.base import OutboundClaim
from app.store.postgres import PostgresIngestStore


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.fetch_calls: list[tuple[str, tuple[Any, ...], bool]] = []
        self._in_txn = False

    def transaction(self) -> Any:
        conn = self

        class _Txn:
            async def __aenter__(self) -> "_Txn":
                conn._in_txn = True
                return self

            async def __aexit__(self, *exc: object) -> bool:
                conn._in_txn = False
                return False

        return _Txn()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, args, self._in_txn))
        return self._rows


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Acq:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return _Acq()


async def test_claim_sql_is_atomic_transactional_and_skip_locked() -> None:
    row = {
        "id": 7,
        "dedupe_key": "order_created:gid://shopify/Order/1",
        "phone_e164": "+911111111111",
        "payload_json": "{}",
        "attempts": 0,
    }
    conn = _FakeConn([row])
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    claims = await store.claim_queued_outbound(limit=25)

    assert len(conn.fetch_calls) == 1
    sql, args, ran_in_txn = conn.fetch_calls[0]
    assert ran_in_txn is True  # the CTE + row lock must run inside an explicit transaction
    assert "FOR UPDATE SKIP LOCKED" in sql  # concurrent claims skip already-locked rows
    assert "state = 'processing'" in sql  # atomically flips claimed rows out of 'queued'
    assert "WHERE state = 'queued'" in sql  # selects only queued rows
    assert "RETURNING id, dedupe_key, phone_e164, payload_json, attempts" in sql
    assert args == (25,)
    assert claims == [
        OutboundClaim(
            id=7,
            dedupe_key="order_created:gid://shopify/Order/1",
            phone_e164="+911111111111",
            payload_json="{}",
            attempts=0,
        )
    ]
