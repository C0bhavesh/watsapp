from dataclasses import dataclass
from typing import Protocol


class ConfigRepo(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


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


class IngestStore(Protocol):
    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult: ...
