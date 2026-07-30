from app.store.base import IngestResult, MappingUpsert, OutboundDraft


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


class InMemoryMessageStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def record_if_new(self, message_id: str) -> bool:
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True
