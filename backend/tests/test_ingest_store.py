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
    # mapping upserted, push already queued once
    assert result == IngestResult(duplicate=False, queued=False)
    assert len(store.outbound) == 1


async def test_ineligible_ingest_maps_without_queueing() -> None:
    store = InMemoryIngestStore()
    result = await store.ingest_order_created("wh1", "orders/create", mapping(), None)
    assert result == IngestResult(duplicate=False, queued=False)
    assert "gid://shopify/Order/1" in store.mappings and not store.outbound
