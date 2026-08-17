# Chat Page Polish (sub-project 1b) — Design

**Status:** Approved by owner (2026-08-17), via conversational brainstorming (not the visual companion).

## Problem

The dedicated WhatsApp-style chat page (shipped and reviewed, 2026-08-17) is functionally correct
but rough around the edges compared to what the owner actually wants:

- The thread list shows only phone numbers, never the customer's name.
- The order panel is missing the order number (hidden whenever the customer has only one order,
  since the dropdown itself is hidden) and shows no product/line-item details.
- Chat bubbles for template sends show raw internal data (`order_shipped → Chiranjiv .,
  tavas4029, Delhivery Surface, https://...`) instead of anything resembling the real message the
  customer received, and bury the send state (`[suppressed]`/`[sent]`/`[failed]`) as a text prefix
  instead of a clear status indicator.
- No way to search the thread list; manual-refresh-only means the owner has to keep clicking to
  see new activity.

This is a presentation/polish pass on the already-shipped, already-reviewed page and its two
supporting endpoints (`GET /admin/conversations`, `GET /admin/conversations/{thread_id}`) — no new
endpoints, no schema changes, no new Shopify calls (`Order` already carries every field needed).

Read/delivered status ticks (blue double-tick like real WhatsApp) are explicitly a SEPARATE,
larger sub-project (1c) — this codebase has no handling at all today for Meta's WhatsApp message
*status* webhooks (sent/delivered/read receipts, distinct from inbound messages), so that requires
a new webhook branch and new storage. Out of scope here.

## What already exists (verified by reading the code, not assumed)

- `Order` (`app/shopify/models.py`) already carries `name`, `line_items: tuple[LineItem, ...]`,
  and `customer: Customer | None` (with `first_name`/`last_name`) — no new Shopify calls needed for
  any of the additions below.
- `GET /admin/conversations` (`router.py:619-680`) unions three phone sources, materializes a
  `thread_id` per phone via `get_or_create`, and already sorts by `last_active_at` descending
  (`router.py:679`) — the "latest message first" requirement is already met; nothing to change
  there.
- `GET /admin/conversations/{thread_id}` (`router.py:705-750`) already resolves `user_id` once and
  uses it for every downstream query, including `find_mirrored_orders_by_phone(user_id)` for the
  `orders` field — reused, not re-derived, by every addition below.
- `_order_summary()` (`router.py:683-702`) already includes `order_name` in its output; the
  *frontend* just never displays it when the dropdown is hidden (`chats.js:85`,
  `select.style.display = orders.length > 1 ? "block" : "none"`) — a display bug, not a missing
  field, for that one part.
- `_template_sent_text()`/`row.state` (`router.py:594-612`, `724-729`) already have everything
  needed to build both a clean message string and a status label — currently concatenated into one
  string (`f"[{row.state}] {_template_sent_text(...)}"`, `router.py:728`).
- `normalize_order_name()` (`app/shopify/models.py:145-149`) already turns bare digits (`"3589"`)
  into the store's order-name format (`"tavas3589"`) — reused for search matching, not
  reimplemented.

## Design

### 1. Backend: `_order_summary()` gains `line_items`

