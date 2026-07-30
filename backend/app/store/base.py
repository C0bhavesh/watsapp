from dataclasses import dataclass
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


class MessageStore(Protocol):
    """Dedupe authority for inbound Meta messages (sibling of processed_webhooks)."""

    async def record_if_new(self, message_id: str) -> bool: ...
