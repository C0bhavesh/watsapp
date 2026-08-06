from datetime import datetime

from app.store.base import (
    DeletionResult,
    IngestResult,
    MappingUpsert,
    MappingView,
    OutboundDraft,
    OutboundView,
    StoredMessage,
)


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
        queued = False
        if outbound is not None and outbound.dedupe_key not in self.outbound:
            self.outbound[outbound.dedupe_key] = outbound
            queued = True
        return IngestResult(duplicate=False, queued=queued)

    async def recent_mappings(self, limit: int) -> list[MappingView]:
        views = [
            MappingView(
                order_gid=m.order_gid,
                order_name=m.order_name,
                phone_e164=m.phone_e164,
                status="pending",
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
                state="queued",
                kind=o.kind,
                phone_e164=o.phone_e164,
                attempts=0,
                last_error_code=None,
                created_at=None,
            )
            for o in self.outbound.values()
        ]
        return list(reversed(views))[:limit]

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        matches = [m for m in self.mappings.values() if m.phone_e164 == phone_e164]
        views = [
            MappingView(
                order_gid=m.order_gid,
                order_name=m.order_name,
                phone_e164=m.phone_e164,
                status="pending",
                is_cod=m.is_cod,
                created_at=None,
            )
            for m in matches
        ]
        return list(reversed(views))[:limit]

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult:
        removed_mappings = [
            gid for gid, m in self.mappings.items() if m.phone_e164 == phone_e164
        ]
        for gid in removed_mappings:
            del self.mappings[gid]
        removed_outbound = [
            key for key, o in self.outbound.items() if o.phone_e164 == phone_e164
        ]
        for key in removed_outbound:
            del self.outbound[key]
        # No in-memory conversation/message/action store yet (Phase 4) -> those counts stay 0.
        return DeletionResult(
            order_mappings=len(removed_mappings),
            outbound_messages=len(removed_outbound),
            conversations=0,
            messages=0,
            pending_actions=0,
            order_actions=0,
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
