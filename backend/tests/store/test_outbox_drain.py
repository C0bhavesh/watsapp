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


async def test_set_mapping_status_and_reconcile_list() -> None:
    store = InMemoryIngestStore()
    await _seed(store, 2)
    gid1 = "gid://shopify/Order/1"
    await store.set_mapping_status(gid1, "cancel_requested")
    assert await store.orders_awaiting_cancel_reconcile() == [gid1]
    # A different status is not surfaced by the reconcile list.
    await store.set_mapping_status("gid://shopify/Order/2", "confirmed")
    assert await store.orders_awaiting_cancel_reconcile() == [gid1]


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
