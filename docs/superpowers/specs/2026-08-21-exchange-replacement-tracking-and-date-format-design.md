# Exchange admin panel: replacement tracking URL + dd/mm/yyyy dates

Date: 2026-08-21
Status: Approved

## Problem

The admin exchange panel (chats.js `renderExchangeDetail`) has one tracking-URL box,
labelled "Return tracking URL", used for the courier tracking link when the customer's
returned item is picked up. There is no equivalent box for the tracking link of the
*replacement* order sent back out to the customer.

Separately, `formatBubbleDate` (chats.js) renders exchange/chat dates via
`Date.prototype.toLocaleDateString()`, which follows the admin's browser locale. On a
US-locale browser this renders mm/dd/yyyy (e.g. "8/21/2026"), which reads wrong for an
India-based admin expecting dd/mm/yyyy ("21/08/2026").

## Scope

Additive only: one new nullable column, one new optional model field, one new input box,
and one date-formatting fix applied at its single call site. No restructuring, no change
to existing exchange statuses or flow.

## Changes

### 1. `replacement_tracking_url` field

- `backend/app/store/schema.sql`: add
  `ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS replacement_tracking_url text;`
  directly after the existing `exchange_requests` block, following the same idempotent
  additive-migration pattern already used throughout this file (e.g. `orders.updated_at`,
  `fulfillments.delivered_at`). Applied automatically at startup — not a manual/blind
  migration.
- `backend/app/core/exchange_models.py`: `ExchangeRequest` gains
  `replacement_tracking_url: str | None`, mirroring `return_tracking_url`.
- `backend/app/store/base.py`: abstract `set_replacement_tracking_url(id: int, url: str) -> None`,
  mirroring `set_return_tracking_url`.
- `backend/app/store/memory.py` and `backend/app/store/postgres.py`: implement the new
  setter and include the column in row construction/mapping, mirroring the existing
  `return_tracking_url` handling exactly (same nullability, same update-touches-`updated_at`
  behavior).
- `backend/app/admin/router.py`:
  - `ExchangeUpdateRequest` gains `replacement_tracking_url: str | None = Field(default=None, max_length=2048)`.
  - `update_exchange` calls `c.exchanges.set_replacement_tracking_url(...)` when the field
    is provided, same pattern as the existing `return_tracking_url` branch.
  - `_order_summary`'s `exchange` dict gains `"replacement_tracking_url": exchange.replacement_tracking_url`.
- `backend/app/admin/static/chats.js` (`renderExchangeDetail`): a second text input,
  placeholder "Replacement order tracking URL", value from
  `order.exchange.replacement_tracking_url`, appended after the existing tracking input.
  Both inputs are always visible/editable regardless of exchange status (matches existing
  `return_tracking_url` box behavior — confirmed with owner, not gated on status). The
  existing Save button's POST body gains `replacement_tracking_url: <input>.value || null`.

### 2. dd/mm/yyyy date format

- `backend/app/admin/static/chats.js` `formatBubbleDate`: replace the
  `d.toLocaleDateString()` call with an explicit zero-padded `dd/mm/yyyy` build
  (`String(d.getDate()).padStart(2, "0") + "/" + String(d.getMonth() + 1).padStart(2, "0")
  + "/" + d.getFullYear()`), so the format no longer depends on the admin's browser locale.
- This is the only date-formatting call site in the admin UI (used for both the exchange
  "Requested on" field and the chat date dividers), so this single change covers both.

## Testing

- `backend/tests/store/test_exchange_store.py`: extend both the in-memory and Postgres
  suites with a `set_replacement_tracking_url` round-trip test, mirroring the existing
  `return_tracking_url` tests.
- `backend/tests/admin/test_views.py`: extend the `update_exchange` endpoint test(s) to
  cover `replacement_tracking_url` in the request body and response, mirroring the
  existing `return_tracking_url` coverage.
- No JS test suite exists in this repo (confirmed via search). The `chats.js` changes
  (new input box, date format) are verified by reading the diff, not by an automated test.

## Out of scope

- Any gating of the new box by exchange status (owner confirmed: always visible, like the
  existing box).
- Any other date-format call sites — none exist outside `formatBubbleDate`.
