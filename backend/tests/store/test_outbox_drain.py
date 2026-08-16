from datetime import UTC, datetime, timedelta

from app.store.base import MappingUpsert, OutboundClaim, OutboundDraft
from app.store.memory import InMemoryIngestStore


def _mapping(gid: str, phone: str = "+911111111111") -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164=phone,
        customer_name="A B", email="a@b.c", language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )


def _draft(gid: str, phone: str = "+911111111111") -> OutboundDraft:
    return OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json="{}",
    )


async def _seed(store: InMemoryIngestStore, n: int) -> None:
    for i in range(1, n + 1):
        gid = f"gid://shopify/Order/{i}"
        await store.ingest_order_created(f"wh{i}", "orders/create", _mapping(gid), _draft(gid))


async def test_claim_returns_only_queued_oldest_first() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 3)
    claims = await store.claim_queued_outbound(limit=10)
    assert all(isinstance(c, OutboundClaim) for c in claims)
    assert [c.dedupe_key for c in claims] == [
        "order_created:gid://shopify/Order/1",
        "order_created:gid://shopify/Order/2",
        "order_created:gid://shopify/Order/3",
    ]
    assert all(c.attempts == 0 for c in claims)
    assert all(c.phone_e164 == "+911111111111" for c in claims)


async def test_claim_respects_limit() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 3)
    claims = await store.claim_queued_outbound(limit=2)
    assert len(claims) == 2


async def test_claim_reclaims_stale_processing_row() -> None:
    # A row claimed by a drain invocation that was then killed mid-send is stranded in
    # 'processing'. A later claim MUST reclaim it once it is older than the staleness threshold,
    # else the message is lost forever (nothing else re-queues a 'processing' row).
    store = InMemoryIngestStore()
    await _seed(store, 1)
    key = "order_created:gid://shopify/Order/1"
    claim = (await store.claim_queued_outbound())[0]
    assert claim.dedupe_key == key
    meta = store._outbound_meta[key]
    assert meta.state == "processing"
    # Backdate its updated_at well past the reclaim threshold (simulating a dead invocation).
    meta.updated_at = datetime.now(UTC) - timedelta(minutes=15)
    reclaimed = await store.claim_queued_outbound()
    assert [c.dedupe_key for c in reclaimed] == [key]


async def test_claim_does_not_reclaim_recent_processing_row() -> None:
    # A genuinely-in-flight row (just claimed moments ago, recent updated_at) must NOT be
    # reclaimed by a concurrent/subsequent claim — that would double-send.
    store = InMemoryIngestStore()
    await _seed(store, 1)
    claim = (await store.claim_queued_outbound())[0]
    assert store._outbound_meta[claim.dedupe_key].state == "processing"
    assert await store.claim_queued_outbound() == []


async def test_ingest_returns_the_new_outbound_row_id() -> None:
    # The inline send path needs the freshly-queued row's id to claim exactly it.
    store = InMemoryIngestStore()
    gid = "gid://shopify/Order/1"
    result = await store.ingest_order_created("wh1", "orders/create", _mapping(gid), _draft(gid))
    assert result.queued is True
    assert result.outbound_id is not None
    claim = await store.claim_outbound_by_id(result.outbound_id)
    assert claim is not None
    assert claim.dedupe_key == f"order_created:{gid}"


async def test_ingest_outbound_id_none_when_nothing_queued() -> None:
    store = InMemoryIngestStore()
    gid = "gid://shopify/Order/1"
    # No draft -> nothing queued.
    r1 = await store.ingest_order_created("wh1", "orders/create", _mapping(gid), None)
    assert r1.queued is False
    assert r1.outbound_id is None
    # Duplicate dedupe_key -> ON CONFLICT DO NOTHING -> no fresh row (fresh webhook_id so it is
    # not a webhook-level duplicate; the outbox row already exists from a prior order).
    await store.ingest_order_created("wh2", "orders/create", _mapping(gid), _draft(gid))
    r3 = await store.ingest_order_created("wh3", "orders/create", _mapping(gid), _draft(gid))
    assert r3.outbound_id is None  # dedupe_key collided -> nothing re-queued


async def test_claim_outbound_by_id_claims_only_that_row() -> None:
    # The inline path must touch ONLY the row it created, never other queued rows (which the
    # backstop drain owns) — proving no shared-claim interaction.
    store = InMemoryIngestStore()
    await _seed(store, 2)
    one = "order_created:gid://shopify/Order/1"
    two = "order_created:gid://shopify/Order/2"
    target_id = store._outbound_meta[one].id

    claim = await store.claim_outbound_by_id(target_id)

    assert claim is not None
    assert claim.dedupe_key == one
    assert store._outbound_meta[one].state == "processing"
    assert store._outbound_meta[two].state == "queued"  # untouched


