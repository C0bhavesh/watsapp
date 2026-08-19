# Admin Chat Unread Marker + Filter Chips — Design

> Owner-directed, same-day follow-up to sub-project 1g (Emoji Picker + Template Resend). Approved 2026-08-19.

## Problem

The admin chat page (`chats.html`/`chats.js`) lists every conversation thread with no indication of which have new customer activity since the owner last looked, and no way to narrow the list to "needs attention" subsets. The owner referenced WhatsApp Web's own unread badge + filter chip row (`All` / `Unread` / `Favourites` / `Groups`) as the reference UX, wanting an equivalent `All` / `Unread` / `Handed to human` row, extensible to more filters later.

## Scope

1. A per-thread unread indicator (green count badge) driven by customer messages arriving since the owner last opened that thread.
2. A row of filter chips above the thread list: `All`, `Unread`, `Handed to human`. Single-select, combined (AND) with the existing search box.
3. Designed so a future filter (e.g. "Failed deliveries") is a small addition, not a redesign.

Out of scope: multi-admin/per-user read state (this store has one shared admin login); notification sounds/desktop alerts; unread counts for anything other than customer messages (button taps, delivery-status changes, AI/manual/template sends never affect unread — matches real WhatsApp).

## Data model

New column: `conversations.last_read_at timestamptz NOT NULL DEFAULT now()`, added via idempotent `ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_read_at timestamptz NOT NULL DEFAULT now()` in `backend/app/store/schema.sql`, same pattern as every prior additive migration in this repo (e.g. `retry_count`, `delivered_at`).

`DEFAULT now()` is deliberate: it means every conversation that already exists at migration time starts "read as of now" — nobody sees a flood of years-old messages marked unread the moment this ships. Only customer messages arriving strictly after a thread's `last_read_at` (which starts at migration time, then advances every time the owner opens that thread) ever count as unread.

## Backend changes

**`ConversationStore` (Protocol + Postgres + in-memory, `app/store/{base,postgres,memory}.py`):**
- `mark_read(thread_id: int, at: datetime) -> None` — sets `last_read_at = at`.
- `count_unread_messages(user_id: str, since: datetime) -> int` — count of rows in `messages` where `role = "user"` (customer) and `created_at > since`.
- `get_last_read_at(thread_id: int) -> datetime | None` — read-side helper `list_conversations` uses to compute `since` per thread (id → phone → `count_unread_messages`).

**`GET /admin/conversations/{thread_id}` (`get_conversation_thread`, `app/admin/router.py`):** after loading entries (existing behavior unchanged), call `c.conversations.mark_read(thread_id, datetime.now(UTC))`. No new endpoint — this route is already called both when the owner opens a thread and by the existing 3-second poll while that thread stays open, so "mark read on open" and "stays read while viewing" both fall out of the existing call pattern for free.

**`GET /admin/conversations` (`list_conversations`, same file):** for each thread already being materialized in the existing per-phone loop, add two more per-thread reads alongside the existing `find_messages_by_user_id`/`find_mirrored_orders_by_phone` calls:
- `unread_count` = `count_unread_messages(norm, since=last_read_at or thread_creation_stamp)`.
- `ai_paused` = `get_paused_until(thread_id)` is non-null and in the future (reuses the existing handoff pause mechanism in `core/conversation.py` — `pause_until`/`mark_handoff_attempted`; no new handoff concept introduced).

This is 2 more queries per thread on top of the existing N+1 shape already accepted and bounded (`limit` capped at 100) for `preview`/`orders` in this same function — same trade-off, same bound.

Response shape gains two additive fields per thread: `{"unread_count": int, "ai_paused": bool}`. No breaking change to existing consumers.

## Frontend changes (`chats.html`/`chats.js`)

- A filter-chip row above the thread list: `All`, `Unread`, `Handed to human`. Implemented as a small array `FILTERS = [{id, label, predicate(thread)}]` so a future chip is a one-entry addition — no backend redesign forced, only a new field on the list response if the new filter needs server-computed data.
- Single-select (click replaces the active chip), combined via logical AND with the existing `threadMatchesQuery` search-box filter — both applied to `allThreads` before `renderThreadRows`.
- `Unread` chip predicate: `thread.unread_count > 0`.
- `Handed to human` chip predicate: `thread.ai_paused`.
- Thread row gains a green count badge (WhatsApp-style circle, `thread.unread_count`) rendered next to the timestamp, shown only when `unread_count > 0`.
- No client-side "mark as read" call needed — opening a thread already triggers `GET /admin/conversations/{thread_id}` via `loadThread()`, which now marks it read server-side. The badge disappears on the next poll/list refresh once `unread_count` recomputes to 0.

## Testing

- Store-layer tests (Postgres + in-memory) for `mark_read`, `count_unread_messages` (zero/non-zero/boundary on `created_at > since`, never counts non-`user`-role rows), `get_last_read_at`.
- Router tests: opening a thread advances `last_read_at`; `unread_count` reflects only customer messages after that stamp; `ai_paused` true only while `paused_until` is in the future, false once expired/resumed.
- No frontend test runner in this repo (existing documented gap) — filter/badge rendering verified by manual owner browser pass, same as every prior chat-page frontend change.

## Database changes summary (for the owner)

One additive, idempotent migration: `conversations.last_read_at timestamptz NOT NULL DEFAULT now()` in `backend/app/store/schema.sql`. Same category as the `retry_count`/`delivered_at` columns already shipped this week — safe to run any time before deploy, no data loss, no lock beyond a normal `ADD COLUMN ... DEFAULT`. Owner runs it manually in Supabase before this feature is pushed, per standing rule (Rule 5, CLAUDE.md).
