# Exchange Filter Chips — Design

> Owner-directed. Approved 2026-08-21.

## Problem

The admin Chats pane has three filter chips today (All / Unread / Handed to human), rendered from `FILTERS` in `backend/app/admin/static/chats.js`. The owner wants two more so they can quickly find exchange requests that still need a response versus ones already being handled: **"Unprocessed Exchange"** and **"Processed Exchange"**. In the owner's own words, unprocessed means "customer ask for [exchange] but i have not respond like adding return tracking link or changing status."

## Scope

Two new chips added to the existing chip mechanism, backed by two new boolean fields on each thread object returned by `GET /admin/conversations`. No new endpoint, no new admin-panel section, no change to the per-order exchange editor (`renderExchangeDetail` / `POST /admin/exchanges/{id}`) that already sets status and tracking URLs — this only reads that same data to classify each thread.

## Definition (owner-confirmed)

An exchange request (`app/core/exchange_models.py::ExchangeRequest`, fields `status: ExchangeStatus` and `return_tracking_url: str | None`) is:
- **Unprocessed** if `status == "requested"` AND `return_tracking_url` is not set — nothing has happened since the customer asked.
- **Processed** if either condition no longer holds — status has moved forward (`return_picked_up` / `qc_passed` / `qc_failed` / `replacement_dispatched` / `delivered`) OR a return tracking URL has been added, even if status itself wasn't changed.

A thread (one phone number) can have multiple exchange requests across different orders. A thread counts as "Unprocessed Exchange" if **any** of its exchange requests is unprocessed, and "Processed Exchange" if **any** is processed — the two are not mutually exclusive at the thread level (a phone with one request of each kind will match both filters), matching how a thread can be both `unread` and not `ai_paused` today.

## Architecture

**Backend** (`backend/app/admin/router.py::list_conversations`): the existing per-thread loop (which already runs `get_or_create`, `find_messages_by_user_id`, `find_mirrored_orders_by_phone`, `count_unread_messages`, `get_paused_until` per displayed thread — 5 queries) gains a sixth: `await c.exchanges.list_for_phone(norm)` (the `ExchangeStore` protocol method already exists, `app/store/base.py:455`, implemented in both `postgres.py` and `memory.py`). From that list, compute:
- `exchange_unprocessed: bool` — `any(r.status == "requested" and not r.return_tracking_url for r in requests)`
- `exchange_processed: bool` — `any(r.status != "requested" or r.return_tracking_url for r in requests)`

Both added to the per-thread result dict alongside the existing `thread_id`/`phone`/etc. fields.

**Frontend** (`backend/app/admin/static/chats.js`): the `FILTERS` array (currently 3 entries: `all`, `unread`, `handoff`) gains two more:
```js
{ id: "exchange_unprocessed", label: "Unprocessed Exchange", predicate: (t) => !!t.exchange_unprocessed },
{ id: "exchange_processed", label: "Processed Exchange", predicate: (t) => !!t.exchange_processed },
```
No other change — `renderFilterChips()` and `applyThreadFilters()` are already fully generic over the `FILTERS` array; adding entries is the entire frontend change.

## Error handling

No special per-thread try/except — matches the existing convention for the other 5 per-thread queries in the same loop, none of which are individually wrapped. A store failure surfaces the same way any of those already would.

## Client-side filter scope (owner-confirmed)

These two chips filter only the currently-loaded page of threads, exactly like the existing Unread / Handed to human chips — not a new server-side search. This keeps the change consistent with the existing chip mechanism and avoids new backend search machinery for what is expected to be a low-volume feature (exchange requests, not the full chat history).

## Testing

Backend: a pytest against `list_conversations` seeding exchange requests in each relevant state (requested + no tracking URL; requested + tracking URL set; a non-`requested` status) and asserting `exchange_unprocessed`/`exchange_processed` come out correctly for each. Frontend: no new automated coverage — the existing Unread/Handed-to-human chips have no JS test coverage in this codebase either (backend fields are tested, not chip-rendering mechanics), and this follows that same precedent.
