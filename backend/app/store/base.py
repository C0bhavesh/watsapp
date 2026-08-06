from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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
    """

    order_mappings: int
    outbound_messages: int
    conversations: int
    messages: int
    pending_actions: int
    order_actions: int


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

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult: ...

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult: ...


class MessageStore(Protocol):
    """Dedupe authority for inbound Meta messages (sibling of processed_webhooks)."""

    async def record_if_new(self, message_id: str) -> bool: ...
