# Admin Chat Auto-Load-All (Bounded) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the admin chat page's filter chips (`Unread`/`Human`/`Unexchanged`/`Exchanged`), which only match threads already loaded into the browser, by auto-paging the thread list in the background (no button click) up to a bounded cap on open and on search-term change.

**Architecture:** Frontend-only change to `backend/app/admin/static/chats.js`. Extract the single-page fetch-and-merge logic currently inlined in `loadOlderThreads()` into a shared `fetchNextPage()` helper, then add a new `autoLoadRemainingThreads()` loop (capped at `AUTO_LOAD_MAX_PAGES = 10` total pages, ~500 threads) invoked from `loadThreadList()` right after the first page renders. The existing manual "Load older chats" button, its per-click 5-page guard, and the `GET /admin/conversations` endpoint are all unchanged and remain the fallback once the cap is hit.

**Tech Stack:** Vanilla JS (`chats.js`, no framework, no bundler — served as a static file). Tests: Python `pytest` + FastAPI `TestClient`, asserting on substrings of the served JS text (this file has no browser/JS test runner — see `backend/tests/admin/test_static_mount.py` for the established pattern).

## Global Constraints

- No backend/endpoint changes — `GET /admin/conversations`, its keyset pagination, rate limit (120/minute), and page-size cap (`limit`, default 50 / max 100) are untouched.
- `AUTO_LOAD_MAX_PAGES = 10` (first page + 9 additional auto-fetched pages, ~500 threads at `limit=50`).
- Auto-load fires from both triggers that call `loadThreadList()` today: initial page open and a search-term change (debounced). It must NOT fire from `refreshFirstPage()` (the manual Refresh button) or from `pollTick()` — neither of those calls `loadThreadList()`, so no change is needed to keep them out of scope.
- Reuse the existing `loadingOlder` flag and `updateLoadOlderButton()` during auto-load so the manual button stays hidden/disabled for the loop's duration (prevents a race if the operator clicks it mid-auto-load).
- Existing tests in `backend/tests/admin/test_static_mount.py` must keep passing unmodified — the refactor must not remove any of the substrings they assert on (`before_last_active_at`, `loadOlderThreads`, `next_cursor`, `has_more`, `conversationsUrl`, `q: currentQuery`).
- Spec: `docs/superpowers/specs/2026-08-24-admin-chat-auto-load-all-design.md`.

---

### Task 1: Extract `fetchNextPage()` and refactor `loadOlderThreads()` to use it

**Files:**
- Modify: `backend/app/admin/static/chats.js:733-769` (current `loadOlderThreads()`, shown in full below)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: existing page-local state `allThreads`, `nextCursor`, `hasMore`, `currentQuery`, `loadingOlder` (all declared at `chats.js:648-654`); existing `mergeThreads(existing, incoming)` (`chats.js:665-674`); existing `conversationsUrl(params)` (`chats.js:656-663`); existing `api(path, method, body)` (`chats.js:6-35`).
- Produces: `async function fetchNextPage(): Promise<boolean>` — fetches ONE older page using the current `nextCursor`, merges it into `allThreads` via `mergeThreads`, updates `nextCursor`/`hasMore` from the response, and returns `true` if any new thread was actually added (`false` on a zero-net-new page). Task 2 calls this function directly.

This is a pure refactor — `loadOlderThreads()`'s external behavior (guard loop of 5, zero-net-new skip, `loadingOlder`/button state, error handling) must not change. The current code, for reference:

