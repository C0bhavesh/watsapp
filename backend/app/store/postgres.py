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

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_gid, order_name, phone_e164, status, is_cod, created_at"
                " FROM order_mappings WHERE phone_e164 = $1 ORDER BY created_at DESC LIMIT $2",
                phone_e164,
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

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult:
        """DPDP right-to-erasure: purge every row keyed to one phone number, atomically.

        Children (messages) are deleted before parents (conversations) for FK integrity.
        conversations/messages have no repo writer yet (Phase 4) but are cleaned defensively
        so erasure is complete the moment that layer starts persisting. pending_actions is
        scoped by ``wa_id`` and order_actions by ``actor_wa_id`` (both the requester's number).

        Known residual: processed_messages retains dedupe rows whose message_id embeds the
        requester's phone in Meta's wamid encoding; these age out via purge_older_than's
        received_at cutoff but are not covered by an on-demand phone-scoped delete (decoding
        wamids to recover the sender number is deliberately out of scope).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                msgs = await conn.execute(
                    "DELETE FROM messages WHERE conversation_id IN "
                    "(SELECT id FROM conversations WHERE user_id = $1)",
                    phone_e164,
                )
                convs = await conn.execute(
                    "DELETE FROM conversations WHERE user_id = $1", phone_e164
                )
                outbound = await conn.execute(
                    "DELETE FROM outbound_messages WHERE phone_e164 = $1", phone_e164
                )
                mappings = await conn.execute(
                    "DELETE FROM order_mappings WHERE phone_e164 = $1", phone_e164
                )
                pending = await conn.execute(
                    "DELETE FROM pending_actions WHERE wa_id = $1", phone_e164
                )
                actions = await conn.execute(
                    "DELETE FROM order_actions WHERE actor_wa_id = $1", phone_e164
                )
        return DeletionResult(
            order_mappings=_rows_affected(mappings),
            outbound_messages=_rows_affected(outbound),
            conversations=_rows_affected(convs),
            messages=_rows_affected(msgs),
            pending_actions=_rows_affected(pending),
            order_actions=_rows_affected(actions),
        )

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult:
        """DPDP age-based retention: delete only the deletable tables older than ``cutoff``.

        ``order_mappings`` and ``outbound_messages`` are DELIBERATELY EXCLUDED and are never
        touched by this automatic job — the client decided (round 3, 2026-08-06,
        docs/FR/client-decisions-all.md Q15) that customer/order data (profile, name, phone,
        order history/number/date, products, SKU, payment method, status, spend, count, tags)
        is kept INDEFINITELY. Do NOT re-add DELETEs for those tables here: on-demand
        right-to-erasure (``delete_by_phone`` / POST /admin/erasure) is the only path allowed to
        remove order/outbound rows, and only for a specific number on request.

        Only the "AI conversation history / temporary AI context / processed-message logs /
        operational data" category (client-approved for deletion after 365 days) ages out:
        conversations/messages on ``last_active_at``, pending_actions/order_actions on
        ``created_at``, processed_messages on ``received_at``. Children first (FK). The
        ``order_mappings``/``outbound_messages`` counts in the returned DeletionResult are
        therefore always 0 (kept indefinitely); processed_messages' blanket count is not
        reported (no phone column, not attributable).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                msgs = await conn.execute(
                    "DELETE FROM messages WHERE conversation_id IN "
                    "(SELECT id FROM conversations WHERE last_active_at < $1)",
                    cutoff,
                )
                convs = await conn.execute(
                    "DELETE FROM conversations WHERE last_active_at < $1", cutoff
                )
                pending = await conn.execute(
                    "DELETE FROM pending_actions WHERE created_at < $1", cutoff
                )
                actions = await conn.execute(
                    "DELETE FROM order_actions WHERE created_at < $1", cutoff
                )
                # Blanket age-out of the dedupe table (no phone column, not attributable).
                await conn.execute(
                    "DELETE FROM processed_messages WHERE received_at < $1", cutoff
                )
        return DeletionResult(
            order_mappings=0,  # kept indefinitely — never age-purged (client Q15)
            outbound_messages=0,  # kept indefinitely — never age-purged (client Q15)
            conversations=_rows_affected(convs),
            messages=_rows_affected(msgs),
            pending_actions=_rows_affected(pending),
            order_actions=_rows_affected(actions),
        )


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


class PostgresConversationStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def get_or_create(self, user_id: str) -> int:
        # Single atomic upsert (relies on the ux_conversations_user_id unique index) instead
        # of SELECT-then-INSERT: two concurrent first-contact messages from the same sender
        # can otherwise both miss the SELECT and both INSERT, splitting history across two
        # conversation ids. ON CONFLICT makes this one round trip with no race window.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO conversations (user_id) VALUES ($1)"
                " ON CONFLICT (user_id) DO UPDATE SET last_active_at = now()"
                " RETURNING id",
                user_id,
            )
        return int(row["id"])

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = $1"
                " ORDER BY created_at DESC LIMIT $2",
                conversation_id,
                limit,
            )
        ordered = list(reversed(rows))
        return [
            StoredMessage(
                role=str(r["role"]),
                content=str(r["content"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in ordered
        ]

    async def append_message(self, conversation_id: int, role: str, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)",
                conversation_id,
                role,
                content,
            )

    async def pause_until(self, conversation_id: int, until: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET paused_until = $1 WHERE id = $2", until, conversation_id
            )

    async def get_paused_until(self, conversation_id: int) -> datetime | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT paused_until FROM conversations WHERE id = $1", conversation_id
            )
        return None if row is None else row["paused_until"]

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET handoff_attempted_at = $1 WHERE id = $2",
                at,
                conversation_id,
            )

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT handoff_attempted_at FROM conversations WHERE id = $1", conversation_id
            )
        return None if row is None else row["handoff_attempted_at"]
