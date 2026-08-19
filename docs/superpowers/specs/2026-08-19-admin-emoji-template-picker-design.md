# Admin Emoji Picker + Template Resend (sub-project 1g) — Design

**Status:** Approved by owner (2026-08-19), via conversational brainstorming.

## Problem

The admin manual-reply box (shipped 2026-08-19) is plain free text. The owner wants two things added to it, matching the mockup shown: an emoji button and a "template" button to the left of the text field. The template button should open a dialog listing the store's approved WhatsApp templates, pre-fill each template's variables from data the admin already has (the currently-selected order), let the admin edit any value before sending, and be built so a template approved in the future needs no code changes beyond one registry entry.

## What already exists (verified by reading the code, not assumed)

- Four templates are sent today, all defined ad hoc inside `app/channels/shopify_webhook.py`, each auto-triggered by a Shopify webhook event, never manually:
  - `cod_confirmation` / `prepaid_order` (`TEMPLATE_NAME_COD`/`TEMPLATE_NAME_PREPAID`, `shopify_webhook.py:35-36`) — NAMED body params (`customer_name`, `order_id`, `product_name`, `product_color`, `product_size`, `product_amount`), optional `image_url` header, and — `cod_confirmation` ONLY — two quick-reply buttons `order:confirm:{gid}` / `order:cancel:{gid}` baked in at send time from the order's Shopify GID (`shopify_webhook.py:482-485`).
  - `order_shipped` / `order_delivered` (`TEMPLATE_NAME_SHIPPED`/`TEMPLATE_NAME_DELIVERED`, `shopify_webhook.py:54-55`) — POSITIONAL body params: shipped is `[name, order.name, tracking_company, tracking_url_or_number]`, delivered is `[name, order.name]`. No buttons.
  - All four are pinned to `language="en"` (Meta-approved in `en` only on this WABA — a documented, deliberate constraint, not an oversight).
- `app/admin/router.py::get_conversation_thread` already loads and returns every order for the thread's phone via `c.ingest.find_mirrored_orders_by_phone(user_id)` and `_order_summary(order)` (`router.py:762-812`) — this is already rendered in the admin UI's order panel, so the admin has ALREADY seen this data before opening the template dialog. `_order_summary` does not currently expose `order.gid` (only `order_name`, financial/fulfillment status, tracking fields, customer fields, line items) — the new endpoint will re-resolve the full `Order` object (including `gid`) server-side, never trusting a client-supplied gid.
- `app/channels/whatsapp_sender.py::send_template(http, cfg, to, template_name, language, body_params, button_payloads=(), header_image_url=None, timeout=20.0)` is the existing low-level sender — same function `delivery_retry.py`'s resend path already calls. Button payloads are opaque strings (`order:confirm:{gid}`) already parsed by the existing button-tap webhook handler in `core/order_actions.py` — reusing the EXACT same string format means a resend's Confirm/Cancel buttons work identically to the original auto-sent ones, with zero changes to `order_actions.py`.
- `app/store/base.py::OutboundDraft`/`IngestStore.enqueue_outbound` is the existing path that persists a template send as an `outbound_messages` row (what makes it show up in the admin thread with tick marks) — `_enqueue_and_send_fulfillment_notification` (`shopify_webhook.py:240-262`) is the exact pattern to mirror: build an `OutboundDraft`, `enqueue_outbound`, then `send_inline_outbound` (from `app/jobs/outbox_drain.py`) to actually send it inline within the request.
- The manual-reply feature (shipped 2026-08-19) established the precedent for an admin-initiated send bypassing `send_decision`/`send_mode` — the design decision there ("a deliberate, targeted admin action is not what the kill switch is for") applies identically here.

## Design

### 1. Template catalog — the single place a future template gets added

New module `backend/app/admin/template_catalog.py`. One dataclass, one dict, nothing else touches template-shape knowledge:

