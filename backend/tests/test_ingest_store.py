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


# --- pending_delivery_confirmations + fulfillment shipment_status (RTO-aware delivery) ---

from datetime import UTC, datetime, timedelta  # noqa: E402

from app.shopify.models import Fulfillment, Order  # noqa: E402

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _delivered_order(gid: str = "gid://shopify/Order/9") -> Order:
    return Order(
        gid=gid, name="tavas9", email=None, phone="+911111111111", shipping_phone=None,
        billing_phone=None, financial_status="PENDING", fulfillment_status="FULFILLED",
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None, customer_locale="en",
    )


def _fulfillment(gid: str = "gid://shopify/Fulfillment/9") -> Fulfillment:
    return Fulfillment(
        gid=gid, status="success", tracking_company="Delhivery",
        tracking_number="AWB9", tracking_url="https://track/AWB9",
    )


async def test_record_pending_confirmation_is_idempotent() -> None:
    # A second record for the same fulfillment_gid must NOT change the stored due_at / phone
    # (ON CONFLICT (fulfillment_gid) DO NOTHING).
    store = InMemoryIngestStore()
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f1", order_gid="o1", phone_e164="+911111111111",
        due_at=_NOW + timedelta(hours=1),
    )
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f1", order_gid="o1", phone_e164="+912222222222",
        due_at=_NOW + timedelta(hours=2),
    )
    due = await store.due_delivery_confirmations(_NOW + timedelta(hours=3))
    assert len(due) == 1
    assert due[0].due_at == _NOW + timedelta(hours=1)
    assert due[0].phone_e164 == "+911111111111"


async def test_due_confirmations_respects_due_at_threshold() -> None:
    store = InMemoryIngestStore()
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f1", order_gid="o1", phone_e164="+911111111111",
        due_at=_NOW + timedelta(hours=2),
    )
    assert await store.due_delivery_confirmations(_NOW) == []
    due = await store.due_delivery_confirmations(_NOW + timedelta(hours=2))
    assert [r.fulfillment_gid for r in due] == ["f1"]


async def test_set_state_removes_from_due() -> None:
    store = InMemoryIngestStore()
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f1", order_gid="o1", phone_e164="+911111111111", due_at=_NOW,
    )
    await store.set_delivery_confirmation_state("f1", "sent")
    assert await store.due_delivery_confirmations(_NOW + timedelta(hours=5)) == []


async def test_due_confirmations_limit_and_ordering() -> None:
    store = InMemoryIngestStore()
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f3", order_gid="o", phone_e164="+913",
        due_at=_NOW - timedelta(hours=1),
    )
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f1", order_gid="o", phone_e164="+911",
        due_at=_NOW - timedelta(hours=3),
    )
    await store.record_pending_delivery_confirmation(
        fulfillment_gid="f2", order_gid="o", phone_e164="+912",
        due_at=_NOW - timedelta(hours=2),
    )
    due = await store.due_delivery_confirmations(_NOW, limit=2)
    assert [r.fulfillment_gid for r in due] == ["f1", "f2"]  # earliest due_at first, capped


async def test_set_shipment_status_surfaces_on_mirror_read() -> None:
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_delivered_order())
    await store.upsert_fulfillment("gid://shopify/Order/9", _fulfillment())
    await store.set_fulfillment_shipment_status("gid://shopify/Fulfillment/9", "delivered")
    order = await store.get_mirrored_order("gid://shopify/Order/9")
    assert order is not None
    assert order.fulfillments[0].shipment_status == "delivered"


async def test_shipment_status_is_monotonic_but_tracking_still_updates() -> None:
    # A terminal shipment_status ('rto') must NOT be overwritten by a later non-terminal one, but
    # the tracking_* fields ARE still written unconditionally.
    store = InMemoryIngestStore()
    await store.upsert_order_mirror(_delivered_order())
    await store.upsert_fulfillment("gid://shopify/Order/9", _fulfillment())
    await store.set_fulfillment_shipment_status("gid://shopify/Fulfillment/9", "rto")
    await store.set_fulfillment_shipment_status(
        "gid://shopify/Fulfillment/9", "in_transit", tracking_city="Pune",
    )
    order = await store.get_mirrored_order("gid://shopify/Order/9")
    assert order is not None
    assert order.fulfillments[0].shipment_status == "rto"
    assert order.fulfillments[0].tracking_city == "Pune"
