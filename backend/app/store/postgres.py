from app.store.base import (
    IngestResult,
    MappingUpsert,
    MappingView,
    OutboundDraft,
    OutboundView,
)
from app.store.pg_factory import LazyPool


def _rows_affected(tag: str) -> int:
    """Rows from an asyncpg command tag, e.g. 'INSERT 0 10' -> 10 (parse suffix, not endswith)."""
    last = tag.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


class PostgresConfigRepo:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def get(self, key: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM app_config WHERE key = $1", key)
        return None if row is None else str(row["value"])

    async def set(self, key: str, value: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES ($1, $2, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
                key,
                value,
            )

    async def get_knowledge_override(self, kind: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content FROM knowledge_overrides WHERE kind = $1", kind
            )
        return None if row is None else str(row["content"])

    async def set_knowledge_override(self, kind: str, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO knowledge_overrides (kind, content, updated_at)"
                " VALUES ($1, $2, now())"
                " ON CONFLICT (kind) DO UPDATE"
                " SET content = EXCLUDED.content, updated_at = now()",
                kind,
                content,
            )

    async def get_knowledge_overrides(self, kinds: list[str]) -> dict[str, str | None]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind, content FROM knowledge_overrides WHERE kind = ANY($1::text[])",
                kinds,
            )
        found = {str(r["kind"]): str(r["content"]) for r in rows}
        return {k: found.get(k) for k in kinds}

    async def bump_config_int(self, key: str) -> None:
        # value must remain a decimal integer string; only our code writes this key
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO app_config (key, value) VALUES ($1, '1')"
                " ON CONFLICT (key) DO UPDATE"
                " SET value = ((app_config.value)::bigint + 1)::text, updated_at = now()",
                key,
            )


class PostgresIngestStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def ingest_order_created(
        self,
        webhook_id: str,
        topic: str,
        mapping: MappingUpsert,
        outbound: OutboundDraft | None,
    ) -> IngestResult:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.execute(
                    "INSERT INTO processed_webhooks (webhook_id, topic) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    webhook_id,
                    topic,
                )
                if _rows_affected(inserted) == 0:
                    return IngestResult(duplicate=True, queued=False)
                await conn.execute(
                    "INSERT INTO order_mappings (order_gid, order_name, order_number_int, "
                    "phone_e164, customer_name, email, language, financial_status_at_create, "
                    "is_cod) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (order_gid) DO UPDATE SET phone_e164 = $4, "
                    "customer_name = $5, email = $6, language = $7, updated_at = now()",
                    mapping.order_gid,
                    mapping.order_name,
                    mapping.order_number_int,
                    mapping.phone_e164,
                    mapping.customer_name,
                    mapping.email,
                    mapping.language,
                    mapping.financial_status_at_create,
                    mapping.is_cod,
                )
                queued = False
                if outbound is not None:
                    result = await conn.execute(
                        "INSERT INTO outbound_messages (dedupe_key, kind, phone_e164, "
                        "payload_json) VALUES ($1, $2, $3, $4) ON CONFLICT (dedupe_key) "
                        "DO NOTHING",
                        outbound.dedupe_key,
                        outbound.kind,
                        outbound.phone_e164,
                        outbound.payload_json,
                    )
                    queued = _rows_affected(result) > 0
                return IngestResult(duplicate=False, queued=queued)

    async def recent_mappings(self, limit: int) -> list[MappingView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid, order_name, phone_e164, status, is_cod, created_at"
                " FROM order_mappings ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            MappingView(
                order_gid=str(r["order_gid"]),
                order_name=str(r["order_name"]),
                phone_e164=None if r["phone_e164"] is None else str(r["phone_e164"]),
                status=str(r["status"]),
                is_cod=bool(r["is_cod"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    async def recent_outbound(self, limit: int) -> list[OutboundView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT dedupe_key, state, kind, phone_e164, attempts, last_error_code,"
                " created_at FROM outbound_messages ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            OutboundView(
                dedupe_key=str(r["dedupe_key"]),
                state=str(r["state"]),
                kind=str(r["kind"]),
                phone_e164=str(r["phone_e164"]),
                attempts=int(r["attempts"]),
                last_error_code=(
                    None if r["last_error_code"] is None else str(r["last_error_code"])
                ),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]


class PostgresMessageStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def record_if_new(self, message_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO processed_messages (message_id) VALUES ($1) "
                "ON CONFLICT DO NOTHING",
                message_id,
            )
        return _rows_affected(result) > 0
