"""DPDP erasure/retention on the in-memory ingest store (delete_by_phone / purge_older_than)."""

from datetime import UTC, datetime

from app.store.base import DeletionResult, MappingUpsert, OutboundDraft
from app.store.memory import InMemoryIngestStore


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
    # No conversation layer writes to the in-memory store yet.
    assert result.conversations == 0
    assert result.messages == 0
    # The other number's rows survive.
    assert set(store.mappings) == {"gid://shopify/Order/3"}
    assert set(store.outbound) == {"order_created:gid://shopify/Order/3"}


async def test_delete_by_phone_no_match_returns_zeros() -> None:
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")

    result = await store.delete_by_phone("+910000000000")

    assert result == DeletionResult(
        order_mappings=0, outbound_messages=0, conversations=0, messages=0
    )
    assert set(store.mappings) == {"gid://shopify/Order/1"}


async def test_purge_older_than_inmemory_is_noop_without_timestamps() -> None:
    # In-memory rows carry no created_at, so age-based purge deletes nothing; the real
    # age filter is exercised against Postgres. It must still honour the Protocol cleanly.
    store = InMemoryIngestStore()
    await _seed(store, "gid://shopify/Order/1", "+919111111111")

    result = await store.purge_older_than(datetime.now(UTC))

    assert result == DeletionResult(
        order_mappings=0, outbound_messages=0, conversations=0, messages=0
    )
    assert set(store.mappings) == {"gid://shopify/Order/1"}
