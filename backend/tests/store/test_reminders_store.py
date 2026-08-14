"""Reminder-sweep store primitives (Q17): stale-template_sent scan + original-payload lookup
+ idempotent re-enqueue. Fast in-memory equivalents of the gated Postgres tests.

The sweep window is anchored on the moment the mapping actually became ``template_sent``
(``updated_at``, stamped by ``set_mapping_status``), NOT ``created_at`` (webhook-ingest time) --
otherwise a backlog flushed hours after ingest would be reminded the instant its template sends.
It is bounded on BOTH ends: at least ``older_than_minutes`` old, but no older than
``max_age_minutes`` (so a historically-unanswered order is never mass-reminded on first run)."""

from datetime import UTC, datetime, timedelta

from app.store.base import MappingUpsert, OutboundDraft
from app.store.memory import InMemoryIngestStore

_CEILING = 24 * 60  # 24h, matching the reminder job's ceiling


def _mapping(gid: str, phone: str = "+911111111111") -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1, phone_e164=phone,
        customer_name="A B", email="a@b.c", language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )


def _draft(gid: str, phone: str = "+911111111111") -> OutboundDraft:
    return OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json='{"template": "order_confirmation_cod"}',
    )


async def _seed(store: InMemoryIngestStore, gid: str, phone: str = "+911111111111") -> None:
    await store.ingest_order_created(
        f"wh-{gid}", "orders/create", _mapping(gid, phone), _draft(gid, phone)
    )


def _age_updated(store: InMemoryIngestStore, gid: str, minutes: int) -> None:
    """Backdate the template-sent moment (updated_at), the field the sweep ages off."""
    store._mapping_updated_at[gid] = datetime.now(UTC) - timedelta(minutes=minutes)


async def test_stale_template_sent_returns_old_template_sent_mapping() -> None:
    store = InMemoryIngestStore()
    gid = "gid://shopify/Order/1"
    await _seed(store, gid)
    await store.set_mapping_status(gid, "template_sent")
    _age_updated(store, gid, minutes=90)  # older than the 60-minute threshold, within ceiling
    stale = await store.find_stale_template_sent(older_than_minutes=60, max_age_minutes=_CEILING)
    assert [m.order_gid for m in stale] == [gid]
    assert stale[0].status == "template_sent"


async def test_recent_template_sent_not_returned() -> None:
    store = InMemoryIngestStore()
    gid = "gid://shopify/Order/2"
    await _seed(store, gid)
    await store.set_mapping_status(gid, "template_sent")
    _age_updated(store, gid, minutes=30)  # newer than the threshold -> not stale
    assert (
        await store.find_stale_template_sent(older_than_minutes=60, max_age_minutes=_CEILING)
        == []
    )


async def test_window_anchors_on_template_sent_time_not_created_at() -> None:
    # HIGH #1: an order INGESTED hours ago but whose template only just SENT must NOT be reminded.
    # created_at is old, updated_at (the template-sent moment) is fresh -> excluded.
    store = InMemoryIngestStore()
    gid = "gid://shopify/Order/anchor"
    await _seed(store, gid)
    store._mapping_created_at[gid] = datetime.now(UTC) - timedelta(minutes=600)  # ingested 10h ago
    await store.set_mapping_status(gid, "template_sent")  # sent just now -> updated_at = now
    assert (
        await store.find_stale_template_sent(older_than_minutes=60, max_age_minutes=_CEILING)
        == []
    )


async def test_ceiling_excludes_too_old_and_includes_only_the_window() -> None:
    # HIGH #2: a mapping 45m past template_sent -> too recent; ~2h -> in window; 30h -> too old.
    store = InMemoryIngestStore()
    too_recent = "gid://shopify/Order/45m"
    in_window = "gid://shopify/Order/2h"
    too_old = "gid://shopify/Order/30h"
    for gid, minutes in ((too_recent, 45), (in_window, 120), (too_old, 30 * 60)):
        await _seed(store, gid)
        await store.set_mapping_status(gid, "template_sent")
        _age_updated(store, gid, minutes=minutes)
    stale = {
        m.order_gid
        for m in await store.find_stale_template_sent(
            older_than_minutes=60, max_age_minutes=_CEILING
        )
    }
    assert stale == {in_window}


async def test_tapped_mapping_excluded_even_when_old() -> None:
    store = InMemoryIngestStore()
    for gid, status in (
        ("gid://shopify/Order/3", "confirmed"),
        ("gid://shopify/Order/4", "cancel_requested"),
    ):
        await _seed(store, gid)
        await store.set_mapping_status(gid, status)
        _age_updated(store, gid, minutes=120)
    assert (
        await store.find_stale_template_sent(older_than_minutes=60, max_age_minutes=_CEILING)
        == []
    )


async def test_find_outbound_by_dedupe_key_returns_original_payload() -> None:
    store = InMemoryIngestStore()
    gid = "gid://shopify/Order/5"
    await _seed(store, gid)
    original = await store.find_outbound_by_dedupe_key(f"order_created:{gid}")
    assert original is not None
    assert original.payload_json == '{"template": "order_confirmation_cod"}'
    assert original.phone_e164 == "+911111111111"
    assert original.kind == "order_confirmation"
    assert await store.find_outbound_by_dedupe_key("order_created:missing") is None


async def test_find_outbound_by_dedupe_keys_batches_lookup() -> None:
    # MEDIUM #1: the sweep fetches every original payload in ONE round-trip, not one-per-row.
    store = InMemoryIngestStore()
    a, b = "gid://shopify/Order/A", "gid://shopify/Order/B"
    await _seed(store, a)
    await _seed(store, b)
    found = await store.find_outbound_by_dedupe_keys(
        [f"order_created:{a}", f"order_created:{b}", "order_created:missing"]
    )
    assert set(found) == {f"order_created:{a}", f"order_created:{b}"}
    assert found[f"order_created:{a}"].kind == "order_confirmation"
    assert await store.find_outbound_by_dedupe_keys([]) == {}


async def test_enqueue_outbound_is_idempotent_on_dedupe_key() -> None:
    store = InMemoryIngestStore()
    reminder = OutboundDraft(
        dedupe_key="order_reminder:gid://shopify/Order/6", kind="order_confirmation",
        phone_e164="+911111111111", payload_json="{}",
    )
    assert await store.enqueue_outbound(reminder) is True
    # Second insert of the SAME dedupe_key is a no-op -> the UNIQUE key is the exactly-once guard.
    assert await store.enqueue_outbound(reminder) is False
    views = [v for v in await store.recent_outbound(10) if v.dedupe_key == reminder.dedupe_key]
    assert len(views) == 1
    assert views[0].state == "queued"
