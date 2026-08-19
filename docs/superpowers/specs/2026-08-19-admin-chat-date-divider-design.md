# Admin Chat Date Divider — Design

> Owner-directed, same-day follow-up to the admin chat unread-marker/filter-chips feature and the thread-list sort-order bugfix. Approved 2026-08-19.

## Problem

The admin chat page's message bubbles (`chats.html`/`chats.js`, `renderBubble`) show only a time (`formatBubbleTime`, e.g. "3:17 pm"), never a date. Scrolling through an older conversation gives no way to tell which day a message was sent on. The owner referenced WhatsApp Web's own date-divider pills (centered, e.g. "7/8/2026") as the reference UX.

## Scope

Add a centered date-divider pill before the first message of each new calendar day in a thread's message view. Purely frontend — the timestamp is already present on every entry (`entry.timestamp`, ISO 8601), no API/DB changes.

Out of scope: "Today"/"Yesterday" relative labels (owner chose always-plain-date); a sticky/scroll-tracked header (owner chose a simple inline divider); date markers on the thread LIST (that already shows a date per row via `renderThreadRows`, unrelated to this feature, which is about the per-message bubble timeline).

## Design

**`formatBubbleDate(isoTimestamp) -> string`** — new helper in `chats.js`, sibling to the existing `formatBubbleTime`. Same defensive shape (invalid/missing timestamp → `""`, guarding with `Number.isNaN(d.getTime())` exactly like `formatBubbleTime` does). Uses `d.toLocaleDateString()` (browser-default locale, no hardcoded format) so it renders consistent with the owner's screenshot (`D/M/YYYY`, e.g. `19/8/2026`) without a format string tied to one locale — mirrors how `formatBubbleTime` already defers to the browser's locale for time formatting.

**`renderDateDivider(dateLabel) -> HTMLElement`** — new helper, a small `<div>` with a centered pill (rounded, light background, small muted text) containing `dateLabel`. Pure presentation, no state.

**Render-loop change in `loadThread`** (`chats.js`, the `for (const entry of data.entries)` loop that calls `renderBubble`): track the local calendar date of the previously-rendered entry (a plain JS variable scoped to this render pass, reset each time `loadThread` runs). Before appending each entry's bubble:
1. Compute the entry's local date via `formatBubbleDate(entry.timestamp)`.
2. If it's non-empty and differs from the tracked "last seen date" (including the very first entry, where there is no previous date to compare against), append a divider via `renderDateDivider` first, then update the tracked date.
3. Append the bubble as today.

An entry with a missing/invalid timestamp contributes no divider (its computed date is `""`, which never differs meaningfully) and does not disturb the tracker — the next valid-timestamped entry is compared against whatever date was last seen.

**CSS:** a new `.date-divider` rule in `chats.html`'s existing `<style>` block, matching the file's WhatsApp-style palette (`#e9edef`/`#54656f`-ish muted tones already used elsewhere in this file) — centered pill, small padding, small font-size, vertical margin to separate it from surrounding bubbles.

## Testing

No frontend test runner exists in this repo (documented, repo-wide, pre-existing gap — every prior `chats.js` change has the same limitation). Verification is a manual owner browser pass: open a thread spanning multiple days, confirm one divider appears per day boundary (not per message), in the right position (before the first message of that day), with no divider before the very first day if there's only one day of history.