Add one field to `_order_summary()`'s return dict: `"line_items": [{"title": li.title, "quantity":
li.quantity, "variant_title": li.variant_title, "price_amount": li.price.amount if li.price else
None, "price_currency": li.price.currency if li.price else None} for li in order.line_items]`. No
new query — `order.line_items` is already populated by `find_mirrored_orders_by_phone`.

### 2. Backend: `list_conversations()` gains `customer_name` and `order_names` per row

Inside the existing per-phone loop in `list_conversations()` (`router.py:664-677`, which already
does two per-row queries: `get_or_create`, `find_messages_by_user_id`), add a third:
`orders_for_phone = await c.ingest.find_mirrored_orders_by_phone(norm)`. From it, derive:
- `customer_name`: from the most-recently-updated order's `customer` (`first_name` + `last_name`,
  stripped and joined with a space; `None` if no orders or no customer name parts present).
- `order_names`: `[o.name for o in orders_for_phone]` (empty list if none).

Both become new keys on each row's dict alongside the existing `thread_id`/`phone`/
`last_active_at`/`preview`. This is one more bounded per-row query, matching the existing N+1-per-
row precedent already accepted and commented on in this function (capped by `limit`, admin-only).

### 3. Backend: template message reconstruction + separated status

Replace the single concatenated string at `router.py:728` with two separate entry fields:
`"text": _template_message_text(row.payload_json)` and `"status": row.state`. New function
`_template_message_text()` (replacing `_template_sent_text()`'s call site for this one purpose;
`_template_sent_text()` itself stays as-is for its fallback role) parses `payload_json` exactly
like `_template_sent_text()` does, then looks up `data["template"]` in a new module-level
`_TEMPLATE_MESSAGE_TEMPLATES: dict[str, str]` mapping template name to a Python format string built
from that template's known param order (positional templates: `cod_confirmmsg`, `cod_cancel`,
`order_shipped`, `order_delivered`; named templates: `cod_confirmation`, `prepaid_order` — see each
feature's own design doc for exact param lists, already verified against the live WABA when those
features shipped). Example entries:

```python
_TEMPLATE_MESSAGE_TEMPLATES: dict[str, str] = {
    "cod_confirmmsg": "Hi {0}, your order {1} has been confirmed. We will ship it soon.",
    "cod_cancel": "Hi {0}, your order {1} has been cancelled as requested.",
    "order_shipped": "Hi {0}, your order {1} has shipped via {2}. Track it here: {3}",
    "order_delivered": "Hi {0}, your order {1} has been delivered. Thank you for shopping with us!",
    "cod_confirmation": (
        "Hi {customer_name}, please confirm your Cash on Delivery order {order_id} for "
        "{product_name} ({product_color}, {product_size}) — {product_amount}."
    ),
    "prepaid_order": (
        "Hi {customer_name}, your order {order_id} for {product_name} ({product_color}, "
        "{product_size}) — {product_amount} has been received."
    ),
}
```

This is a **best-effort approximation**, not Meta's literal approved copy (not stored anywhere in
this codebase) — acceptable because it's admin-tooling display only, never sent anywhere. If
`data["template"]` isn't in the map, or substitution fails (wrong param count/shape — defensive,
should not happen given the map is hand-built from each template's own known shape), fall back to
today's `_template_sent_text()` output unchanged, so this can never crash or blank out a bubble.

### 4. Frontend: order panel shows order number + products, always

`renderOrderDetail()` (`chats.js`) gets a new header line rendering `order.order_name` (always,
regardless of dropdown visibility) and a new "Products" section listing each `line_items` entry as
`"{quantity}× {title}{variant ? ' (' + variant + ')' : ''} — {price_amount} {price_currency}"`
(omit the price fragment if `price_amount` is null). Same `.textContent`/`createTextNode` pattern
as the rest of the file — no `.innerHTML` with dynamic content.

### 5. Frontend: bubbles render clean text + separate status label

`renderBubble()` reads the new `entry.status` field (present only on `template_sent` entries,
`undefined` elsewhere). When `status` is present and not `"sent"`, append a small label under the
message text (e.g. `"Not delivered — suppressed"`, `"Failed to send"`, `"Queued"` — one line per
state, styled muted/gray, red only for `"failed"`/`"undeliverable"`). When `status === "sent"` or
absent, no label — matching how a real WhatsApp message with no delivery problem shows nothing
extra.

### 6. Frontend: thread list shows name, supports search

Each thread row renders `t.customer_name ? \`${t.customer_name} (${t.phone})\` : t.phone` as its
primary line (falls back to phone-only exactly like today when no name is known). A new search
input above the list filters the already-fetched `threads` array client-side on every keystroke:
a thread matches if the query (lowercased, trimmed) is a substring of `phone`, `customer_name`, or
any entry in `order_names`, OR if the query is all-digits and `normalize_order_name(query)` matches
any `order_names` entry (so typing `3589` matches `tavas3589` — `normalize_order_name`'s existing
`isdigit()` branch already does exactly this transform; the frontend reimplements just that one
branch in JS since it's a two-line pure function, not worth a new API round trip per keystroke).
Filtering re-renders the list from the cached array, no new request.

### 7. Frontend: 3-second diff-checked poll

Replace the manual-refresh-only model with `setInterval(pollTick, 3000)`. `pollTick()`:
1. Fetches `/admin/conversations` (same call `loadThreadList()` already makes).
2. Compares the fetched array's `(thread_id, last_active_at)` pairs against the currently-rendered
   set (kept in a module-level `let renderedThreadSnapshot`). If unchanged, do nothing — no
   re-render, no flicker, search box and scroll position stay untouched.
3. If changed, re-render the thread list (preserving the current search-box filter and the
   `active` highlight on `currentThreadId`) and update `renderedThreadSnapshot`.
4. If a thread is currently open (`currentThreadId !== null`), also fetches that thread and
   compares its `entries` array length + last entry's `timestamp` against a second snapshot
   (`renderedThreadSnapshot_entries`); re-renders only on an actual difference, same as above.

The existing manual refresh button stays, calling the same full-refresh path unconditionally (an
explicit user action always gets a real refresh, poll-diffing is only for the automatic tick).
This is still plain polling (matches this admin panel's existing precedent, no new persistent-
connection infrastructure) — the 3-second interval combined with the diff-check is what produces
the "near-instant, no visible disruption when idle" feel the owner asked for, without a websocket/
SSE addition.

## Out of scope (YAGNI)

- Sent/delivered/read ticks (blue double-tick) — separate sub-project 1c, needs new Meta status-
  webhook handling and new storage, not built here.
- Server-Sent Events / websockets / any persistent connection — explicitly rejected in favor of
  diff-checked polling (owner's own choice during brainstorming).
- Server-side search endpoint — client-side filtering over the already-loaded thread list is
  sufficient at this admin tool's scale (capped at 100 threads).
- Literal, guaranteed-exact reconstruction of Meta's approved template copy — approximate wording
  is accepted since this is a display-only convenience, never sent anywhere.
- Any change to `core/order_actions.py`, the merge/dedupe logic, the opaque `thread_id` scheme, or
  either endpoint's auth — all untouched.
- Importing/backfilling older chat history from another source — noted by the owner as a future
  want, not part of this sub-project.

## Testing

- `_order_summary()`: an order with `line_items=()` returns `"line_items": []`, not a crash; an
  order with several line items returns each with all five sub-fields, `price_amount`/
  `price_currency` both `None` when a line item's `price` is `None`.
- `list_conversations()`: a phone with no mirrored orders returns `customer_name: None`,
  `order_names: []`; a phone with orders returns the most-recently-updated order's customer name
  and all order names; a phone with an order but no customer name parts returns `customer_name:
  None` (not an empty string).
- `_template_message_text()`: each of the 6 mapped templates substitutes correctly given a
  representative `payload_json`; an unmapped template name falls back to the existing
  `_template_sent_text()` format unchanged; a payload whose `body_params` shape doesn't match the
  expected param count for its template falls back cleanly (no exception).
- `get_conversation_thread()`: a `template_sent` entry carries separate `text` and `status` keys
  (`status` equal to the row's `state`); `customer_message`/`ai_reply`/`button_tap` entries have no
  `status` key at all (the dict simply omits it, matching how these three entry types are built in
  separate loops today with no shared base shape) — the frontend treats `entry.status` as
  `undefined` for these, per section 5.
- Frontend smoke tests (existing convention — Python `TestClient` markup/JS-substring assertions):
  `chats.html`/`chats.js` contain the new search input's id, the order panel's product-list
  container id, and the poll-interval `setInterval` call; no test can exercise actual browser
  timing/diffing behavior (documented as a manual-verification gap, same as the original page).

## Global constraints (already binding, restated for this feature)

- Admin-only surface — no new auth mechanism, `require_admin` unchanged.
- No schema/migration changes — every field added here already exists on `Order`/`LineItem`/
  `Customer` or is derived from data already fetched.
- `core/order_actions.py` untouched — this remains a strictly read-only feature.
- No new secrets, no new Shopify API calls, no new backend endpoints — both existing endpoints
  gain response fields only, no new routes.
