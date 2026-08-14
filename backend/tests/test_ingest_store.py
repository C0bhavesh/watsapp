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
    assert (result.duplicate, result.queued) == (False, True)
    assert ("wh1", "orders/create") in store.webhooks
    assert "gid://shopify/Order/1" in store.mappings
    assert "order_created:gid://shopify/Order/1" in store.outbound
    # outbound_id carries the freshly-queued row's id (for the inline send to claim exactly it).
    assert result.outbound_id is not None
    assert result == IngestResult(duplicate=False, queued=True, outbound_id=result.outbound_id)


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
    # mapping upserted, push already queued once -> no fresh row queued.
    assert result == IngestResult(duplicate=False, queued=False)
    assert len(store.outbound) == 1


async def test_ineligible_ingest_maps_without_queueing() -> None:
    store = InMemoryIngestStore()
    result = await store.ingest_order_created("wh1", "orders/create", mapping(), None)
    assert result == IngestResult(duplicate=False, queued=False)
    assert "gid://shopify/Order/1" in store.mappings and not store.outbound


def _state(store: InMemoryIngestStore, dedupe_key: str) -> str:
    meta = store._outbound_meta[dedupe_key]
    return meta.state


async def test_claim_transitions_queued_to_processing() -> None:
    # The atomic claim flips 'queued' -> 'processing' (matching the Postgres CTE claim), so a
    # claimed row is never handed out again by a later claim while it is in flight.
    store = InMemoryIngestStore()
    await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    key = "order_created:gid://shopify/Order/1"
    assert _state(store, key) == "queued"

    (claim,) = await store.claim_queued_outbound()

    assert _state(store, key) == "processing"
    # A second claim finds nothing queued -> the same row is never handed out twice.
    assert await store.claim_queued_outbound() == []


async def test_success_flow_queued_processing_sent() -> None:
    store = InMemoryIngestStore()
    await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    key = "order_created:gid://shopify/Order/1"
    (claim,) = await store.claim_queued_outbound()
    assert _state(store, key) == "processing"
    await store.mark_outbound_sent(claim.id, "wamid.1")
    assert _state(store, key) == "sent"
    assert await store.claim_queued_outbound() == []  # a sent row is never re-claimed


async def test_transport_failure_returns_processing_to_queued_until_cap() -> None:
    # A retryable failure must move the row back to 'queued' (re-claimable by a later cron run),
    # never leave it stuck in 'processing'. Only at max_attempts does it become terminal 'failed'.
    store = InMemoryIngestStore()
    await store.ingest_order_created("wh1", "orders/create", mapping(), outbound())
    key = "order_created:gid://shopify/Order/1"

    states: list[str] = []
    for _ in range(3):
        (claim,) = await store.claim_queued_outbound()
        assert _state(store, key) == "processing"  # never stuck: re-claimable each round
        states.append(await store.bump_outbound_attempt(claim.id, "transport", max_attempts=3))

    assert states == ["queued", "queued", "failed"]
    assert _state(store, key) == "failed"
    assert await store.claim_queued_outbound() == []  # terminal -> never re-claimed


async def test_terminal_rows_are_never_reclaimed() -> None:
    # A mixed-state table: only the 'queued' row is ever handed out; sent/failed/undeliverable
    # rows (and a suppressed one) are all skipped by the WHERE state='queued' claim.
    store = InMemoryIngestStore()
    for i, state in enumerate(("sent", "failed", "undeliverable", "suppressed", "queued")):
        gid = f"gid://shopify/Order/{i}"
        await store.ingest_order_created(f"wh{i}", "orders/create", mapping(gid), outbound(gid))
        store._outbound_meta[f"order_created:{gid}"].state = state

    claims = await store.claim_queued_outbound()

    assert [c.dedupe_key for c in claims] == ["order_created:gid://shopify/Order/4"]


async def test_find_mappings_by_phone_returns_matches_only() -> None:
    from app.store.base import MappingUpsert
    from app.store.memory import InMemoryIngestStore

    store = InMemoryIngestStore()
    await store.ingest_order_created(
        "wh1", "orders/create",
        MappingUpsert(
            order_gid="gid://1", order_name="tavas1", order_number_int=1,
            phone_e164="+919999999999", customer_name=None, email=None,
            language="en", financial_status_at_create=None, is_cod=False,
        ),
        None,
    )
    await store.ingest_order_created(
        "wh2", "orders/create",
        MappingUpsert(
            order_gid="gid://2", order_name="tavas2", order_number_int=2,
            phone_e164="+918888888888", customer_name=None, email=None,
            language="en", financial_status_at_create=None, is_cod=False,
        ),
        None,
    )
    matches = await store.find_mappings_by_phone("+919999999999")
    assert [m.order_gid for m in matches] == ["gid://1"]


async def test_find_mappings_by_phone_no_match_returns_empty() -> None:
    from app.store.memory import InMemoryIngestStore

    store = InMemoryIngestStore()
    assert await store.find_mappings_by_phone("+910000000000") == []


async def test_count_orders_by_phone_counts_matches_only() -> None:
    from app.store.base import MappingUpsert
    from app.store.memory import InMemoryIngestStore

    store = InMemoryIngestStore()
    for i in range(3):
        await store.ingest_order_created(
            f"wh{i}", "orders/create",
            MappingUpsert(
                order_gid=f"gid://{i}", order_name=f"tavas{i}", order_number_int=i,
                phone_e164="+919999999999", customer_name=None, email=None,
                language="en", financial_status_at_create=None, is_cod=False,
            ),
            None,
        )
    assert await store.count_orders_by_phone("+919999999999") == 3
    assert await store.count_orders_by_phone("+910000000000") == 0