async def test_claim_outbound_by_id_returns_none_when_already_claimed() -> None:
    # A second claim of the same row (e.g. a racing backstop drain) must get None -> no double-send.
    store = InMemoryIngestStore()
    await _seed(store, 1)
    key = "order_created:gid://shopify/Order/1"
    target_id = store._outbound_meta[key].id
    assert await store.claim_outbound_by_id(target_id) is not None
    assert await store.claim_outbound_by_id(target_id) is None


async def test_claim_outbound_by_id_unknown_id_returns_none() -> None:
    store = InMemoryIngestStore()
    assert await store.claim_outbound_by_id(999) is None


async def test_mark_sent_transitions_and_leaves_queue() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 2)
    first = (await store.claim_queued_outbound())[0]
    await store.mark_outbound_sent(first.id, "wamid.123")
    remaining = await store.claim_queued_outbound()
    assert first.dedupe_key not in {c.dedupe_key for c in remaining}
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[first.dedupe_key].state == "sent"


async def test_mark_suppressed_transitions_state() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 1)
    claim = (await store.claim_queued_outbound())[0]
    await store.mark_outbound_suppressed(claim.id)
    assert not await store.claim_queued_outbound()
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[claim.dedupe_key].state == "suppressed"


async def test_mark_undeliverable_records_code() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 1)
    claim = (await store.claim_queued_outbound())[0]
    await store.mark_outbound_undeliverable(claim.id, "131026")
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    view = views[claim.dedupe_key]
    assert view.state == "undeliverable"
    assert view.last_error_code == "131026"


async def test_bump_stays_queued_until_cap_then_fails() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 1)
    claim = (await store.claim_queued_outbound())[0]
    for _ in range(4):
        assert await store.bump_outbound_attempt(claim.id, "500", max_attempts=5) == "queued"
    assert await store.bump_outbound_attempt(claim.id, "500", max_attempts=5) == "failed"
    assert not await store.claim_queued_outbound()
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    view = views[claim.dedupe_key]
    assert view.state == "failed"
    assert view.attempts == 5
    assert view.last_error_code == "500"


async def test_reconcile_claim_flips_status_and_is_not_double_claimed() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 2)
    gid1 = "gid://shopify/Order/1"
    await store.set_mapping_status(gid1, "cancel_requested")
    # The claim returns the row AND flips it out of 'cancel_requested' so a second overlapping
    # reconcile run can never re-claim (and thus re-send cod_cancel for) the same order.
    assert await store.orders_awaiting_cancel_reconcile() == [gid1]
    assert store._mapping_status[gid1] == "cancel_reconciling"
    assert await store.orders_awaiting_cancel_reconcile() == []
    # A different status is never surfaced by the reconcile claim.
    await store.set_mapping_status("gid://shopify/Order/2", "confirmed")
    assert await store.orders_awaiting_cancel_reconcile() == []


async def test_reconcile_claim_reclaims_stale_reconciling_row() -> None:
    # A row a killed reconcile invocation flipped to 'cancel_reconciling' but never finalized or
    # reverted would otherwise be stranded forever (nothing else re-queues it). Once older than the
    # staleness threshold it MUST be reclaimed -- mirroring claim_queued_outbound's stale-processing
    # reclaim -- while a freshly-claimed row is NOT (that would double-send).
    store = InMemoryIngestStore()
    await _seed(store, 1)
    gid = "gid://shopify/Order/1"
    await store.set_mapping_status(gid, "cancel_requested")
    assert await store.orders_awaiting_cancel_reconcile() == [gid]
    assert await store.orders_awaiting_cancel_reconcile() == []  # fresh -> not reclaimed
    store._mapping_updated_at[gid] = datetime.now(UTC) - timedelta(minutes=15)
    assert await store.orders_awaiting_cancel_reconcile() == [gid]  # stale -> reclaimed


async def test_record_order_action_persists() -> None:
    store = InMemoryIngestStore()
    await store.record_order_action(
        "gid://shopify/Order/1", "confirm", "+919999999999", "wamid.1", "ok", None
    )
    assert len(store.order_actions) == 1
    row = store.order_actions[0]
    assert row["order_gid"] == "gid://shopify/Order/1"
    assert row["action"] == "confirm"
    assert row["actor_wa_id"] == "+919999999999"
    assert row["result"] == "ok"