```python
@dataclass(frozen=True)
class TemplateField:
    key: str          # body-param key (named) or positional index name (positional)
    label: str        # shown in the dialog, e.g. "Customer name"
    default_from: str # dotted path into the resolved order summary, e.g. "customer_name",
                       # "order_name", "tracking_company", "tracking_link", "" (no default)

@dataclass(frozen=True)
class TemplateDef:
    language: str
    param_style: str          # "named" or "positional" (matches send_template's two body_params shapes)
    fields: tuple[TemplateField, ...]
    has_confirm_cancel_buttons: bool = False
    supports_image_header: bool = False

TEMPLATE_CATALOG: dict[str, TemplateDef] = {
    "cod_confirmation": TemplateDef(
        language="en", param_style="named",
        fields=(
            TemplateField("customer_name", "Customer name", "customer_name"),
            TemplateField("order_id", "Order ID", "order_name"),
            TemplateField("product_name", "Product name", "product_name"),
            TemplateField("product_color", "Color", "product_color"),
            TemplateField("product_size", "Size", "product_size"),
            TemplateField("product_amount", "Amount", "product_amount"),
        ),
        has_confirm_cancel_buttons=True, supports_image_header=True,
    ),
    "prepaid_order": TemplateDef(
        language="en", param_style="named",
        fields=(  # identical field set to cod_confirmation, no buttons
            TemplateField("customer_name", "Customer name", "customer_name"),
            TemplateField("order_id", "Order ID", "order_name"),
            TemplateField("product_name", "Product name", "product_name"),
            TemplateField("product_color", "Color", "product_color"),
            TemplateField("product_size", "Size", "product_size"),
            TemplateField("product_amount", "Amount", "product_amount"),
        ),
        supports_image_header=True,
    ),
    "order_shipped": TemplateDef(
        language="en", param_style="positional",
        fields=(
            TemplateField("name", "Customer name", "customer_name"),
            TemplateField("order_name", "Order #", "order_name"),
            TemplateField("tracking_company", "Courier", "tracking_company"),
            TemplateField("tracking_link", "Tracking link/number", "tracking_link"),
        ),
    ),
    "order_delivered": TemplateDef(
        language="en", param_style="positional",
        fields=(
            TemplateField("name", "Customer name", "customer_name"),
            TemplateField("order_name", "Order #", "order_name"),
        ),
    ),
}
```

(`product_name`/`product_color`/`product_size`/`product_amount`/`tracking_link` are DERIVED default-source keys, not raw `_order_summary` fields — see "Order-summary extension" below; a field with no sensible default uses `default_from=""` and starts blank.)

Adding a future template = one new `TEMPLATE_CATALOG` entry. No frontend code change is needed (the frontend renders fields generically from what the catalog-listing endpoint returns — see below) and no `send_template` change is needed (already generic). This satisfies the owner's "any if template come in future" requirement structurally, not by a promise.

### 2. Backend: two new admin-gated endpoints

**`GET /admin/conversations/{thread_id}/templates`** — returns, for every order belonging to this thread's customer, the list of available templates with pre-filled default values:
```json
{
  "orders": [
    {
      "order_name": "tavas4142",
      "templates": [
        {"key": "cod_confirmation", "label": "COD Confirmation",
         "fields": [{"key": "customer_name", "label": "Customer name", "value": "Shiva khatik"}, ...],
         "has_buttons": true},
        {"key": "order_shipped", "label": "Shipped Notice", "fields": [...], "has_buttons": false},
        ...
      ]
    }
  ]
}
```
Built by: resolve `user_id` from `thread_id` (404 if unknown, same as every other thread-scoped endpoint) → `c.ingest.find_mirrored_orders_by_phone(user_id)` → for each order, for each `TEMPLATE_CATALOG` entry, resolve each field's default via `default_from` against a small internal mapping built from the `Order` object (adds `product_name`/`product_color`/`product_size`/`product_amount` — parsed from `order.line_items[0]` the same way `_order_summary` already does for its own display, plus `tracking_link` = `tracking_url or tracking_number or ""`, matching `shopify_webhook.py`'s own fallback chain exactly). A template whose required defaults are structurally unavailable (e.g. no line items at all) is still listed — fields just start blank, since the admin can type any value; the endpoint never refuses to show a template.

