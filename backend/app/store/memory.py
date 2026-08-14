from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.shopify.models import Customer, LineItem, Order, normalize_order_name
from app.store.base import (
    DeletionResult,
    IngestResult,
    MappingUpsert,
    MappingView,
    OutboundClaim,
    OutboundDraft,
    OutboundView,
    StoredMessage,
)
from app.store.postgres import _e164

# A 'processing' row older than this is treated as abandoned by a dead invocation and re-claimed
# (mirrors the Postgres claim's `interval '10 minutes'`). There is no real concurrency in the
# in-memory store, but the staleness semantics must not regress behind Postgres's.
_PROCESSING_STALE = timedelta(minutes=10)

# Cap on the reminder sweep, mirroring the Postgres query's LIMIT. Bounds a single tick's work on
# the shared pool (matches the sibling capped reads: find_mappings_by_phone LIMIT 20, mirror LIMIT
# 10) — a leftover stale row is just picked up on the next tick.
_STALE_SWEEP_LIMIT = 25


@dataclass
class _OutboundRow:
    """Mutable per-row outbox state tracked alongside the frozen ``OutboundDraft``."""

    id: int
    state: str
    attempts: int
    last_error_code: str | None
    template_wamid: str | None
    # Stamp of the last state transition — the in-memory analogue of the Postgres `updated_at`
    # column. Used only to decide whether a 'processing' row is stale enough to reclaim.
    updated_at: datetime


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
        queued = self._enqueue(outbound) if outbound is not None else False
        return IngestResult(duplicate=False, queued=queued)

    def _enqueue(self, outbound: OutboundDraft) -> bool:
        """Queue one draft, idempotent on dedupe_key (mirrors Postgres ON CONFLICT DO NOTHING).

        Shared by ingest_order_created (original push) and enqueue_outbound (reminder re-queue) so
        the outbox bookkeeping (id, state, updated_at, id index) is created identically for both.
        """
        if outbound.dedupe_key in self.outbound:
            return False
        self.outbound[outbound.dedupe_key] = outbound
        row = _OutboundRow(
            id=self._outbound_next_id,
            state="queued",
            attempts=0,
            last_error_code=None,
            template_wamid=None,
            updated_at=datetime.now(UTC),
        )
        self._outbound_meta[outbound.dedupe_key] = row
        self._outbound_by_id[row.id] = outbound.dedupe_key
        self._outbound_next_id += 1
        return True

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

    async def customer_exists(self, gid: str) -> bool:
        return gid in self.customers

    async def get_mirrored_order(self, gid: str) -> Order | None:
        return self.orders.get(gid)

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        for order in self.orders.values():
            if order.name == name:
                return order
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
        return matches[:10]

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

    async def enqueue_outbound(self, outbound: OutboundDraft) -> bool:
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

    async def mark_outbound_sent(self, id: int, wamid: str | None) -> None:
        meta = self._meta_by_id(id)
        if meta is not None:
            meta.state = "sent"
            meta.template_wamid = wamid

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
            }
        )

    async def orders_awaiting_cancel_reconcile(self, limit: int = 50) -> list[str]:
        return [
            gid for gid, status in self._mapping_status.items() if status == "cancel_requested"
        ][:limit]


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
        self._messages: dict[int, list[StoredMessage]] = {}
        self._paused_until: dict[int, datetime] = {}
        self._handoff_attempted_at: dict[int, datetime] = {}
        self._next_id = 1

    async def get_or_create(self, user_id: str) -> int:
        # Not racy: this check-then-set has no `await` between the membership test and the
        # dict writes, so under asyncio's single-threaded cooperative scheduling no other
        # coroutine can run in between — there is no yield point for a concurrent
        # get_or_create(same user_id) call to interleave on. Safe without a lock.
        if user_id not in self._conversations:
            self._conversations[user_id] = self._next_id
            self._messages[self._next_id] = []
            self._next_id += 1
        return self._conversations[user_id]

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        return self._messages.get(conversation_id, [])[-limit:]

    async def append_message(self, conversation_id: int, role: str, content: str) -> None:
        self._messages.setdefault(conversation_id, []).append(
            StoredMessage(role=role, content=content, created_at=None)
        )

    async def pause_until(self, conversation_id: int, until: datetime) -> None:
        self._paused_until[conversation_id] = until

    async def get_paused_until(self, conversation_id: int) -> datetime | None:
        return self._paused_until.get(conversation_id)

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None:
        self._handoff_attempted_at[conversation_id] = at

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None:
        return self._handoff_attempted_at.get(conversation_id)
