from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.exchange_models import ExchangeRequest, ExchangeStatus
from app.shopify.models import Customer, Fulfillment, Order


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
    # id of the outbound_messages row this ingest freshly queued, or None when nothing was
    # queued (no eligible draft, or the dedupe_key already existed -> ON CONFLICT DO NOTHING).
    # The inline order-confirmation send (jobs.outbox_drain.send_inline_outbound, ADR-001
    # 2026-08-15 amendment) claims EXACTLY this row by id so it never touches the generic
    # claim/drain path; a duplicate/no-op ingest carries None and triggers no inline send.
    outbound_id: int | None = None


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
    # Latest Meta delivery/read status for the send (None until a status webhook lands). Surfaced
    # so a permanently-undeliverable row (delivery_status='failed', or state='undeliverable' once
    # retries are exhausted) is distinguishable in the admin outbox view from a healthy 'sent' row.
    delivery_status: str | None = None


@dataclass(frozen=True)
class ConversationSummary:
    user_id: str
    last_active_at: str | None


@dataclass(frozen=True)
class OutboundEntry:
    dedupe_key: str
    kind: str
    state: str
    payload_json: str
    created_at: str | None
    delivery_status: str | None = None


@dataclass(frozen=True)
class OutboundRetryInfo:
    """Retry state of a sent template row, looked up by its current template_wamid. Consumed by
    the delivery-failure auto-retry resend logic: `id` re-targets the row for record_outbound_retry,
    `phone_e164`/`payload_json` are the original send it re-uses verbatim, `retry_count` is how many
    retry attempts have already been spent (the resend cap is checked against it)."""

    id: int
    phone_e164: str
    payload_json: str
    retry_count: int
    # The most recent Meta delivery-failure error code captured for this row (via
    # apply_outbound_delivery_status), or None if none was ever recorded. Used at the retry cap to
    # mark the row undeliverable with a meaningful code instead of a bare "retries_exhausted".
    last_error_code: str | None = None


@dataclass(frozen=True)
class OrderActionEntry:
    order_gid: str
    action: str
    result: str
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

    async def find_stale_template_sent(
        self, older_than_minutes: int, max_age_minutes: int
    ) -> list[MappingView]: ...

    async def find_outbound_by_dedupe_key(self, dedupe_key: str) -> OutboundDraft | None: ...

    async def find_outbound_by_dedupe_keys(
        self, dedupe_keys: list[str]
    ) -> dict[str, OutboundDraft]: ...

    async def enqueue_outbound(self, outbound: OutboundDraft) -> int | None: ...

    async def delete_by_phone(self, phone_e164: str) -> DeletionResult: ...

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult: ...

    async def count_orders_by_phone(self, phone_e164: str) -> int: ...

    # --- Phase 5: outbox drain + mutation audit + mapping status ---

    async def claim_queued_outbound(self, limit: int = 20) -> list[OutboundClaim]: ...

    async def claim_outbound_by_id(self, id: int) -> OutboundClaim | None: ...

    async def mark_outbound_sent(self, id: int, wamid: str | None) -> None: ...

    # Applies a Meta delivery/read status update, found by template_wamid, through the ordering
    # guard in app.core.delivery_status. Returns one of three strings:
    #   "not_found"  -- no row has this template_wamid (try the messages table instead);
    #   "applied"    -- this call genuinely changed delivery_status (a forward transition or a
    #                   fresh 'failed');
    #   "unchanged"  -- a row was found but the ordering guard rejected the write (a duplicate or
    #                   regressive report, or an already-terminal 'failed' getting another
    #                   'failed').
    # The applied/unchanged distinction lets retry wiring fire only on a genuinely new 'failed',
    # never on a Meta-redelivered duplicate webhook (which would double-fire a retry).
    # error_code (Meta's numeric delivery-failure code, from a 'failed' status' errors[] array) is
    # persisted to last_error_code via COALESCE only when the guarded write applies -- a None
    # error_code (or an unchanged/not_found result) never clears a previously-captured code.
    async def apply_outbound_delivery_status(
        self, wamid: str, status: str, error_code: str | None = None
    ) -> str: ...

    # Looks up a sent template row by its current template_wamid, returning its retry state
    # (id/phone/payload/retry_count) or None if no row carries this wamid. Read-only.
    async def get_outbound_retry_info(self, wamid: str) -> OutboundRetryInfo | None: ...

    # Records that a retry attempt was used. `new_wamid` non-None means the resend succeeded and
    # got a fresh wamid -- the row's wamid is updated to it and delivery_status reset to NULL (a
    # fresh send awaiting its own confirmation). `new_wamid` None means the resend attempt itself
    # could not be sent (or was suppressed by the kill switch) -- retry_count still increments,
    # but wamid/delivery_status are left as-is.
    async def record_outbound_retry(self, id: int, new_wamid: str | None) -> None: ...

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

    async def get_mapping_phone(self, order_gid: str) -> str | None: ...

    async def find_outbound_by_phone(
        self, phone_e164: str, limit: int = 100
    ) -> list[OutboundEntry]: ...

    # actor_wa_id is written RAW (no leading +, see core/order_actions.py). The unified chat
    # view resolves a thread from the NORMALIZED phone, so it must query with BOTH the normalized
    # and the raw/digits-only candidate keys -- hence a multi-key (ANY) lookup, not a single key.
    async def find_order_actions_by_wa_ids(
        self, wa_ids: list[str], limit: int = 100
    ) -> list[OrderActionEntry]: ...

    # Distinct enumeration for the unified thread LIST: the list unions phones/wa_ids across all
    # three sources (conversations, outbound_messages, order_actions), so a customer who only ever
    # received an order confirmation (no conversation row) still surfaces as a thread. Each entry is
    # (identifier, latest_iso) ordered by MAX(created_at) DESC -- so the LIMIT keeps the most
    # RECENTLY-active customers (not a lexicographic slice) and the router can sort the union on the
    # real last-active stamp instead of treating these two sources as always-oldest.
    async def distinct_outbound_phones(
        self, limit: int = 100
    ) -> list[tuple[str, str | None]]: ...

    async def distinct_order_action_wa_ids(
        self, limit: int = 100
    ) -> list[tuple[str, str | None]]: ...

    # Atomically CLAIMS the returned orders: each is flipped out of 'cancel_requested' to a
    # transient in-progress state so two overlapping reconcile runs cannot both claim the same
    # order (and double-send cod_cancel). The caller must finalize each claimed order -- advance
    # it on success, or release it back to 'cancel_requested' on any skip/failure.
    async def orders_awaiting_cancel_reconcile(self, limit: int = 50) -> list[str]: ...

    # --- Order mirror sync (Shopify webhook -> Postgres, no live read-path change yet) ---

    async def upsert_customer(self, customer: Customer) -> None: ...

    async def upsert_order_mirror(self, order: Order) -> None: ...

    async def upsert_fulfillment(self, order_gid: str, fulfillment: Fulfillment) -> None: ...

    async def customer_exists(self, gid: str) -> bool: ...

    async def get_mirrored_order(self, gid: str) -> Order | None: ...

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None: ...

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]: ...