**`POST /admin/conversations/{thread_id}/templates`** — body `{"order_name": str, "template": str, "values": dict[str, str]}`. Flow:
1. Resolve `user_id` from `thread_id` (404 if unknown).
2. Re-resolve the order SERVER-SIDE via `find_mirrored_orders_by_phone(user_id)`, matching `order_name` from the request against the authoritative list — 404 if no such order for this customer. **The order's `gid` used for `cod_confirmation`'s buttons always comes from this server-side lookup, never from the request body** — the request never carries a gid at all, only the human-readable `order_name`, mirroring the manual-reply endpoint's "recipient resolved server-side only" pattern.
3. `template = TEMPLATE_CATALOG.get(body.template)` — 400 if unknown (this is the whitelist; `values` keys not present in the catalog's `fields` for this template are ignored, never passed through to `send_template` blind).
4. Build `body_params` from `values` (falling back to the same default-resolution as the GET endpoint for any field the admin left unset/empty), in the shape (`dict` or `list`) `param_style` dictates.
5. `button_payloads = (f"order:confirm:{order.gid}", f"order:cancel:{order.gid}")` if `has_confirm_cancel_buttons`, else `()`.
6. Build an `OutboundDraft` (`dedupe_key=f"admin_resend:{template_key}:{order.gid}:{uuid4()}"` — a fresh key every send, since this is a deliberate repeat, not a dedupe-guarded automatic trigger) and `enqueue_outbound` + `send_inline_outbound`, mirroring `_enqueue_and_send_fulfillment_notification`'s exact shape.
7. `_audit("admin_template_resend", ...)`, return `{"ok": true}` / `{"ok": false, "error": ...}` matching the manual-reply endpoint's response contract.
8. **Respects `send_decision`/`send_mode`/`allowlist_phones` — does NOT bypass the kill switch**, unlike the free-text manual-reply endpoint. This is a deliberate difference: `send_inline_outbound` internally checks `send_mode` and cannot be told to ignore it without reimplementing its claim/finalize logic, and a template resend is architecturally the same category of message as every other automatic template send this codebase already gates behind `send_mode` (order confirmation, shipped, delivered) — respecting the same gate is the consistent, safer choice, unlike ad hoc free text where no existing gated pipeline applies. When `send_mode == "off"`, the resend is left queued for the backstop `outbox_drain` job rather than sent immediately or lost.

### 3. Frontend

- Two icon buttons added to the LEFT of `#reply-input` (per the mockup): an emoji button and a template button, inside `#reply-bar`.
- **Emoji button**: opens a small popup with a fixed curated grid of ~30 common emojis (no external library — self-contained, matching this project's no-CDN-dependency convention). Clicking one inserts it at the current cursor position in `#reply-input` (not just appended) and closes the popup.
- **Template button**: opens a dialog. Step 1: if the customer has more than one order, a dropdown to pick which order (skipped/auto-selected if only one); step 2: a list of that order's available templates (key + label); step 3, on picking one: a form with one text input per field (pre-filled from the GET response, all editable), a Send button, and a Cancel button. Submitting POSTs to the template-send endpoint, closes the dialog, and reloads the thread (same `loadThread()` refresh pattern as the manual-reply box).
- Both are purely additive to the existing reply bar — the free-text input/send button behavior (24h-window gating, draft-clearing on thread switch, etc.) is unchanged.

## Out of scope (YAGNI)

- Any language other than the templates' existing pinned `en` — matches the existing, deliberate, documented constraint.
- Editing/adding template DEFINITIONS from the admin UI — the catalog is a code-level registry (`template_catalog.py`), edited by a developer when Meta approves a new template. "Any future template" is satisfied by making that a one-entry-diff, not by a database-driven admin template builder (that's a materially different, much larger feature, not requested).
- A custom/animated emoji picker with search or skin-tone variants — a fixed common-emoji grid, matching what the mockup shows.
- Any change to `core/order_actions.py` — untouched; the resend's buttons reuse the EXACT existing `order:confirm:{gid}`/`order:cancel:{gid}` string format that file already parses, so no change is needed there for buttons to work.

## Testing

- `GET /admin/conversations/{thread_id}/templates`: 401 without auth, 404 for unknown thread, returns all 4 templates per order with correctly-resolved default values (spot-check each `default_from` mapping, including the `tracking_link` fallback chain and the line-item-derived product fields).
- `POST /admin/conversations/{thread_id}/templates`: 401/404/400 (unknown template key) as above; a successful `cod_confirmation` send produces an `outbound_messages` row with the correct named `body_params` AND the two button payloads built from the SERVER-resolved gid (test with a request body that tries to smuggle a different order's data and confirm it's ignored); a successful `order_shipped`/`order_delivered` send produces positional `body_params` matching `shopify_webhook.py`'s own existing shape exactly; admin-edited field values override the defaults; an admin-left-blank field falls back to the default; kill-switch respected confirmed (test with `send_mode="off"` leaving the row queued for the backstop drain and returning `{"ok": true}`, NOT sending — unlike the manual-reply endpoint's equivalent test, which confirms the opposite: that its free text still sends with `send_mode="off"`).
- Frontend smoke tests (existing markup/JS-substring style): the two new buttons exist; the emoji popup's insert-at-cursor logic; the template dialog wiring references the new GET/POST routes.

## Global constraints (already binding, restated for this feature)

- `core/order_actions.py` untouched.
- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- The order `gid` used for button payloads is ALWAYS resolved server-side from the thread's own mirrored orders — never accepted from the request body.
- The template-resend endpoint respects `send_decision`/`send_mode`/`allowlist_phones` (via `send_inline_outbound`'s existing gating) — unlike the manual-reply endpoint's free text, it does NOT bypass the kill switch. See "Design" section 2, point 8 for the reasoning.
- This touches an outbound-send path with mutation-adjacent button payloads — a `security-reviewer` pass is required after `code-reviewer`, same as every other send-path feature.
