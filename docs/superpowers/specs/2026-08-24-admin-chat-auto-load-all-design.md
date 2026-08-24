# Admin chat page — auto-load all chats (bounded), fixing filter-chip blind spot

**Date:** 2026-08-24
**Status:** Approved (owner is sole decision-maker on this project — no separate client sign-off)

## Problem

The admin chat page (`backend/app/admin/static/chats.{html,js}`) loads only the first page of
threads (`limit=50`) on open, requiring a manual "Load older chats" click to page further back via
keyset pagination (`GET /admin/conversations?before_last_active_at=...`).

The filter chips (`All`/`Unread`/`Human`/`Unexchanged`/`Exchanged`, driven by `FILTERS` in
`chats.js`) are entirely **client-side**: `applyThreadFilters()` and the per-chip count both run
over `allThreads`, the in-memory array of whatever pages have been loaded so far. A thread that is
unread (or handed to a human, or has a pending exchange) but sits on page 2+ is invisible to every
filter — it shows in no chip's count and appears in no chip's filtered list — until the operator
manually clicks "Load older chats" enough times to reach it. Reported symptom: the Unread chip
shows no count and no rows even though an unread message exists, because it landed past page 1.

Search (`q` param) had this identical problem and was fixed on 2026-08-17 by moving it server-side.
The filter chips were never given the same fix.

## Chosen fix

Auto-load additional pages in the background (no button click) on the two places the thread list
does a "fresh first page" fetch — initial page open and a search-term change — up to a bounded cap,
so filters see the whole (bounded) loaded set without any operator action for realistic chat
volumes. This is a frontend-only change; the `/admin/conversations` endpoint, its keyset pagination,
and the per-click 5-page guard on the existing manual "Load older chats" button are all unchanged
and remain the fallback path if the cap is ever hit.

This does **not** make filters correct at unbounded scale — a store with more than the cap's worth
of total threads would still have chats past the cap invisible to filters until "Load older chats"
is clicked further. That tradeoff is accepted: making filters truly server-side (mirroring the `q`
search fix) was considered and explicitly deferred as out of scope for this change.

## Design

**Cap:** `AUTO_LOAD_MAX_PAGES = 10` total pages (the first page fetched by `loadThreadList()` counts
as page 1, so up to 9 additional automatic fetches), i.e. up to ~500 threads at the existing
`limit=50` page size, before falling back to the manual button.

**Shared fetch helper:** Extract the single-page fetch-and-merge body currently inlined in
`loadOlderThreads()`'s loop into `fetchNextPage()`:
- Calls `api(conversationsUrl({ q: currentQuery, before_last_active_at: nextCursor }))`.
- Merges the result into `allThreads` via the existing `mergeThreads()`.
- Updates `nextCursor` / `hasMore` from the response.
- Returns whether any new thread was actually added (existing "zero-net-new page" signal, used by
  the manual button's crowded-union skip logic).

`loadOlderThreads()` (the manual button handler) is refactored to call `fetchNextPage()` inside its
existing guarded loop (unchanged behavior: up to 5 pages per click, stops early on hitting the
zero-net-new guard or `hasMore` going false).

**Auto-load loop:** After `loadThreadList()` renders the first page (initial load or search-term
change — both go through this one function today), if `hasMore` is true:
1. Set `list-status` text to "Loading chats…" and reuse the existing `loadingOlder` flag (so
   `updateLoadOlderButton()` keeps the manual button hidden/disabled for the duration — prevents a
   race if the operator clicks it mid-auto-load).
2. Loop calling `fetchNextPage()`, re-rendering the thread list and filter chip counts after each
   page (so rows appear progressively rather than in one final jump), while `hasMore` is true and
   fewer than `AUTO_LOAD_MAX_PAGES` total pages have been fetched.
3. Clear `list-status` and unset `loadingOlder` when the loop ends (either exhausted history or hit
   the cap). `updateLoadOlderButton()` naturally shows the manual button only if `hasMore` is still
   true when the loop exits (cap hit with more history remaining) — for any store under ~500 total
   threads, it never appears.

No change to the zero-net-new-page skip semantics for the auto-load loop itself (YAGNI — that
crowded-union edge case is rare and already has a fallback: the manual button stays available
whenever `hasMore` remains true after the cap).

## Out of scope

- Server-side filter chips (deferred; noted above as the eventual full fix if a store outgrows the
  500-thread cap).
- Any change to `GET /admin/conversations`, its keyset pagination, rate limit, or per-request page
  size cap (`limit`, default 50 / max 100).
- Any change to the existing "known limitation, parked" items (uncapped unread badge, no empty-state
  message) noted in `component_registry.md`.

## Testing

This file has an accepted, existing structural gap: no browser/JS test runner in this repo, only
Python `TestClient` substring-presence checks against the served static JS (see
`backend/tests/admin/test_static_mount.py`). Follow that existing pattern rather than introducing a
new one — e.g. assert the served `chats.js` contains the new `AUTO_LOAD_MAX_PAGES` constant / helper
function name as a shipped-marker, consistent with how existing features in that test file are
verified.