class MessageStore(Protocol):
    """Dedupe authority for inbound Meta messages (sibling of processed_webhooks)."""

    async def record_if_new(self, message_id: str) -> bool: ...


@dataclass(frozen=True)
class MessageRetryInfo:
    """Retry state of an AI-reply message row, looked up by its current wamid. The messages-table
    sibling of OutboundRetryInfo (template sends). Consumed by the delivery-failure auto-retry
    resend logic: `id` re-targets the row for record_message_retry, `conversation_id`/`content` are
    the original reply it re-uses verbatim, `retry_count` is how many retry attempts have already
    been spent (the resend cap is checked against it)."""

    id: int
    conversation_id: int
    content: str
    retry_count: int


@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    created_at: str | None
    delivery_status: str | None = None
    sender: str | None = None


class ConversationStore(Protocol):
    """Windowed chat history + handoff state per WhatsApp sender."""

    async def get_or_create(self, user_id: str) -> int: ...

    # Explicit "genuine customer activity just happened" bump. This is the ONLY place
    # last_active_at is written to now() after the row is first created -- get_or_create no longer
    # bumps on an already-existing row, so a display-only lookup (e.g. the admin thread list
    # materializing a thread_id) can call get_or_create without corrupting recency. No-op if the
    # conversation does not exist yet.
    async def touch(self, user_id: str) -> None: ...

    # Read-only reverse lookup: thread id -> the conversation's user_id (the normalized phone).
    # Returns None for an unknown id (the unified thread view maps that to a 404). MUST NOT create.
    async def get_user_id(self, conversation_id: int) -> str | None: ...

    # Genuinely read-only: unlike get_or_create, MUST NOT create a conversation row on a miss.
    # A miss (no conversation for this user_id) returns an empty list.
    async def find_messages_by_user_id(
        self, user_id: str, limit: int = 100
    ) -> list[StoredMessage]: ...

    async def recent_conversations(self, limit: int = 50) -> list[ConversationSummary]: ...

    async def recent_messages(self, conversation_id: int, limit: int) -> list[StoredMessage]: ...

    async def append_message(
        self, conversation_id: int, role: str, content: str, sender: str | None = None
    ) -> int: ...

    # Attaches WhatsApp's message id to an already-persisted message row, once the actual send
    # (which happens AFTER persist_turn writes the row) succeeds and returns a wamid. No-op if the
    # message id doesn't exist (should not happen in practice, defensive only).
    async def set_message_wamid(self, message_id: int, wamid: str) -> None: ...

    # Sets delivery_status on a message row directly by its id (NOT by wamid, unlike
    # apply_message_delivery_status). Used when a send never produced a wamid at all -- e.g. a
    # manual admin reply whose WhatsApp send raised or returned not-ok -- so the row can be marked
    # "failed" and show the failed tick instead of a misleading grey "sent" tick. No ordering
    # guard: the caller sets a terminal state on a row it just created.
    async def set_message_delivery_status(self, message_id: int, status: str) -> None: ...

    # Applies a Meta delivery/read status update, found by wamid, through the ordering guard in
    # app.core.delivery_status. Returns one of three strings:
    #   "not_found"  -- no row has this wamid (try the outbound_messages table instead);
    #   "applied"    -- this call genuinely changed delivery_status (a forward transition or a
    #                   fresh 'failed');
    #   "unchanged"  -- a row was found but the ordering guard rejected the write (a duplicate or
    #                   regressive report, or an already-terminal 'failed' getting another
    #                   'failed').
    # The applied/unchanged distinction lets retry wiring fire only on a genuinely new 'failed',
    # never on a Meta-redelivered duplicate webhook (which would double-fire a retry).
    # error_code (see IngestStore.apply_outbound_delivery_status) is persisted to the messages
    # row's last_error_code via COALESCE only when the guarded write applies.
    async def apply_message_delivery_status(
        self, wamid: str, status: str, error_code: str | None = None
    ) -> str: ...

    # Looks up an AI-reply message row by its current wamid, returning its retry state
    # (id/conversation_id/content/retry_count) or None if no row carries this wamid. The
    # messages-table sibling of IngestStore.get_outbound_retry_info. Read-only.
    async def get_message_retry_info(self, wamid: str) -> MessageRetryInfo | None: ...

    # Records that a retry attempt was used. `new_wamid` non-None means the resend succeeded and
    # got a fresh wamid -- the row's wamid is updated to it and delivery_status reset to NULL (a
    # fresh send awaiting its own confirmation). `new_wamid` None means the resend attempt itself
    # could not be sent (or was suppressed by the kill switch) -- retry_count still increments,
    # but wamid/delivery_status are left as-is. Sibling of IngestStore.record_outbound_retry.
    async def record_message_retry(self, id: int, new_wamid: str | None) -> None: ...

    async def pause_until(self, conversation_id: int, until: datetime) -> None: ...

    async def get_paused_until(self, conversation_id: int) -> datetime | None: ...

    async def mark_handoff_attempted(self, conversation_id: int, at: datetime) -> None: ...

    async def get_handoff_attempted_at(self, conversation_id: int) -> datetime | None: ...

    # Admin "unread" tracking (2026-08-19). Stamps the moment the owner opened this thread in the
    # admin chat page -- called from GET /admin/conversations/{thread_id}, which fires both on
    # thread-open and on every 3s poll tick while that thread stays open, so "read on open" and
    # "stays read while viewing" both fall out of this one call site.
    async def mark_read(self, conversation_id: int, at: datetime) -> None: ...

    # Count of customer (role="user") messages strictly newer than this conversation's
    # last_read_at. Encapsulates the "since" comparison in the store so callers never read a
    # raw last_read_at themselves. A brand-new conversation (mark_read never called) has
    # last_read_at defaulted to its creation time (mirrors the Postgres column's DEFAULT now()),
    # so it starts at 0, not a flood of pre-existing history.
    async def count_unread_messages(self, conversation_id: int) -> int: ...


class ExchangeStore(Protocol):
    """Size-exchange requests: create, look up by customer phone, advance status/tracking.

    See app/core/exchange_eligibility.py for the eligibility check that gates a create() call
    (enforced in app/agents/exchange.py, not here -- this store trusts its caller, same as
    every other store in this codebase).
    """

    async def create(
        self, order_gid: str, order_name: str, phone_e164: str, requested_size: str,
    ) -> ExchangeRequest: ...

    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]: ...

    async def get(self, id: int) -> ExchangeRequest | None: ...

    async def set_status(self, id: int, status: ExchangeStatus) -> None: ...

    async def set_return_tracking_url(self, id: int, url: str) -> None: ...