```js
async function loadOlderThreads() {
  if (!hasMore || loadingOlder || nextCursor === null) return;
  loadingOlder = true;
  updateLoadOlderButton();
  try {
    // Auto-continue across zero-net-new pages. The list is a union of sources deduped per phone by
    // their MAX stamp, but the server truncates each page to `limit` on that union BEFORE the client
    // is consulted -- so a phone whose stamp straddles the cursor across sources can re-occupy a page
    // slot and crowd out a genuinely-unseen older phone, making a whole page add zero net-new threads
    // while `has_more` is still true and real older threads remain (would have permanently hidden
    // "Load older" if we stopped on the first empty-feeling page). Keep paging while the server
    // reports more: the keyset cursor STRICTLY decreases every page (every source row is `< before`,
    // so next_cursor < before), which guarantees this loop terminates. The bound is a hard backstop
    // kept SMALL (5) so one click can never fan out into a long burst of source+per-thread queries
    // against the shared max_size=5 pool the live webhook/reply path uses -- if a genuinely-crowded
    // run needs more, the operator clicks again (has_more keeps the button visible).
    let addedNew = false;
    for (let guard = 0; guard < 5 && !addedNew; guard++) {
      const body = await api(
        conversationsUrl({ q: currentQuery, before_last_active_at: nextCursor })
      );
      const before = allThreads.length;
      allThreads = mergeThreads(allThreads, body.threads);
      addedNew = allThreads.length > before;
      nextCursor = body.next_cursor;
      hasMore = body.has_more;
      if (!hasMore || nextCursor === null) break;
    }
    renderThreadRows(applyThreadFilters(allThreads));
    renderFilterChips();
  } catch (e) {
    el("list-status").textContent = e.message;
  } finally {
    loadingOlder = false;
    updateLoadOlderButton();
  }
}
```

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_js_has_shared_fetch_next_page_helper(client: TestClient) -> None:
    # loadOlderThreads' single-page fetch-and-merge body must be extracted into a shared
    # fetchNextPage() helper so the auto-load loop (added next) can reuse it instead of
    # duplicating the fetch/merge/cursor-update logic.
    js = client.get("/admin/ui/chats.js").text
    assert "async function fetchNextPage()" in js
    assert "await fetchNextPage()" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py::test_chats_js_has_shared_fetch_next_page_helper -v`
Expected: FAIL — `fetchNextPage` not present in `chats.js` yet.

- [ ] **Step 3: Extract the helper and refactor `loadOlderThreads()`**

Replace `chats.js:733-769` (the full `loadOlderThreads()` function shown above) with:

```js
async function fetchNextPage() {
  // Fetches ONE older page using the current cursor, merges it into allThreads, and updates the
  // paging state (nextCursor/hasMore). Returns true if any new thread was actually added. Shared
  // by the manual "Load older" button (loadOlderThreads, below) and the auto-load-on-open/search
  // loop (autoLoadRemainingThreads) so there is one source of truth for "fetch the next keyset page".
  const body = await api(
    conversationsUrl({ q: currentQuery, before_last_active_at: nextCursor })
  );
  const before = allThreads.length;
  allThreads = mergeThreads(allThreads, body.threads);
  const addedNew = allThreads.length > before;
  nextCursor = body.next_cursor;
  hasMore = body.has_more;
  return addedNew;
}

async function loadOlderThreads() {
  if (!hasMore || loadingOlder || nextCursor === null) return;
  loadingOlder = true;
  updateLoadOlderButton();
  try {
    // Auto-continue across zero-net-new pages. The list is a union of sources deduped per phone by
    // their MAX stamp, but the server truncates each page to `limit` on that union BEFORE the client
    // is consulted -- so a phone whose stamp straddles the cursor across sources can re-occupy a page
    // slot and crowd out a genuinely-unseen older phone, making a whole page add zero net-new threads
    // while `has_more` is still true and real older threads remain (would have permanently hidden
    // "Load older" if we stopped on the first empty-feeling page). Keep paging while the server
    // reports more: the keyset cursor STRICTLY decreases every page (every source row is `< before`,
    // so next_cursor < before), which guarantees this loop terminates. The bound is a hard backstop
    // kept SMALL (5) so one click can never fan out into a long burst of source+per-thread queries
    // against the shared max_size=5 pool the live webhook/reply path uses -- if a genuinely-crowded
    // run needs more, the operator clicks again (has_more keeps the button visible).
    let addedNew = false;
    for (let guard = 0; guard < 5 && !addedNew; guard++) {
      addedNew = await fetchNextPage();
      if (!hasMore || nextCursor === null) break;
    }
    renderThreadRows(applyThreadFilters(allThreads));
    renderFilterChips();
  } catch (e) {
    el("list-status").textContent = e.message;
  } finally {
    loadingOlder = false;
    updateLoadOlderButton();
  }
}
```

- [ ] **Step 4: Run the new test and the full existing suite to verify no regression**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: PASS — the new test passes, and every pre-existing test in this file (in particular `test_chats_js_supports_load_older_pagination`, which checks `before_last_active_at`, `loadOlderThreads`, `next_cursor`, `has_more`) still passes unmodified, since the refactor preserves those substrings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "refactor(admin): extract fetchNextPage() out of loadOlderThreads"
```

---

### Task 2: Auto-load remaining threads on open/search, up to `AUTO_LOAD_MAX_PAGES`

**Files:**
- Modify: `backend/app/admin/static/chats.js:715-731` (current `loadThreadList()`, shown in full below); insert the new constant + function immediately after it (before `loadOlderThreads()`, which Task 1 placed at what was line 733)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `fetchNextPage()` from Task 1; existing `hasMore`, `loadingOlder`, `el()`, `updateLoadOlderButton()`, `renderThreadRows()`, `applyThreadFilters()`, `renderFilterChips()`.
- Produces: `AUTO_LOAD_MAX_PAGES` constant (`10`); `async function autoLoadRemainingThreads(): Promise<void>` — no other task consumes this; it's invoked only from `loadThreadList()`.

Current `loadThreadList()`, for reference:

