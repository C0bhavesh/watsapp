# Dedicated WhatsApp-Style Chat Page — Design

**Status:** Approved by owner (2026-08-17), via conversational brainstorming with the visual companion (layout + style mockups reviewed and picked in-browser).

## Problem

The read-only unified chat thread view (shipped and reviewed, 2026-08-17) was built as one card inside the existing admin settings panel, alongside Shopify creds, WhatsApp creds, knowledge base, and controls. The owner wants a genuine dedicated page instead — something that actually looks and feels like WhatsApp, not a table embedded among unrelated settings.

This is a presentation-layer redesign, not a new data feature: the backend (`GET /admin/conversations`, `GET /admin/conversations/{thread_id}`) is already built and fully reviewed. This spec covers a new page consuming that same API, plus one small, additive API extension (order details per thread).

## What already exists (verified by reading the code, not assumed)

- `GET /admin/conversations` — thread list, opaque `thread_id` per row, `{thread_id, phone, last_active_at, preview}`.
- `GET /admin/conversations/{thread_id}` — merged chat entries `{type, timestamp, text}` (`customer_message`/`ai_reply`/`template_sent`/`button_tap`), 404 on an unknown id. Both already admin-authenticated, already security-reviewed (opaque ids, dual-format phone lookup, no PII in URLs).
- `IngestStore.find_mirrored_orders_by_phone(phone_e164) -> list[Order]` already exists and already returns full `Order` objects including `fulfillments: tuple[Fulfillment, ...]` (tracking_company/tracking_number/tracking_url per fulfillment) — no new Shopify calls needed for the order-details panel.
- `Order` carries `name`, `financial_status`, `fulfillment_status`, `cancelled_at`, `tags`, `total` (a `Money(amount, currency)`), `is_cod()` — everything the details panel needs is already on this one object.
- The existing admin panel (`app/admin/static/{index.html,admin.js}`) is served via `StaticFiles` at `/admin/ui/` (`app/main.py`) — any new static file dropped in `app/admin/static/` is automatically reachable the same way, no new route registration needed for the page itself (only for the one new/extended API field).

## Design

### 1. New standalone page

`app/admin/static/chats.html` (+ a dedicated `app/admin/static/chats.js`, kept separate from `admin.js` since the settings panel and the chat page are now two independent surfaces with no shared state) — reachable at `/admin/ui/chats.html`, behind the exact same `require_admin` session cookie every other admin route already uses (no new auth mechanism; a direct navigation without a valid session gets the same 401 the API already returns, and the page's own JS should redirect to `/admin/ui/` — the login page — on a 401 from either endpoint). No link is added from the existing settings panel; the owner navigates directly. The existing embedded "Chats" card (`chats-card` in `index.html`/its `admin.js` functions) is removed from the settings panel — this page replaces it, not duplicates it.

### 2. Three-pane WhatsApp-style layout, WhatsApp's own color palette

Left: thread list (same data as today's card, same click-to-open interaction). Center: the open thread's chat bubbles (same four entry types, same left/right alignment already built and reviewed). Right: an order-details panel for the phone's most recent order by default, with a dropdown to switch between all of that customer's orders if they have more than one.

Colors/style (from the approved mockup): WhatsApp teal-green headers (`#00a884`), off-white chat background (`#efeae2`), pale-green outgoing bubbles (`#d9fdd3`), white incoming bubbles, near-black text (`#111b21`), muted gray secondary text (`#667781`) — a self-contained visual identity for this one page, intentionally not matching the indigo admin-panel palette used elsewhere (the owner explicitly chose "look like WhatsApp" over "match the settings panel").

### 3. Order-details panel — one small, additive API extension

`GET /admin/conversations/{thread_id}`'s response gains one new top-level field: `orders`, a list of `{order_name, financial_status, fulfillment_status, cancelled_at, is_cod, total_amount, total_currency, tags, tracking_company, tracking_number, tracking_url}` — built from `find_mirrored_orders_by_phone(resolved_phone)`, sorted most-recent-first (the endpoint already resolves `resolved_phone` from `thread_id` for the existing merge; this reuses that same resolved value, no new lookup chain). `financial_status`/`fulfillment_status`/`cancelled_at` are passed through as three SEPARATE fields, not collapsed into one derived string — this deliberately mirrors `app/agents/order_tracking.py`'s existing customer-facing pattern (verified by reading it: it presents "payment status X, fulfillment Y" as two distinct labels, never a single merged status), so the panel reads the same way staff already expect order state to be described elsewhere in this app, rather than inventing a new summarization convention. Tracking fields come from the order's FIRST fulfillment (`fulfillments[0]`) if any exist, else `null` — a split-shipment order showing only its first parcel's tracking in this summary panel is an accepted simplification (the full per-fulfillment detail isn't needed for a quick reference panel; staff needing more can already look up the order directly in `/admin/mappings` or Shopify itself).

The chat entries (`entries` field) themselves are completely unchanged — this is a strictly additive field on the same response, not a redesign of the merge logic already reviewed.

### 4. Interaction model — unchanged from the existing reviewed behavior

Manual refresh only (a button, like the rest of this admin panel) — no live polling/websockets, matching the explicit YAGNI decision already made for the read-only thread view. Still entirely read-only: no message composition, no send button — that remains a separate future sub-project (manual send), not built here. Selecting a different order in the details-panel dropdown only re-renders already-fetched data (the full `orders` list comes back in the one thread-load request) — no extra network round trip per dropdown change.

## Out of scope (YAGNI)

- Sending messages — a separate future sub-project.
- Any change to the thread-list endpoint (`GET /admin/conversations`) — untouched.
- Any change to the chat-entry merge logic, dedupe, or the opaque `thread_id` scheme — all already built and reviewed; this spec only adds the `orders` field alongside it.
- Live/auto-refresh — explicitly deferred, matches existing precedent.
- Per-fulfillment tracking detail for split shipments in this panel (only the first fulfillment shown) — an accepted simplification, not a gap to fix here.
- Any change to `core/order_actions.py` — this remains a read-only surface; the mutation-safety core is untouched.

## Testing

- New `IngestStore` consumption in the router: the `orders` field is built correctly from `find_mirrored_orders_by_phone`'s existing return shape — a thread with zero orders for that phone (shouldn't normally happen given every mirrored order implies at least one webhook interaction, but a customer whose only interaction was an AI chat with no linked order is possible) returns `orders: []`, not an error.
- The three status fields (`financial_status`, `fulfillment_status`, `cancelled_at`) pass through independently from the mirrored `Order` with no transformation — a test confirms all three appear in the response exactly as stored, including when one or more is `None`.
- Multiple orders for one phone: all returned, most-recent-first, each with independent tracking data.
- Tracking fields: an order with zero fulfillments returns `null` for all three tracking fields, not a crash.
- Frontend smoke tests (matching this repo's established convention for the static admin panel — no JS unit-test framework, Python tests asserting served HTML/JS contain expected markup/identifiers): `chats.html` is served at `/admin/ui/chats.html`, requires the same auth as every other admin route, contains the three-pane structure's key element ids; the old `chats-card`/its JS functions are confirmed REMOVED from `index.html`/`admin.js` (not just newly duplicated).

## Global constraints (already binding, restated for this feature)

- Admin-only surface — no new auth mechanism, reuses `require_admin` exactly.
- No schema/migration changes — `find_mirrored_orders_by_phone` already exists and is already populated.
- `core/order_actions.py` untouched — this remains a strictly read-only feature.
- No new secrets, no new Shopify API calls (all order data already mirrored).
