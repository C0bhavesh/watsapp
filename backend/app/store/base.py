from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.shopify.models import Customer, Order


class ConfigRepo(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def get_knowledge_override(self, kind: str) -> str | None: ...

    async def set_knowledge_override(self, kind: str, content: str) -> None: ...

    async def get_knowledge_overrides(self, kinds: list[str]) -> dict[str, str | None]: ...

    async def bump_config_int(self, key: str) -> None: ...


@dataclass(frozen=True)
class MappingUpsert:
    order_gid: str
    order_name: str
    order_number_int: int | None
    phone_e164: str | None
    customer_name: str | None
    email: str | None
    language: str
    financial_status_at_create: str | None
    is_cod: bool


@dataclass(frozen=True)
class OutboundDraft:
    dedupe_key: str
    kind: str
    phone_e164: str
    payload_json: str


@dataclass(frozen=True)
class OutboundClaim:
    """A queued outbound row handed to the drain job for sending."""

    id: int
    dedupe_key: str
    phone_e164: str
    payload_json: str
    attempts: int


@dataclass(frozen=True)
class IngestResult:
    duplicate: bool
    queued: bool


@dataclass(frozen=True)
class MappingView:
    order_gid: str
    order_name: str
    phone_e164: str | None
    status: str
    is_cod: bool
    created_at: str | None  # ISO string; None for in-memory rows


@dataclass(frozen=True)
class OutboundView:
    dedupe_key: str
    state: str
    kind: str
    phone_e164: str
    attempts: int
    last_error_code: str | None
    created_at: str | None


@dataclass(frozen=True)
class DeletionResult:
    """Row counts removed by a DPDP erasure/retention operation, per phone-bearing table.

    Shared by both ``delete_by_phone`` (on-demand right-to-erasure) and ``purge_older_than``
    (automatic age-based retention). ``order_mappings`` and ``outbound_messages`` are only ever
    non-zero for ``delete_by_phone``: the age-based ``purge_older_than`` keeps customer/order
    data INDEFINITELY (client decision, round 3 2026-08-06, Q15) and always reports 0 for those
    two fields — one shared shape is kept rather than a narrower type because the difference is a
    pair of guaranteed-zero counts, not a structural change.

    ``processed_messages`` is intentionally absent: it is a dedupe table with no
    ``phone_e164`` column, aged out blindly by ``purge_older_than`` (received_at cutoff) and
    not attributable to a single phone, so its blanket age-purge count is not reported here.

    ``customers``/``orders`` are the order-mirror tables (name/email/phone/postal address), added
    to ``delete_by_phone``'s coverage with the mirror sync; like the two fields above they are
    always 0 for ``purge_older_than`` (order/customer data is kept indefinitely). ``order_items``
    has no count of its own — those rows follow their order via ``ON DELETE CASCADE``. Both are
    defaulted so the existing five-field construction sites stay valid.
    """

    order_mappings: int
    outbound_messages: int
    conversations: int
    messages: int
    pending_actions: int
    order_actions: int
    customers: int = 0
    orders: int = 0


class IngestStore(Protocol):
    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult: ...

    async def recent_mappings(self, limit: int) -> list[MappingView]: ...

    async def recent_outbound(self, limit: int) -> list[OutboundView]: ...

    async def find_mappings_by_phone(
        self, phone_e164: str, limit: int = 20
    ) -> list[MappingView]: ...

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult: ...

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult: ...

    async def count_orders_by_phone(self, phone_e164: str) -> int: ...

    # --- Phase 5: outbox drain + mutation audit + mapping status ---

    async def claim_queued_outbound(self, limit: int = 20) -> list[OutboundClaim]: ...

    async def mark_outbound_sent(self, id: int, wamid: str | None) -> None: ...

    async def mark_outbound_suppressed(self, id: int) -> None: ...

    async def mark_outbound_undeliverable(self, id: int, code: str) -> None: ...

    async def bump_outbound_attempt(self, id: int, code: str, max_attempts: int = 5) -> str: ...

    async def set_mapping_status(self, order_gid: str, status: str) -> None: ...

    async def record_order_action(
        self,
        order_gid: str,
        action: str,
        actor_wa_id: str | None,
        source_wamid: str | None,
        result: str,
        user_errors_json: str | None,
    ) -> None: ...

    async def orders_awaiting_cancel_reconcile(self, limit: int = 50) -> list[str]: ...

    # --- Order mirror sync (Shopify webhook -> Postgres, no live read-path change yet) ---

    async def upsert_customer(self, customer: Customer) -> None: ...

    async def upsert_order_mirror(self, order: Order) -> None: ...

    async def customer_exists(self, gid: str) -> bool: ...

    async def get_mirrored_order(self, gid: str) -> Order | None: ...

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]: ...


class MessageStore(Protocol):
    """Dedupe authority for inbound Meta messages (sibling of processed_webhooks)."""

    async def record_if_new(self, message_id: str) -> bool: ...


@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None


class ConversationStore(Protocol):
    """Windowed chat history + handoff state per WhatsApp sender."""

    async def get_or_create(self, user_id: str) -> int: ...

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]: ...

    async def append_message(self, conversation_id: int, role: str, content: str) -> None: ...

    async def pause_until(self, conversation_id: int, until: datetime) -> None: ...

    async def get_paused_until(self, conversation_id: int) -> datetime | None: ...

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None: ...

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None: ...
