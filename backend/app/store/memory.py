from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.core.delivery_status import should_apply_delivery_status
from app.core.exchange_models import ExchangeRequest, ExchangeStatus
from app.shopify.models import Customer, Fulfillment, LineItem, Order, normalize_order_name
from app.store.base import (
    ConversationSummary,
    DeletionResult,
    IngestResult,
    MappingUpsert,
    MappingView,
    MessageRetryInfo,
    OrderActionEntry,
    OutboundClaim,
    OutboundDraft,
    OutboundEntry,
    OutboundRetryInfo,
    OutboundView,
    StoredMessage,
)
from app.store.postgres import MAX_MIRROR_FULFILLMENTS, _e164


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fulfillment_update_wins(existing: Fulfillment, incoming: Fulfillment) -> bool:
    """Mirror the Postgres shopify_updated_at guard: a NULL stored stamp is always overwritable;
    otherwise the incoming write must carry a stamp >= the stored one (an incoming NULL loses),
    so a replayed older fulfillments/create can't revert good tracking."""
    stored = _parse_iso(existing.updated_at)
    if stored is None:
        return True
    incoming_ts = _parse_iso(incoming.updated_at)
    if incoming_ts is None:
        return False
    return incoming_ts >= stored

# A 'processing' row older than this is treated as abandoned by a dead invocation and re-claimed
# (mirrors the Postgres claim's `interval '10 minutes'`). There is no real concurrency in the
# in-memory store, but the staleness semantics must not regress behind Postgres's.
_PROCESSING_STALE = timedelta(minutes=10)

# Cap on the reminder sweep, mirroring the Postgres query's LIMIT. Bounds a single tick's work on
# the shared pool (matches the sibling capped reads: find_mappings_by_phone LIMIT 20, mirror LIMIT
# 10) — a leftover stale row is just picked up on the next tick.
_STALE_SWEEP_LIMIT = 25


@dataclass
class _MessageRow:
    """Mutable per-message state (id, wamid, delivery_status) tracked internally, exactly as
    ``_OutboundRow`` backs the frozen ``OutboundDraft``. The frozen ``StoredMessage`` is only the
    read-boundary VIEW (``_message_view``): it carries the public fields (role/content/created_at/
    delivery_status) and hides the internal id + wamid, mirroring how ``OutboundView`` hides the
    internal row's bookkeeping."""

    id: int
    role: str
    content: str
    created_at: str | None
    wamid: str | None
    delivery_status: str | None
    # Count of delivery-failure retry attempts spent on this AI reply (the in-memory analogue of
    # messages.retry_count). record_message_retry increments it; the resend cap is checked against
    # it. Defaulted so the existing append_message construction site stays unchanged.
    retry_count: int = 0
    # Latest Meta delivery-failure error code captured from a 'failed' status (the in-memory
    # analogue of messages.last_error_code). Written by apply_message_delivery_status when a code
    # is present; no public view surfaces it. Defaulted so append_message stays unchanged.
    last_error_code: str | None = None
    # Display-only marker distinguishing a manually-typed admin reply from an AI-generated one.
    # Defaulted to None (AI) so the existing append_message construction site stays unchanged.
    sender: str | None = None


def _message_view(row: _MessageRow) -> StoredMessage:
    """Convert an internal mutable row to its frozen public view at the read boundary."""
    return StoredMessage(
        role=row.role,
        content=row.content,
        created_at=row.created_at,
        delivery_status=row.delivery_status,
        sender=row.sender,
    )


@dataclass
class _OutboundRow:
    """Mutable per-row outbox state tracked alongside the frozen ``OutboundDraft``."""

    id: int
    state: str
    attempts: int
    last_error_code: str | None
    template_wamid: str | None
    # Latest Meta delivery/read status for the template send (None until a status webhook lands).
    # The in-memory analogue of outbound_messages.delivery_status.
    delivery_status: str | None
    # Stamp of the last state transition — the in-memory analogue of the Postgres `updated_at`
    # column. Used only to decide whether a 'processing' row is stale enough to reclaim.
    updated_at: datetime
    # Immutable enqueue time — the in-memory analogue of outbound_messages.created_at (never
    # re-stamped on a state transition, unlike updated_at). distinct_outbound_phones orders on it
    # so the recency ranking matches the Postgres MAX(created_at) DESC.
    created_at: datetime
    # Count of delivery-failure retry attempts spent on this send (the in-memory analogue of
    # outbound_messages.retry_count). record_outbound_retry increments it; the resend cap is
    # checked against it. Defaulted so the existing _enqueue construction site stays unchanged.
    retry_count: int = 0


class InMemoryConfigRepo:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._knowledge: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        self._data[key] = value

    async def get_knowledge_override(self, kind: str) -> str | None:
        return self._knowledge.get(kind)

    async def set_knowledge_override(self, kind: str, content: str) -> None:
        self._knowledge[kind] = content

    async def get_knowledge_overrides(self, kinds: list[str]) -> dict[str, str | None]:
        return {k: self._knowledge.get(k) for k in kinds}

    async def bump_config_int(self, key: str) -> None:
        self._data[key] = str(int(self._data.get(key, "0")) + 1)