```js
async function loadThreadList() {
  // A fresh load of the FIRST page (initial load, refresh button, or a search-term change). Resets
  // the paging state -- any previously appended older pages are discarded and re-fetched on demand.
  try {
    const body = await api(conversationsUrl({ q: currentQuery }));
    allThreads = body.threads;
    nextCursor = body.next_cursor;
    hasMore = body.has_more;
    renderThreadRows(applyThreadFilters(allThreads));
    renderFilterChips();
    updateLoadOlderButton();
    listSnapshotKey = threadListKey(body.threads);
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  }
}
```

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_js_auto_loads_all_threads_up_to_a_cap(client: TestClient) -> None:
    # "All chats on load": the filter chips (Unread/Human/Unexchanged/Exchanged) are client-side
    # over allThreads, so a matching thread sitting on page 2+ was invisible to every filter and
    # its count until "Load older chats" was clicked enough times to reach it. Auto-page in the
    # background (bounded, so the shared DB pool is never hit with an unbounded burst) instead of
    # requiring a manual click for realistic chat volumes.
    js = client.get("/admin/ui/chats.js").text
    assert "const AUTO_LOAD_MAX_PAGES = 10" in js
    assert "async function autoLoadRemainingThreads()" in js
    assert "await autoLoadRemainingThreads()" in js
    assert "Loading chats" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py::test_chats_js_auto_loads_all_threads_up_to_a_cap -v`
Expected: FAIL — none of `AUTO_LOAD_MAX_PAGES`, `autoLoadRemainingThreads`, or `"Loading chats"` exist in `chats.js` yet.

- [ ] **Step 3: Implement the auto-load loop and wire it into `loadThreadList()`**

Replace `chats.js:715-731` (the full `loadThreadList()` function shown above) with:

```js
async function loadThreadList() {
  // A fresh load of the FIRST page (initial load, refresh button, or a search-term change). Resets
  // the paging state -- any previously appended older pages are discarded and re-fetched on demand.
  try {
    const body = await api(conversationsUrl({ q: currentQuery }));
    allThreads = body.threads;
    nextCursor = body.next_cursor;
    hasMore = body.has_more;
    renderThreadRows(applyThreadFilters(allThreads));
    renderFilterChips();
    updateLoadOlderButton();
    listSnapshotKey = threadListKey(body.threads);
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
    return;
  }
  await autoLoadRemainingThreads();
}

// Auto-load cap for the loop below: 10 total pages (the first page fetched by loadThreadList
// above counts as page 1) at limit=50/page = up to ~500 threads loaded with no operator click.
// Bounded so a store that grows well past this still falls back to the existing manual
// "Load older chats" button instead of an unbounded burst against the shared DB pool.
const AUTO_LOAD_MAX_PAGES = 10;

async function autoLoadRemainingThreads() {
  // Called right after loadThreadList's first page. Keeps fetching subsequent pages (reusing
  // fetchNextPage, the same fetch loadOlderThreads uses) until history is exhausted or
  // AUTO_LOAD_MAX_PAGES is reached, so the filter chips above (which only match allThreads) see
  // the full loaded set with no click for realistic chat volumes. loadingOlder/updateLoadOlderButton
  // are reused so the manual button stays hidden/disabled for the loop's duration.
  if (!hasMore) return;
  loadingOlder = true;
  updateLoadOlderButton();
  el("list-status").textContent = "Loading chats…";
  try {
    let pagesFetched = 1;
    while (hasMore && pagesFetched < AUTO_LOAD_MAX_PAGES) {
      await fetchNextPage();
      pagesFetched++;
      renderThreadRows(applyThreadFilters(allThreads));
      renderFilterChips();
    }
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  } finally {
    loadingOlder = false;
    updateLoadOlderButton();
  }
}
```

- [ ] **Step 4: Run the new test and the full existing suite to verify no regression**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: PASS — the new test passes, and every pre-existing test in this file still passes (in particular the Task 1 test and `test_chats_js_supports_load_older_pagination`).

- [ ] **Step 5: Run the full backend test suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS, no unrelated regressions (this change touches only `chats.js`, which no Python code imports, so this is a sanity check rather than an expected-impact area).

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): auto-load chat threads up to a bounded cap so filters see the full loaded set"
```

---

## Manual Verification (after both tasks)

Not part of either task's automated test (this file has no browser test runner — see Global Constraints), but should be done once before considering the feature done, per this project's verification-before-completion norm:

1. Run the app locally, open `/admin/ui/chats.html` with a store/test data that has more than 50 threads and at least one unread thread beyond the first 50 (sorted by `last_active_at`).
2. Confirm the thread list keeps growing automatically after open (no click), `list-status` briefly shows "Loading chats…", and the "Load older chats" button never appears if total threads are under ~500.
3. Click the `Unread` filter chip and confirm the previously-buried unread thread now shows up with a nonzero count, matching the reported bug.
4. Type a search term that matches a thread beyond the first page and confirm it also auto-loads.
