# Admin Chat Date Divider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a WhatsApp-style centered date-divider pill before the first message of each new calendar day in the admin chat page's message view.

**Architecture:** Purely frontend (`chats.html`/`chats.js`), no backend/API/DB change — `entry.timestamp` is already present on every entry returned by `GET /admin/conversations/{thread_id}`. A new `formatBubbleDate` helper (sibling to the existing `formatBubbleTime`) plus a new `renderDateDivider` helper; the `loadThread` render loop tracks the last-rendered entry's local date and inserts a divider whenever it changes.

**Tech Stack:** Vanilla JS (`chats.js`), plain CSS in `chats.html`'s existing `<style>` block. No test runner exists for this frontend (documented, repo-wide, pre-existing gap) — verification is a manual owner browser pass.

## Global Constraints

- No "Today"/"Yesterday" relative labels — always the plain browser-locale date (owner's explicit choice).
- Inline divider only — no scroll-tracked/sticky header (owner's explicit choice).
- No backend/API/DB changes of any kind.
- `formatBubbleDate` must follow the exact same defensive shape as the existing `formatBubbleTime` (`chats.js:58-64`): missing/invalid timestamp → `""`, guarded with `Number.isNaN(d.getTime())`.
- CSS must match this file's existing WhatsApp-style palette (`#e9edef`, `#8696a0`, `#667781` are the muted/border tones already used throughout `chats.html`'s `<style>` block).
- `#chat-messages` is a flex column (`display: flex; flex-direction: column; gap: .5rem;`, `chats.html:44-45`) — bubbles use `align-self: flex-start`/`flex-end` (`chats.html:48-49`). A centered divider in this layout needs `align-self: center` (not `text-align: center` alone, which would have no effect on a flex item's own position).

---

### Task 1: Date divider helpers + render-loop wiring + CSS

**Files:**
- Modify: `backend/app/admin/static/chats.js`
- Modify: `backend/app/admin/static/chats.html`

**Interfaces:**
- Produces: `formatBubbleDate(isoTimestamp: string | null | undefined) -> string` and `renderDateDivider(dateLabel: string) -> HTMLElement`, both used only within this task (no other task consumes them — this is the only task in this plan).

- [ ] **Step 1: Add `formatBubbleDate` next to `formatBubbleTime`**

In `backend/app/admin/static/chats.js`, immediately after the existing `formatBubbleTime` function (`chats.js:58-64`):

```javascript
function formatBubbleTime(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true })
    .toLowerCase();
}
```

add:

```javascript

function formatBubbleDate(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString();
}
```

- [ ] **Step 2: Add `renderDateDivider`**

Immediately after `renderBubble` (`chats.js`, ends at line 321, right before `function renderOrderDetail(order) {`), add:

```javascript

function renderDateDivider(dateLabel) {
  const div = document.createElement("div");
  div.className = "date-divider";
  div.textContent = dateLabel;
  return div;
}
```

- [ ] **Step 3: Wire the divider into `loadThread`'s render loop**

In `backend/app/admin/static/chats.js`, `loadThread` (~line 429), find this block:

```javascript
    if (!data.entries.length) {
      container.innerHTML = '<div id="chat-empty">No messages yet</div>';
    } else {
      for (const entry of data.entries) {
        container.appendChild(renderBubble(entry));
      }
      container.scrollTop = container.scrollHeight;
    }
```

Replace it with:

```javascript
    if (!data.entries.length) {
      container.innerHTML = '<div id="chat-empty">No messages yet</div>';
    } else {
      let lastRenderedDate = "";
      for (const entry of data.entries) {
        const entryDate = formatBubbleDate(entry.timestamp);
        if (entryDate && entryDate !== lastRenderedDate) {
          container.appendChild(renderDateDivider(entryDate));
          lastRenderedDate = entryDate;
        }
        container.appendChild(renderBubble(entry));
      }
      container.scrollTop = container.scrollHeight;
    }
```

`lastRenderedDate` is declared fresh inside this `else` branch, so it resets on every call to `loadThread` (every thread switch and every poll-triggered refresh) — there is no stale cross-thread state to worry about.

- [ ] **Step 4: Add the `.date-divider` CSS rule**

In `backend/app/admin/static/chats.html`, in the existing `<style>` block, immediately after the `.bubble-status-error { color: #dc2626; }` rule (`chats.html:54`) and before the `.delivery-mark` rules, add:

```css
    .date-divider { align-self: center; background: #e9edef; color: #54656f;
      font-size: .72rem; padding: .25rem .7rem; border-radius: 8px; margin: .2rem 0; }
```

- [ ] **Step 5: Syntax-check the JS**

Run: `node --check backend/app/admin/static/chats.js`
Expected: no output (exit code 0) — this repo has no JS test runner, so this is the only automated check available for this file.

- [ ] **Step 6: Manual code-trace verification (no browser in most sandboxes — see Step 7 for the real check)**

Read through the modified `loadThread` block and confirm by inspection: entries are already returned in chronological ascending order by the API (`get_conversation_thread`'s `entries.sort(key=lambda e: str(e["timestamp"] or ""))`, `backend/app/admin/router.py`, unchanged by this task), so the divider-insertion logic only ever needs to detect a FORWARD date change, never reorder anything. Confirm `formatBubbleDate` is called on `entry.timestamp` (the same field `formatBubbleTime` already renders inside `renderBubble`), so both the divider and the bubble's own time are derived from the same source per entry — no risk of the divider disagreeing with the bubble it precedes.

- [ ] **Step 7: Manual browser verification (owner-performed)**

This repo has no frontend test runner and most sandboxes have no browser — this step must be performed by the owner in a real browser before considering the feature done:
1. Open `/admin/ui/chats.html`, open a thread whose history spans more than one calendar day (e.g. one of the older threads with several template sends across days).
2. Confirm exactly one divider pill appears at the start of each new day's messages, not one per message.
3. Confirm the divider's date matches the date of the messages that follow it (cross-check against the bubble timestamps' AM/PM times you'd expect for that date).
4. Confirm a thread with only one day of history shows exactly one divider (before its first message), not zero and not more than one.
5. Confirm the divider is horizontally centered, not stuck to the left/right like a bubble.

- [ ] **Step 8: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js
git commit -m "feat(admin): add date dividers to chat message view"
```

---

## Self-review notes (plan author)

- **Spec coverage:** `formatBubbleDate` helper (spec) ✓ Step 1; `renderDateDivider` helper (spec) ✓ Step 2; render-loop wiring with last-seen-date tracking, first entry always gets a divider (spec) ✓ Step 3; CSS matching the file's palette (spec) ✓ Step 4; manual-verification testing note (spec) ✓ Step 7.
- **Placeholder scan:** no TBD/TODO; every step has literal, complete code.
- **Type consistency:** `formatBubbleDate` and `renderDateDivider` signatures match between where they're defined (Steps 1-2) and where they're called (Step 3) — single task, no cross-task drift possible.
- **Scope:** single task is correct here — helpers, wiring, and CSS have no independent value shipped separately (a helper with no call site, or CSS with no element to style, isn't a testable deliverable on its own), so this doesn't need splitting per the Task Right-Sizing rule.

## Next steps after Task 1 is done

1. Route to `code-reviewer` (scoped to the 2 touched files: `chats.js`, `chats.html`).
2. No sensitive surface touched (no credentials, webhooks, mutations, auth, CORS, or store changes) — `security-reviewer` is not needed per `.claude/rules/common/agents.md`.
3. Owner performs the Step 7 manual browser verification.
4. Owner reviews → push after approval (never auto-push, per CLAUDE.md Rule 7).
