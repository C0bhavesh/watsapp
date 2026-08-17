# Resume AI Button — Design

**Status:** Approved by owner (2026-08-17), via conversational brainstorming (urgent, prompted by a live incident where a customer's AI handoff pause left them stuck for 24h with no admin way to clear it).

## Problem

Earlier today a bug caused the AI to pause itself (a 24h "handoff to human" window) for a customer far too readily; that bug is now fixed for future conversations, but the customer it happened to (Ravi Pandey) is still paused under the old behavior and there is no way, from the admin panel or anywhere else, to manually clear a conversation's pause and put the AI back in charge of that chat. The owner asked to "set Ravi's chat to AI" and there was nothing to click.

## What already exists (verified by reading the code)

- `ConversationStore.pause_until(conversation_id: int, until: datetime) -> None` (`app/store/base.py:287`, implemented in both `memory.py`/`postgres.py`) already exists and is exactly the primitive needed — calling it with `until = now` immediately un-pauses the conversation, since `core/conversation.py`'s pause check is `if paused_until is not None and now < paused_until`. No new store method is needed.
- `ConversationStore.get_paused_until(conversation_id: int) -> datetime | None` (`app/store/base.py:289`) already exists — the read side is also done.
- The chats page's `thread_id` IS the conversation's DB primary key (`conversations.id`) — no separate lookup needed to resolve which conversation a thread's pause state belongs to; `thread_id` can be passed directly to both store methods above.
- `GET /admin/conversations/{thread_id}` (`router.py:705+`) already resolves and validates `thread_id` (404 on unknown) before doing any per-thread work — the new field and new endpoint reuse that exact pattern.
- The admin router already has a precedent for a simple admin-triggered mutation endpoint: `POST /admin/erasure` (`router.py:555`) — same `Depends(require_admin)` gating, same "resolve thread/customer, do one focused write, return a small confirmation" shape.

## Design

### 1. Backend: expose pause state, add a resume endpoint

`GET /admin/conversations/{thread_id}`'s response gains one new field: `"paused_until": str | None` (ISO-8601, or `null` if not paused / pause already expired) — read via `await c.conversations.get_paused_until(thread_id)`, using the already-validated `thread_id`.

New endpoint: `POST /admin/conversations/{thread_id}/resume`, admin-gated identically to the existing thread endpoint. Resolves `thread_id` the same way (404 if unknown — reuse `get_user_id(thread_id)` for the same validation, even though the phone value itself isn't needed here, so an invalid id gets the same 404 as every other thread route rather than silently succeeding on nothing). Calls `await c.conversations.pause_until(thread_id, datetime.now(UTC))` to clear the pause immediately, and returns `{"ok": true}`.

### 2. Frontend: conditional button, one click

The chat header (`#chat-header` in `chats.html`) gains a "Resume AI" button, hidden by default (`display: none`). When a thread loads, if the response's `paused_until` is non-null AND parses to a time still in the future compared to the browser's current time, the button is shown; otherwise it stays hidden. Clicking it calls `POST /admin/conversations/{thread_id}/resume`, then re-loads the thread (reusing the existing `loadThread()` call) so the button disappears once the now-cleared `paused_until` comes back `null`.

## Out of scope (YAGNI)

- Any change to how/when a pause gets SET (that's the separate handoff-trigger-narrowing fix already shipped today) — this only adds a way to clear one.
- Any bulk "resume all paused chats" action — one thread at a time, matching every other action on this page.
- Showing the pause's exact remaining duration in the UI beyond "the button is visible" — a nice-to-have, not requested.
- Any change to `core/order_actions.py` — untouched, this remains a conversation-level (not order-mutation) action.

## Testing

- `GET /admin/conversations/{thread_id}`: a paused conversation (future `paused_until`) returns that timestamp; an unpaused one (or one whose pause already expired) returns `null`.
- `POST /admin/conversations/{thread_id}/resume`: clears a real pause (subsequent `get_paused_until` returns `None`/a past time); 404s on an unknown thread id, matching the existing thread-detail endpoint's behavior; calling it on an already-unpaused conversation is a harmless no-op (still 200).
- Frontend smoke tests (existing convention): the button's element id and the new `paused_until`/`resume` fetch call are present in served `chats.html`/`chats.js`.

## Global constraints (restated)

- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- No schema/migration changes — reuses `pause_until`/`get_paused_until`, both already implemented in both store backends.
- `core/order_actions.py` untouched.
- No new secrets, no new Shopify/Meta API calls.