class InMemoryIngestStore:
    def __init__(self) -> None:
        self.webhooks: set[tuple[str, str]] = set()
        self.mappings: dict[str, MappingUpsert] = {}
        self.outbound: dict[str, OutboundDraft] = {}
        # Phase 5: outbox drain state kept parallel to the frozen drafts, plus per-order
        # status and an audit list. `self.outbound` stays a dict[str, OutboundDraft] so
        # existing assertions on it are unchanged.
        self._outbound_meta: dict[str, _OutboundRow] = {}
        self._outbound_by_id: dict[int, str] = {}
        self._outbound_next_id = 1
        self._mapping_status: dict[str, str] = {}
        # Creation timestamp per mapping — the in-memory analogue of order_mappings.created_at,
        # stamped once on first ingest (Postgres never resets it on ON CONFLICT DO UPDATE) and
        # surfaced as MappingView.created_at.
        self._mapping_created_at: dict[str, datetime] = {}
        # Last-write timestamp per mapping — the in-memory analogue of order_mappings.updated_at,
        # re-stamped now() on every ingest upsert and every set_mapping_status (mirrors Postgres'
        # `updated_at = now()`). find_stale_template_sent ages off THIS, not created_at, so a
        # mapping that only just became 'template_sent' (e.g. a backlog flush hours after ingest)
        # is NOT reminded instantly. tests backdate it to simulate an old template-sent order.
        self._mapping_updated_at: dict[str, datetime] = {}
        self.order_actions: list[dict[str, str | None]] = []
        self.customers: dict[str, Customer] = {}
        self.orders: dict[str, Order] = {}
        self.order_items: dict[str, tuple[LineItem, ...]] = {}
        # order_gid -> {fulfillment_gid: Fulfillment}. Keyed by fulfillment gid so a
        # fulfillments/update replaces the same shipment in place; the outer dict-of-dicts models
        # split shipments (several fulfillments per order). Kept separate from `self.orders` so it
        # survives an order re-mirror, exactly like the Postgres fulfillments table + FK.
        self.fulfillments: dict[str, dict[str, Fulfillment]] = {}

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
        self._mapping_created_at.setdefault(mapping.order_gid, datetime.now(UTC))
        self._mapping_updated_at[mapping.order_gid] = datetime.now(UTC)
        outbound_id = self._enqueue(outbound) if outbound is not None else None
        return IngestResult(
            duplicate=False, queued=outbound_id is not None, outbound_id=outbound_id
        )

    def _enqueue(self, outbound: OutboundDraft) -> int | None:
        """Queue one draft, idempotent on dedupe_key (mirrors Postgres ON CONFLICT DO NOTHING).

        Shared by ingest_order_created (original push) and enqueue_outbound (reminder re-queue) so
        the outbox bookkeeping (id, state, updated_at, id index) is created identically for both.
        Returns the new row's id, or None if the dedupe_key already existed (nothing queued).
        """
        if outbound.dedupe_key in self.outbound:
            return None
        self.outbound[outbound.dedupe_key] = outbound
        now = datetime.now(UTC)
        row = _OutboundRow(
            id=self._outbound_next_id,
            state="queued",
            attempts=0,
            last_error_code=None,
            template_wamid=None,
            delivery_status=None,
            updated_at=now,
            created_at=now,
        )
        self._outbound_meta[outbound.dedupe_key] = row
        self._outbound_by_id[row.id] = outbound.dedupe_key
        self._outbound_next_id += 1
        return row.id

    async def upsert_customer(self, customer: Customer) -> None:
        self.customers[customer.gid] = customer

    async def upsert_order_mirror(self, order: Order) -> None:
        # Normalize phones on write with the SAME `_e164` helper Postgres uses, so the two
        # IngestStore impls no longer diverge (delete_by_phone / lookups compare E.164). An
        # unparseable value is kept verbatim (degrade, don't discard).
        order = replace(
            order,
            phone=_e164(order.phone),
            shipping_phone=_e164(order.shipping_phone),
            billing_phone=_e164(order.billing_phone),
            customer=(
                replace(order.customer, phone=_e164(order.customer.phone))
                if order.customer is not None
                else None
            ),
        )
        if order.customer is not None:
            await self.upsert_customer(order.customer)
        self.orders[order.gid] = order
        self.order_items[order.gid] = order.line_items
        # Persist any fulfillments the order carries (the backfill attaches them from the separate
        # live fetch); webhook-mirrored orders carry none so this is a no-op. The order row now
        # exists, so upsert_fulfillment's existence check passes and its guard applies.
        for fulfillment in order.fulfillments[:MAX_MIRROR_FULFILLMENTS]:
            await self.upsert_fulfillment(order.gid, fulfillment)

    async def upsert_fulfillment(self, order_gid: str, fulfillment: Fulfillment) -> None:
        # Skip a fulfillment for an order we have not mirrored -- parity with the Postgres FK
        # (fulfillments.order_gid REFERENCES orders(gid)), which would reject it. Never raises.
        if order_gid not in self.orders:
            return
        existing = self.fulfillments.get(order_gid, {}).get(fulfillment.gid)
        # Out-of-order-delivery guard, mirroring the Postgres shopify_updated_at WHERE clause.
        if existing is not None and not _fulfillment_update_wins(existing, fulfillment):
            return
        # delivered_at parity with the Postgres COALESCE: it is supplied ONLY by the live GraphQL
        # read (the REST webhook payload has no delivery date), so a winning webhook update carries
        # delivered_at=None. A delivery date is monotonic, so keep a previously captured one rather
        # than let the newer (webhook) write wipe it.
        if (
            existing is not None
            and fulfillment.delivered_at is None
            and existing.delivered_at is not None
        ):
            fulfillment = replace(fulfillment, delivered_at=existing.delivered_at)
        self.fulfillments.setdefault(order_gid, {})[fulfillment.gid] = fulfillment

    def _with_fulfillments(self, order: Order | None) -> Order | None:
        if order is None:
            return None
        stored = self.fulfillments.get(order.gid)
        if not stored:
            return order
        return replace(order, fulfillments=tuple(stored.values()))

    async def customer_exists(self, gid: str) -> bool:
        return gid in self.customers

    async def get_mirrored_order(self, gid: str) -> Order | None:
        return self._with_fulfillments(self.orders.get(gid))

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        for order in self.orders.values():
            if order.name == name:
                return self._with_fulfillments(order)
        return None

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        # Q16 (docs/FR/client-decisions-all.md, ANSWERED 2026-08-12): this chat-Q&A lookup matches
        # the buyer's own `o.phone` OR its `o.shipping_phone`. The store's Shopflo checkout often
        # leaves the order's top-level phone empty while the shipping contact number is reliably
        # present (the order-confirmation push falls back to it for the same reason), so o.phone
        # alone would miss many real orders. Still NARROWER than delete_by_phone's three-column
        # erasure predicate — billing stays excluded (never asked for), erasure and disclosure have
        # different safety directions.
        # Cap matches the Postgres impl (10) so the two do not silently diverge; no ordering
        # requirement here since this store is test/dev-only.
        matches = [
            o for o in self.orders.values() if phone_e164 in (o.phone, o.shipping_phone)
        ]
        return [self._with_fulfillments(o) or o for o in matches[:10]]

    async def recent_mappings(self, limit: int) -> list[MappingView]:
        views = [
            MappingView(
                order_gid=m.order_gid,
                order_name=m.order_name,
                phone_e164=m.phone_e164,
                status=self._mapping_status.get(m.order_gid, "pending"),
                is_cod=m.is_cod,
                created_at=None,
            )
            for m in self.mappings.values()
        ]
        return list(reversed(views))[:limit]

    async def recent_outbound(self, limit: int) -> list[OutboundView]:
        views = [
            OutboundView(
                dedupe_key=o.dedupe_key,
                state=self._outbound_meta[key].state,
                kind=o.kind,
                phone_e164=o.phone_e164,
                attempts=self._outbound_meta[key].attempts,
                last_error_code=self._outbound_meta[key].last_error_code,
                created_at=None,
                delivery_status=self._outbound_meta[key].delivery_status,
            )
            for key, o in self.outbound.items()
        ]
        return list(reversed(views))[:limit]

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        matches = [m for m in self.mappings.values() if m.phone_e164 == phone_e164]
        views = [
            MappingView(
                order_gid=m.order_gid,
                order_name=m.order_name,
                phone_e164=m.phone_e164,
                status=self._mapping_status.get(m.order_gid, "pending"),
                is_cod=m.is_cod,
                created_at=None,
            )
            for m in matches
        ]
        return list(reversed(views))[:limit]

    async def find_outbound_by_phone(
        self, phone_e164: str, limit: int = 100
    ) -> list[OutboundEntry]:
        matches = [
            (dedupe_key, draft) for dedupe_key, draft in self.outbound.items()
            if draft.phone_e164 == phone_e164
        ]
        return [
            OutboundEntry(
                dedupe_key=dedupe_key,
                kind=draft.kind,
                state=self._outbound_meta[dedupe_key].state,
                payload_json=draft.payload_json,
                created_at=self._outbound_meta[dedupe_key].updated_at.isoformat(),
                delivery_status=self._outbound_meta[dedupe_key].delivery_status,
            )
            for dedupe_key, draft in matches[-limit:]
        ]

    async def find_order_actions_by_wa_ids(
        self, wa_ids: list[str], limit: int = 100
    ) -> list[OrderActionEntry]:
        candidates = set(wa_ids)
        matches = [row for row in self.order_actions if row.get("actor_wa_id") in candidates]
        return [
            OrderActionEntry(
                order_gid=str(row["order_gid"]),
                action=str(row["action"]),
                result=str(row["result"]),
                created_at=row.get("created_at"),
            )
            for row in matches[-limit:]
        ]

    async def distinct_outbound_phones(self, limit: int = 100) -> list[tuple[str, str | None]]:
        # Recency-ordered (MAX(created_at) DESC) to mirror the Postgres impl: the LIMIT must keep
        # the most-recently-active phones, not an insertion/lexicographic-first slice. Insertion
        # order alone is not enough -- a test (or a real re-enqueue) can produce phones whose newest
        # outbound is not their first, so group by phone taking the latest created_at, then sort.
        latest: dict[str, datetime] = {}
        for key, draft in self.outbound.items():
            created = self._outbound_meta[key].created_at
            if draft.phone_e164 not in latest or created > latest[draft.phone_e164]:
                latest[draft.phone_e164] = created
        ordered = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)
        return [(phone, ts.isoformat()) for phone, ts in ordered[:limit]]

    async def distinct_order_action_wa_ids(
        self, limit: int = 100
    ) -> list[tuple[str, str | None]]:
        # Same recency ordering as the Postgres impl (MAX(created_at) DESC). order_actions rows
        # carry an ISO created_at string; compare as datetimes (a zero-microsecond stamp serializes
        # shorter, so a naive string compare could misorder). None wa_id / bad stamp is skipped.
        latest: dict[str, datetime] = {}
        for row in self.order_actions:
            wa_id = row.get("actor_wa_id")
            created = _parse_iso(row.get("created_at"))
            if not isinstance(wa_id, str) or created is None:
                continue
            if wa_id not in latest or created > latest[wa_id]:
                latest[wa_id] = created
        ordered = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)
        return [(wa_id, ts.isoformat()) for wa_id, ts in ordered[:limit]]

    async def find_stale_template_sent(
        self, older_than_minutes: int, max_age_minutes: int
    ) -> list[MappingView]:
        # Q17: a mapping still at status 'template_sent' whose template was SENT between
        # `older_than_minutes` and `max_age_minutes` ago = the customer never tapped Confirm/Cancel
        # in the window. Anchored on updated_at (the template-sent moment), NOT created_at: a
        # backlog flushed hours after ingest would otherwise be reminded the instant it sends.
        # Bounded on both ends: the upper ceiling stops a historically-unanswered order (mappings
        # are kept indefinitely, client Q15) from being mass-reminded on first run. Capped +
        # ordered like the sibling reads (find_mappings_by_phone LIMIT 20 / mirror LIMIT 10).
        now = datetime.now(UTC)
        min_cutoff = now - timedelta(minutes=older_than_minutes)  # updated_at must be <= this
        max_cutoff = now - timedelta(minutes=max_age_minutes)  # updated_at must be > this
        rows: list[tuple[datetime, MappingView]] = []
        for gid, m in self.mappings.items():
            if self._mapping_status.get(gid, "pending") != "template_sent":
                continue
            updated = self._mapping_updated_at.get(gid)
            if updated is None or updated > min_cutoff or updated <= max_cutoff:
                continue
            rows.append(
                (
                    updated,
                    MappingView(
                        order_gid=m.order_gid,
                        order_name=m.order_name,
                        phone_e164=m.phone_e164,
                        status="template_sent",
                        is_cod=m.is_cod,
                        created_at=self._mapping_created_at[gid].isoformat(),
                    ),
                )
            )
        rows.sort(key=lambda r: r[0])
        return [view for _, view in rows[:_STALE_SWEEP_LIMIT]]

    async def find_outbound_by_dedupe_key(self, dedupe_key: str) -> OutboundDraft | None:
        return self.outbound.get(dedupe_key)

    async def find_outbound_by_dedupe_keys(
        self, dedupe_keys: list[str]
    ) -> dict[str, OutboundDraft]:
        # Batched sibling of find_outbound_by_dedupe_key: one lookup for the whole sweep, so the
        # reminder job never round-trips once per stale row (mirrors the mirror-order line-item
        # WHERE ... = ANY($1) batching).
        return {k: self.outbound[k] for k in dedupe_keys if k in self.outbound}

    async def enqueue_outbound(self, outbound: OutboundDraft) -> int | None:
        # `_enqueue` already returns the new row's id (or None on a dedupe_key conflict) --
        # `ingest_order_created` has relied on this since the ADR-001 inline-send amendment; this
        # method just needed to stop discarding that value.
        return self._enqueue(outbound)

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult:
        removed_mappings = [
            gid for gid, m in self.mappings.items() if m.phone_e164 == phone_e164
        ]
        for gid in removed_mappings:
            del self.mappings[gid]
            self._mapping_created_at.pop(gid, None)
            self._mapping_updated_at.pop(gid, None)
        removed_outbound = [
            key for key, o in self.outbound.items() if o.phone_e164 == phone_e164
        ]
        for key in removed_outbound:
            meta = self._outbound_meta.pop(key, None)
            if meta is not None:
                self._outbound_by_id.pop(meta.id, None)
            del self.outbound[key]
        # Mirror tables: an order carries the number in any of three columns and its items
        # follow it (the Postgres FK cascades). A customer is matched on its own phone OR by
        # being linked from one of those orders — a Shopify customer resource usually has no
        # phone of its own (it lives on the shipping address), so the link is the only way to
        # reach the name/email/address of the ordinary COD customer.
        removed_orders = [
            gid
            for gid, o in self.orders.items()
            if phone_e164 in (o.phone, o.shipping_phone, o.billing_phone)
        ]
        linked_customer_gids = {
            o.customer.gid for gid in removed_orders if (o := self.orders[gid]).customer
        }
        for gid in removed_orders:
            del self.orders[gid]
            self.order_items.pop(gid, None)
            # Courier/AWB tracking for an erased customer must not survive (Postgres cascades
            # via the FK; the in-memory store has to pop it explicitly).
            self.fulfillments.pop(gid, None)
        removed_customers = [
            gid
            for gid, cust in self.customers.items()
            if cust.phone == phone_e164 or gid in linked_customer_gids
        ]
        for gid in removed_customers:
            del self.customers[gid]
        # No in-memory conversation/message/action store yet (Phase 4) -> those counts stay 0.
        return DeletionResult(
            order_mappings=len(removed_mappings),
            outbound_messages=len(removed_outbound),
            conversations=0,
            messages=0,
            pending_actions=0,
            order_actions=0,
            customers=len(removed_customers),
            orders=len(removed_orders),
        )

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult:
        # In-memory rows carry no created_at timestamp, so there is nothing to age out;
        # the real age-based purge is the Postgres implementation.
        # order_mappings/outbound_messages are excluded from the age-based purge by design:
        # the client decided (round 3, 2026-08-06, client-decisions-all.md Q15) that
        # customer/order data is kept INDEFINITELY. Even once this store gains timestamps, do
        # NOT age those out here — only delete_by_phone (erasure-on-request) may remove them.
        return DeletionResult(
            order_mappings=0,
            outbound_messages=0,
            conversations=0,
            messages=0,
            pending_actions=0,
            order_actions=0,
        )

    async def count_orders_by_phone(self, phone_e164: str) -> int:
        return len([m for m in self.mappings.values() if m.phone_e164 == phone_e164])

    # --- Phase 5: outbox drain + mutation audit + mapping status ---

    def _meta_by_id(self, id: int) -> _OutboundRow | None:
        key = self._outbound_by_id.get(id)
        return self._outbound_meta.get(key) if key is not None else None

    async def claim_queued_outbound(self, limit: int = 20) -> list[OutboundClaim]:
        # dict preserves insertion order -> oldest first, mirroring ORDER BY created_at.
        # Atomically flip each claimed row 'queued' -> 'processing' as it is returned, matching
        # the Postgres CTE claim's state transition so tests against either store agree (there is
        # no real concurrency to guard here, but the STATE must move so a claimed row is never
        # re-claimed by a later call and bump can move it back to 'queued' for a retry).
        # Also reclaim a 'processing' row abandoned by a dead invocation once it is older than the
        # staleness threshold — mirroring the Postgres predicate so this store does not regress
        # behind it (a stranded 'processing' row would otherwise be lost forever).
        now = datetime.now(UTC)
        claims: list[OutboundClaim] = []
        for key, draft in self.outbound.items():
            meta = self._outbound_meta[key]
            is_stale_processing = (
                meta.state == "processing" and now - meta.updated_at >= _PROCESSING_STALE
            )
            if meta.state != "queued" and not is_stale_processing:
                continue
            meta.state = "processing"
            meta.updated_at = now
            claims.append(
                OutboundClaim(
                    id=meta.id,
                    dedupe_key=key,
                    phone_e164=draft.phone_e164,
                    payload_json=draft.payload_json,
                    attempts=meta.attempts,
                )
            )
            if len(claims) >= limit:
                break
        return claims

    async def claim_outbound_by_id(self, id: int) -> OutboundClaim | None:
        # Atomically claim ONE specific row by id — the inline order-confirmation send path
        # (jobs.outbox_drain.send_inline_outbound) targets only the row it just created. Flip
        # 'queued' -> 'processing' exactly like claim_queued_outbound so a concurrent/backstop
        # run_outbox_drain can never also claim and double-send it. Returns None if the row is
        # missing or not 'queued' (already claimed/sent/suppressed) -> the caller sends nothing.
        # Unlike claim_queued_outbound this does NOT reclaim a stale 'processing' row: the inline
        # path only ever targets a just-queued row, and stale reclaim stays the drain's job.
        key = self._outbound_by_id.get(id)
        if key is None:
            return None
        meta = self._outbound_meta.get(key)
        if meta is None or meta.state != "queued":
            return None
        meta.state = "processing"
        meta.updated_at = datetime.now(UTC)
        draft = self.outbound[key]
        return OutboundClaim(
            id=meta.id,
            dedupe_key=key,
            phone_e164=draft.phone_e164,
            payload_json=draft.payload_json,
            attempts=meta.attempts,
        )

    async def mark_outbound_sent(self, id: int, wamid: str | None) -> None:
        meta = self._meta_by_id(id)
        if meta is not None:
            meta.state = "sent"
            meta.template_wamid = wamid

    async def apply_outbound_delivery_status(
        self, wamid: str, status: str, error_code: str | None = None
    ) -> str:
        # Route a Meta delivery/read status by template_wamid. "not_found" means the wamid is not
        # in this table (try messages); "applied" means the ordering guard let the write through
        # (a genuine forward move or a fresh 'failed'); "unchanged" means the wamid IS here but the
        # guard rejected the write (a duplicate/regressive report). See base.py for the contract.
        # error_code is persisted (COALESCE semantics: only overwrite when non-None) alongside the
        # applied write, mirroring the Postgres UPDATE that only runs its SET when the guard passes.
        for meta in self._outbound_meta.values():
            if meta.template_wamid == wamid:
                if should_apply_delivery_status(meta.delivery_status, status):
                    meta.delivery_status = status
                    if error_code is not None:
                        meta.last_error_code = error_code
                    return "applied"
                return "unchanged"
        return "not_found"

    async def get_outbound_retry_info(self, wamid: str) -> OutboundRetryInfo | None:
        # Same lookup-by-template_wamid scan as apply_outbound_delivery_status. The draft carries
        # phone/payload; the mutable row carries id + retry_count.
        for key, meta in self._outbound_meta.items():
            if meta.template_wamid == wamid:
                draft = self.outbound[key]
                return OutboundRetryInfo(
                    id=meta.id,
                    phone_e164=draft.phone_e164,
                    payload_json=draft.payload_json,
                    retry_count=meta.retry_count,
                    last_error_code=meta.last_error_code,
                )
        return None

    async def record_outbound_retry(self, id: int, new_wamid: str | None) -> None:
        # Mirror the Postgres UPDATE: always increment retry_count; a non-None new_wamid means the
        # resend succeeded -> reassign the wamid and reset delivery_status to None (a fresh send
        # awaiting its own confirmation). A None new_wamid leaves wamid/delivery_status as-is.
        meta = self._meta_by_id(id)
        if meta is None:
            return
        meta.retry_count += 1
        if new_wamid is not None:
            meta.template_wamid = new_wamid
            meta.delivery_status = None

    async def mark_outbound_suppressed(self, id: int) -> None:
        meta = self._meta_by_id(id)
        if meta is not None:
            meta.state = "suppressed"

    async def mark_outbound_undeliverable(self, id: int, code: str) -> None:
        meta = self._meta_by_id(id)
        if meta is not None:
            meta.state = "undeliverable"
            meta.last_error_code = code

    async def bump_outbound_attempt(self, id: int, code: str, max_attempts: int = 5) -> str:
        meta = self._meta_by_id(id)
        if meta is None:
            return "failed"  # unknown row -> terminal (never happens in practice)
        meta.attempts += 1
        meta.last_error_code = code
        if meta.attempts >= max_attempts:
            meta.state = "failed"
            return "failed"
        # A retryable failure moves the row 'processing' -> 'queued' so a later cron run can
        # re-claim it; it must NOT stay 'processing' (nothing re-claims a processing row). This
        # mirrors the Postgres bump's explicit ELSE 'queued'.
        meta.state = "queued"
        return "queued"

    async def set_mapping_status(self, order_gid: str, status: str) -> None:
        self._mapping_status[order_gid] = status
        # Mirror Postgres' `updated_at = now()`: the transition to 'template_sent' (drain) is what
        # the reminder sweep ages off, so it must move the clock here too.
        self._mapping_updated_at[order_gid] = datetime.now(UTC)

    async def record_order_action(
        self,
        order_gid: str,
        action: str,
        actor_wa_id: str | None,
        source_wamid: str | None,
        result: str,
        user_errors_json: str | None,
    ) -> None:
        self.order_actions.append(
            {
                "order_gid": order_gid,
                "action": action,
                "actor_wa_id": actor_wa_id,
                "source_wamid": source_wamid,
                "result": result,
                "user_errors_json": user_errors_json,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    async def get_mapping_phone(self, order_gid: str) -> str | None:
        mapping = self.mappings.get(order_gid)
        return mapping.phone_e164 if mapping is not None else None

    async def orders_awaiting_cancel_reconcile(self, limit: int = 50) -> list[str]:
        # Atomic-claim parity with the Postgres impl: flip each returned order out of
        # 'cancel_requested' to the transient 'cancel_reconciling' state as it is claimed, so a
        # second call (a would-be overlapping reconcile run) never re-hands the same order and
        # cod_cancel is not double-sent. There is no real concurrency here, but the STATE must move
        # so the semantics do not regress behind Postgres. Also reclaim a 'cancel_reconciling' row
        # abandoned by a dead invocation once it is older than the 10-minute staleness threshold
        # (mirrors claim_queued_outbound), so a stranded transient row is not lost forever.
        now = datetime.now(UTC)
        claimed: list[str] = []
        for gid, status in self._mapping_status.items():
            is_stale = (
                status == "cancel_reconciling"
                and now - self._mapping_updated_at.get(gid, now) >= _PROCESSING_STALE
            )
            if status != "cancel_requested" and not is_stale:
                continue
            self._mapping_status[gid] = "cancel_reconciling"
            self._mapping_updated_at[gid] = now
            claimed.append(gid)
            if len(claimed) >= limit:
                break
        return claimed


class InMemoryMessageStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def record_if_new(self, message_id: str) -> bool:
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._conversations: dict[str, int] = {}
        # Internal mutable rows (not the frozen StoredMessage): per-conversation ordered list, plus
        # id- and wamid-indexed lookups for O(1) set_message_wamid / delivery-status routing. Same
        # split as the outbound side (_outbound_meta + _outbound_by_id). Converted to the frozen
        # StoredMessage view only at the recent_messages / find_messages_by_user_id read boundary.
        self._messages: dict[int, list[_MessageRow]] = {}
        self._messages_by_id: dict[int, _MessageRow] = {}
        self._message_by_wamid: dict[str, _MessageRow] = {}
        self._message_next_id = 1
        self._paused_until: dict[int, datetime] = {}
        self._handoff_attempted_at: dict[int, datetime] = {}
        # Mirrors conversations.last_active_at. Stamped once at row creation (the Postgres column
        # DEFAULT now()); thereafter only touch() bumps it -- get_or_create on an already-existing
        # row must NOT bump (a display-only lookup like the admin thread list would otherwise
        # corrupt recency). Needed so recent_conversations can order threads by recency, same as
        # the Postgres index does.
        self._last_active_at: dict[int, datetime] = {}
        # Mirrors conversations.last_read_at (DEFAULT now() in Postgres). Stamped at row creation
        # so a brand-new thread starts with zero unread, exactly like the Postgres column default.
        self._last_read_at: dict[int, datetime] = {}
        self._next_id = 1

    async def get_or_create(self, user_id: str) -> int:
        # Not racy: this check-then-set has no `await` between the membership test and the
        # dict writes, so under asyncio's single-threaded cooperative scheduling no other
        # coroutine can run in between — there is no yield point for a concurrent
        # get_or_create(same user_id) call to interleave on. Safe without a lock.
        if user_id not in self._conversations:
            self._conversations[user_id] = self._next_id
            self._messages[self._next_id] = []
            # First-creation stamp only (mirrors the Postgres DEFAULT now()); an existing row is
            # left untouched so a display-only lookup never bumps recency.
            self._last_active_at[self._next_id] = datetime.now(UTC)
            self._last_read_at[self._next_id] = datetime.now(UTC)
            self._next_id += 1
        return self._conversations[user_id]

    async def touch(self, user_id: str) -> None:
        # Explicit recency bump for genuine customer activity. The only place last_active_at is
        # set to now() after row creation. No-op if the conversation does not exist yet.
        conversation_id = self._conversations.get(user_id)
        if conversation_id is not None:
            self._last_active_at[conversation_id] = datetime.now(UTC)

    async def get_user_id(self, conversation_id: int) -> str | None:
        for user_id, conv_id in self._conversations.items():
            if conv_id == conversation_id:
                return user_id
        return None

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        return [_message_view(r) for r in self._messages.get(conversation_id, [])[-limit:]]

    async def append_message(
        self, conversation_id: int, role: str, content: str, sender: str | None = None
    ) -> int:
        # Stamp created_at (Postgres stamps a real timestamp via the column default) so the two
        # implementations stay behaviorally equivalent for the chat-thread timeline ordering.
        row = _MessageRow(
            id=self._message_next_id,
            role=role,
            content=content,
            created_at=datetime.now(UTC).isoformat(),
            wamid=None,
            delivery_status=None,
            sender=sender,
        )
        self._message_next_id += 1
        self._messages.setdefault(conversation_id, []).append(row)
        self._messages_by_id[row.id] = row
        return row.id

    async def set_message_wamid(self, message_id: int, wamid: str) -> None:
        # Attach WhatsApp's message id to an already-persisted row (no-op if the id is unknown,
        # defensive only). Index it by wamid so the status-webhook routing is an O(1) lookup.
        row = self._messages_by_id.get(message_id)
        if row is not None:
            row.wamid = wamid
            self._message_by_wamid[wamid] = row

    async def set_message_delivery_status(self, message_id: int, status: str) -> None:
        # Set delivery_status directly by id (no wamid), mirroring set_message_wamid's by-id
        # lookup. Used for a send that never produced a wamid so the row can still be marked
        # "failed" (no-op if the id is unknown, defensive only).
        row = self._messages_by_id.get(message_id)
        if row is not None:
            row.delivery_status = status

    async def apply_message_delivery_status(
        self, wamid: str, status: str, error_code: str | None = None
    ) -> str:
        # Route a Meta delivery/read status by wamid. "not_found" means the wamid is not in this
        # table (try outbound_messages); "applied" means the ordering guard let the write through
        # (a genuine forward move or a fresh 'failed'); "unchanged" means the wamid IS here but the
        # guard rejected the write (a duplicate/regressive report). See base.py for the contract.
        # error_code is persisted (COALESCE: only overwrite when non-None) on the applied write.
        row = self._message_by_wamid.get(wamid)
        if row is None:
            return "not_found"
        if should_apply_delivery_status(row.delivery_status, status):
            row.delivery_status = status
            if error_code is not None:
                row.last_error_code = error_code
            return "applied"
        return "unchanged"

    async def get_message_retry_info(self, wamid: str) -> MessageRetryInfo | None:
        # Same lookup-by-wamid (O(1) via _message_by_wamid) as apply_message_delivery_status. The
        # mutable row carries id/content/retry_count; conversation_id is not stored on the row, so
        # resolve it from the per-conversation index. The messages-table sibling of
        # get_outbound_retry_info. Defense-in-depth: pin role='assistant' (mirrors the Postgres
        # impl) so a future change stamping an inbound wamid onto a user row can never be "resent".
        row = self._message_by_wamid.get(wamid)
        if row is None or row.role != "assistant":
            return None
        conversation_id = self._conversation_id_of(row.id)
        if conversation_id is None:
            return None
        return MessageRetryInfo(
            id=row.id,
            conversation_id=conversation_id,
            content=row.content,
            retry_count=row.retry_count,
        )

    async def record_message_retry(self, id: int, new_wamid: str | None) -> None:
        # Mirror the Postgres UPDATE: always increment retry_count; a non-None new_wamid means the
        # resend succeeded -> reassign the wamid (re-index it, dropping the old key) and reset
        # delivery_status to None (a fresh send awaiting its own confirmation). A None new_wamid
        # leaves wamid/delivery_status as-is. Sibling of record_outbound_retry.
        row = self._messages_by_id.get(id)
        if row is None:
            return
        row.retry_count += 1
        if new_wamid is not None:
            if row.wamid is not None:
                self._message_by_wamid.pop(row.wamid, None)
            row.wamid = new_wamid
            self._message_by_wamid[new_wamid] = row
            row.delivery_status = None

    def _conversation_id_of(self, message_id: int) -> int | None:
        for conversation_id, rows in self._messages.items():
            if any(r.id == message_id for r in rows):
                return conversation_id
        return None

    async def find_messages_by_user_id(
        self, user_id: str, limit: int = 100
    ) -> list[StoredMessage]:
        conversation_id = self._conversations.get(user_id)
        if conversation_id is None:
            return []
        return [_message_view(r) for r in self._messages.get(conversation_id, [])[-limit:]]

    async def recent_conversations(self, limit: int = 50) -> list[ConversationSummary]:
        # A conversation row with zero messages must never surface here -- a brand-new row created
        # by get_or_create's display-only lookup (e.g. a customer who only ever received an
        # outbound template, never sent one) picks up a creation-time stamp, not real activity.
        with_messages = [
            (user_id, conv_id)
            for user_id, conv_id in self._conversations.items()
            if self._messages.get(conv_id)
        ]
        ordered = sorted(
            with_messages,
            key=lambda item: self._last_active_at.get(item[1], datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )
        summaries: list[ConversationSummary] = []
        for user_id, conv_id in ordered[:limit]:
            active = self._last_active_at.get(conv_id)
            summaries.append(
                ConversationSummary(
                    user_id=user_id,
                    last_active_at=active.isoformat() if active is not None else None,
                )
            )
        return summaries

    async def pause_until(self, conversation_id: int, until: datetime) -> None:
        self._paused_until[conversation_id] = until

    async def get_paused_until(self, conversation_id: int) -> datetime | None:
        return self._paused_until.get(conversation_id)

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None:
        self._handoff_attempted_at[conversation_id] = at

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None:
        return self._handoff_attempted_at.get(conversation_id)

    async def mark_read(self, conversation_id: int, at: datetime) -> None:
        self._last_read_at[conversation_id] = at

    async def count_unread_messages(self, conversation_id: int) -> int:
        last_read = self._last_read_at.get(conversation_id, datetime.min.replace(tzinfo=UTC))
        count = 0
        for row in self._messages.get(conversation_id, []):
            if row.role != "user":
                continue
            created = _parse_iso(row.created_at)
            if created is not None and created > last_read:
                count += 1
        return count


class InMemoryExchangeStore:
    def __init__(self) -> None:
        self._rows: dict[int, ExchangeRequest] = {}
        self._next_id = 1

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest:
        now = datetime.now(UTC).isoformat()
        row = ExchangeRequest(
            id=self._next_id, order_gid=order_gid, order_name=order_name,
            phone_e164=phone_e164, requested_size=requested_size, status="requested",
            requested_at=now, return_tracking_url=None, replacement_tracking_url=None,
            updated_at=now,
        )
        self._rows[row.id] = row
        self._next_id += 1
        return row

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
        return [r for r in self._rows.values() if r.phone_e164 == phone_e164]

    async def get(self, id: int) -> ExchangeRequest | None:
        return self._rows.get(id)

    async def set_status(self, id: int, status: ExchangeStatus) -> None:
        row = self._rows.get(id)
        if row is None:
            return
        self._rows[id] = replace(row, status=status, updated_at=datetime.now(UTC).isoformat())

    async def set_return_tracking_url(self, id: int, url: str) -> None:
        row = self._rows.get(id)
        if row is None:
            return
        self._rows[id] = replace(
            row, return_tracking_url=url, updated_at=datetime.now(UTC).isoformat()
        )

    async def set_replacement_tracking_url(self, id: int, url: str) -> None:
        row = self._rows.get(id)
        if row is None:
            return
        self._rows[id] = replace(
            row, replacement_tracking_url=url, updated_at=datetime.now(UTC).isoformat()
        )
