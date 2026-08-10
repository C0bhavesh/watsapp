# Order Item Details + Attractive Formatting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `order_tracking` reveal product name, variant, and price per line item (a
reversal of the earlier "items/amounts stay hidden" decision, now owner-approved), correctly
frame a Pending payment status as normal for Cash-on-Delivery orders, and give order-detail
replies a warmer, visually structured format (bold key fields, light emoji use).

**Architecture:** Extend the Shopify order query + `Order` model with line items; add `"items"`
as a fourth `reveal_fields` value (default-enabled); render items and COD-aware status text in
`order_tracking`; give its prompt explicit formatting guidance and a concrete example.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio.

## Global Constraints

- Critical Rule 3 (ownership check before revealing anything) — untouched; line items are
  attached to the same `AuthorizedOrder` already ownership-verified, no new disclosure path.
- `reveal_fields` stays config-driven (admin-editable, no redeploy needed to toggle) — matches
  every other disclosure control in this codebase.
- Full type hints; `mypy` strict clean; `ruff` clean; no bare `except`; no `print()`.
- Design spec: `docs/superpowers/specs/2026-08-10-order-item-details-design.md`.

---

### Task 1: Line items, disclosure toggle, COD framing, and formatted replies

**Files:**
- Modify: `backend/app/shopify/client.py`
- Modify: `backend/app/shopify/models.py`
- Modify: `backend/app/admin/controls.py`
- Modify: `backend/app/admin/static/index.html`
- Modify: `backend/app/admin/static/admin.js`
- Modify: `backend/app/agents/base.py`
- Modify: `backend/app/agents/order_tracking.py`
- Modify/Create tests: `backend/tests/test_client_reads.py`, `backend/tests/agents/test_order_tracking.py`, `backend/tests/admin/test_controls.py`

**Interfaces:**
- Consumes: existing `Order`/`Money`/`AuthorizedOrder` (`app/shopify/models.py`), existing `REVEAL_ALLOWED`/`AdminControls.reveal_fields` (`app/admin/controls.py`), existing `Order.is_cod()` (unused until now).
- Produces: `LineItem` dataclass (`app/shopify/models.py`) — new. `Order.line_items: tuple[LineItem, ...]` — new field. `REVEAL_ALLOWED` grows to 4 values.

- [ ] **Step 1: Read current state of every file before editing**

Read `backend/app/shopify/client.py`, `backend/app/shopify/models.py`,
`backend/app/admin/controls.py`, `backend/app/admin/static/index.html` (search for `c-rv-`),
`backend/app/admin/static/admin.js` (search for `c-rv-` and `reveal_fields`),
`backend/app/agents/base.py`, and `backend/app/agents/order_tracking.py`. Confirm current
content matches what this plan's steps assume before editing; adapt to actual current content
if anything has drifted.

- [ ] **Step 2: Add `LineItem` and extend `Order` in `backend/app/shopify/models.py`**

Find the `Order` dataclass. Add this new dataclass directly above it:

```python
@dataclass(frozen=True)
class LineItem:
    title: str
    quantity: int
    variant_title: str | None
    price: Money | None
```

Add one new field to `Order`, after `customer_locale: str | None`:

```python
    customer_locale: str | None
    line_items: tuple[LineItem, ...] = ()
```

(Given a default of `()`, this does not break any existing `Order(...)` construction call site
that doesn't pass `line_items` explicitly — confirm this by checking `backend/tests/` for
existing `Order(...)` constructions; they will keep working unmodified.)

- [ ] **Step 3: Extend the Shopify query and parsing in `backend/app/shopify/client.py`**

Find `ORDER_FIELDS`:

```python
ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    "displayFulfillmentStatus cancelledAt customerLocale "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone } billingAddress { phone }"
)
```

Replace with:

```python
ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    "displayFulfillmentStatus cancelledAt customerLocale "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone } billingAddress { phone } "
    # first: 50 is a query-time ceiling far above any realistic order size -- the display side
    # (order_tracking) shows every item with no further cap, by design (owner's explicit
    # choice: show all, don't summarize/truncate a customer's own order).
    "lineItems(first: 50) { edges { node { title quantity variant { title } "
    "originalUnitPriceSet { shopMoney { amount currencyCode } } } } }"
)
```

Find `_order_from_node`:

```python
def _order_from_node(node: dict[str, Any]) -> Order:
    total_node = (node.get("totalPriceSet") or {}).get("shopMoney")
    return Order(
        gid=str(node["id"]),
        name=str(node["name"]),
        email=node.get("email"),
        phone=node.get("phone"),
        shipping_phone=(node.get("shippingAddress") or {}).get("phone"),
        billing_phone=(node.get("billingAddress") or {}).get("phone"),
        financial_status=node.get("displayFinancialStatus"),
        fulfillment_status=node.get("displayFulfillmentStatus"),
        cancelled_at=node.get("cancelledAt"),
        tags=tuple(node.get("tags") or ()),
```

Read the rest of this function (it continues past what's shown above — find its closing
`)`/return) and add a new `line_items=_line_items_from_node(node),` keyword argument to the
`Order(...)` construction, placed anywhere among the other keyword arguments (Python doesn't
require a specific order for keyword arguments). Add this new helper function directly above
`_order_from_node`:

```python
def _line_items_from_node(node: dict[str, Any]) -> tuple[LineItem, ...]:
    edges = (node.get("lineItems") or {}).get("edges") or []
    items: list[LineItem] = []
    for edge in edges:
        item_node = edge.get("node") or {}
        price_node = (item_node.get("originalUnitPriceSet") or {}).get("shopMoney")
        variant = item_node.get("variant") or {}
        items.append(
            LineItem(
                title=str(item_node.get("title", "")),
                quantity=int(item_node.get("quantity") or 0),
                variant_title=variant.get("title"),
                price=(
                    Money(amount=price_node["amount"], currency=price_node["currencyCode"])
                    if price_node
                    else None
                ),
            )
        )
    return tuple(items)
```

Find the import line near the top of the file that imports from `app.shopify.models` (search
for `from app.shopify.models import`) and add `LineItem` to it if not already present (it will
need `Order`, `Money`, and now `LineItem` — check what's currently imported and add only what's
missing).

- [ ] **Step 4: Add unit tests for line-item parsing in `backend/tests/test_client_reads.py`**

Read the existing tests in this file for the pattern used to test `_order_from_node`-adjacent
behavior (search for `get_order` or `_order_from_node` tests) to match its exact fixture/mock
style. Add tests covering: an order with 2 line items (each with a variant and a price) parses
correctly into two `LineItem` entries with correct `title`/`quantity`/`variant_title`/`price`;
an order with an item that has no variant (`variant: null` in the response) parses with
`variant_title=None`; an order with zero line items (`lineItems: {edges: []}`) parses to
`line_items=()`; an order response missing the `lineItems` key entirely (defensive — some mocked
responses in existing tests won't have been updated to include it) still parses without raising,
defaulting to `line_items=()`.

- [ ] **Step 5: Extend `REVEAL_ALLOWED` and the default in `backend/app/admin/controls.py`**

Find:

```python
REVEAL_ALLOWED: tuple[str, ...] = ("order_number", "email", "status")
```

Replace with:

```python
# "items" reverses the earlier client decision that order items/amounts stay hidden (recorded
# in docs/superpowers/specs/2026-08-10-order-item-details-design.md) -- default-enabled per
# that decision, still independently admin-toggleable like every other value here.
REVEAL_ALLOWED: tuple[str, ...] = ("order_number", "email", "status", "items")
```

Find:

```python
    reveal_fields: list[str] = Field(
        default_factory=lambda: ["order_number", "email", "status"], max_length=3
    )
```

Replace with:

```python
    reveal_fields: list[str] = Field(
        default_factory=lambda: ["order_number", "email", "status", "items"], max_length=4
    )
```

- [ ] **Step 6: Update the admin panel UI (`index.html` + `admin.js`)**

In `backend/app/admin/static/index.html`, find:

```html
        <label><input type="checkbox" id="c-rv-status" style="width:auto" /> status</label>
```

Add immediately after it (still inside the same `<div class="field">`):

```html
        <label><input type="checkbox" id="c-rv-items" style="width:auto" /> items &amp; price</label>
```

In `backend/app/admin/static/admin.js`, find:

```javascript
  el("c-rv-status").checked = c.reveal_fields.includes("status");
```

Add immediately after it:

```javascript
  el("c-rv-items").checked = c.reveal_fields.includes("items");
```

Find:

```javascript
  if (el("c-rv-status").checked) reveal.push("status");
```

Add immediately after it:

```javascript
  if (el("c-rv-items").checked) reveal.push("items");
```

- [ ] **Step 7: Update `DEFAULT_REVEAL_FIELDS` in `backend/app/agents/base.py`**

Find:

```python
DEFAULT_REVEAL_FIELDS: tuple[str, ...] = ("order_number", "email", "status")
```

Replace with:

```python
DEFAULT_REVEAL_FIELDS: tuple[str, ...] = ("order_number", "email", "status", "items")
```

The existing test `test_default_reveal_fields_tracks_the_admin_allowed_set`
(`backend/tests/agents/test_base.py`) already asserts `DEFAULT_REVEAL_FIELDS == REVEAL_ALLOWED
== tuple(AdminControls().reveal_fields)` — no change needed to that test, it will continue to
pass and continue to catch any future drift between the two.

- [ ] **Step 8: Update `order_tracking.py` — rendering, COD framing, and prompt formatting guidance**

Find the imports at the top of `backend/app/agents/order_tracking.py` and add `LineItem` and
`Money` to the existing `from app.shopify.models import AuthorizedOrder` line if not already
present (it will need `AuthorizedOrder`, `LineItem`, `Money` — actually `Money` is only needed
if type-hinting the new helper function's parameter, which it is below).

Add this new helper function (placed near the top of the file, after the imports, before
`_is_cancel_eligible`):

```python
def _format_money(money: Money) -> str:
    """Render a price for a customer-facing WhatsApp reply.

    INR gets its symbol (this store's currency); anything else falls back to the raw currency
    code rather than guessing a symbol. A trailing ".00" is stripped for a cleaner look
    ("999.00" -> "₹999", not "₹999.00") -- non-".00" amounts are left exactly as Shopify sent
    them (no rounding).
    """
    amount = money.amount[:-3] if money.amount.endswith(".00") else money.amount
    if money.currency == "INR":
        return f"₹{amount}"
    return f"{amount} {money.currency}"


def _line_item_line(item: LineItem) -> str:
    variant = f" ({item.variant_title})" if item.variant_title else ""
    price = f" — {_format_money(item.price)}" if item.price else ""
    return f"- *{item.title}*{variant}{price}"
```

Find `_order_line`:

```python
def _order_line(order: AuthorizedOrder, reveal_fields: Sequence[str]) -> str:
    """Render one order using ONLY the fields the admin approved for disclosure.

    ``AdminControls.reveal_fields`` allows ``order_number`` / ``email`` / ``status``.
    ``order_number`` is the order name; ``status`` covers the whole payment/fulfillment/
    cancellation picture, cancel-eligibility included (it is derived from fulfillment and
    cancellation state, so it discloses nothing beyond them). ``email`` has never been rendered
    into this prompt, so there is nothing to gate for it. Withheld fields are omitted from the
    prompt entirely rather than merely "not to be mentioned" -- what the model never sees, it
    can never leak.
    """
    label = f"order {order.order.name}" if "order_number" in reveal_fields else "an order"
    if "status" not in reveal_fields:
        return f"- {label} (the store has not approved sharing its status over WhatsApp)"
    return (
        f"- {label}: payment status {order.order.financial_status or 'unknown'}, "
        f"fulfillment {order.order.fulfillment_status or 'not dispatched'}, "
        f"cancelled: {order.order.is_cancelled()}, "
        f"cancel eligible: {_is_cancel_eligible(order)}"
    )
```

Replace with:

```python
def _order_line(order: AuthorizedOrder, reveal_fields: Sequence[str]) -> str:
    """Render one order using ONLY the fields the admin approved for disclosure.

    ``AdminControls.reveal_fields`` allows ``order_number`` / ``email`` / ``status`` / ``items``.
    ``order_number`` is the order name; ``status`` covers the whole payment/fulfillment/
    cancellation picture, cancel-eligibility included (it is derived from fulfillment and
    cancellation state, so it discloses nothing beyond them); ``items`` adds each line item's
    product name, variant, and price. ``email`` has never been rendered into this prompt, so
    there is nothing to gate for it. Withheld fields are omitted from the prompt entirely rather
    than merely "not to be mentioned" -- what the model never sees, it can never leak.
    """
    label = f"order {order.order.name}" if "order_number" in reveal_fields else "an order"
    if "status" not in reveal_fields:
        return f"- {label} (the store has not approved sharing its status over WhatsApp)"
    cod_note = " (Cash on Delivery)" if order.order.is_cod() else ""
    lines = [
        f"- {label}: payment status {order.order.financial_status or 'unknown'}{cod_note}, "
        f"fulfillment {order.order.fulfillment_status or 'not dispatched'}, "
        f"cancelled: {order.order.is_cancelled()}, "
        f"cancel eligible: {_is_cancel_eligible(order)}"
    ]
    if "items" in reveal_fields and order.order.line_items:
        lines.extend(_line_item_line(item) for item in order.order.line_items)
    return "\n".join(lines)
```

Find `_SYSTEM_TEMPLATE`:

```python
_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}
{format_hint}
Store cancellation policy: orders can only be cancelled BEFORE they are dispatched. Once
dispatched, cancellation is not possible -- if the customer asks to cancel a dispatched order,
tell them clearly and do not offer a cancel option for it.

If the customer wants to cancel an order that IS still eligible, tell them you'll bring up a
Confirm/Cancel button for them to tap -- you never cancel anything yourself.

{contract}
"""
```

Replace with:

```python
_SYSTEM_TEMPLATE = """{personality}

You help customers with questions about THEIR OWN orders. Below is the customer's verified
order history for this WhatsApp number -- answer only from this data, never guess or invent
order details.

{order_context}
{format_hint}
Store cancellation policy: orders can only be cancelled BEFORE they are dispatched. Once
dispatched, cancellation is not possible -- if the customer asks to cancel a dispatched order,
tell them clearly and do not offer a cancel option for it.

If the customer wants to cancel an order that IS still eligible, tell them you'll bring up a
Confirm/Cancel button for them to tap -- you never cancel anything yourself.

If payment status is Pending on a Cash on Delivery order, explain that as normal -- payment is
simply collected on delivery, not something to worry about -- rather than sounding alarmed.

Format order-detail replies warmly and clearly, for example:

Hey there! 👋
Here are your order details:

*Order ID:* tavas1234
*Status:* Payment Pending (Cash on Delivery — paid on delivery) 💵
*Fulfillment:* Not yet dispatched 📦

*Items:*
- *Product Name* (Blue / M) — ₹999

Use bold (*like this*) for the order ID, status, and item names, a warm greeting, and light,
natural emoji use -- not on every line, and never more than the message needs.

{contract}
"""
```

- [ ] **Step 9: Add tests for the new rendering and formatting to `backend/tests/agents/test_order_tracking.py`**

Read the existing tests in this file first (especially any covering `reveal_fields`) to match
their exact fixture/order-construction style. Add tests covering:
- `_format_money`: `Money(amount="999.00", currency="INR")` → `"₹999"`; `Money(amount="499.50",
  currency="INR")` → `"₹499.50"` (not stripped, doesn't end in exactly ".00"); `Money(amount="10.00",
  currency="USD")` → `"10 USD"`.
- `_order_line` / `run()` with `"items"` in `reveal_fields` and an order with line items →
  rendered output (or the system prompt sent to the fake provider) contains each item's title
  and formatted price.
- `_order_line` / `run()` with `"items"` withheld from `reveal_fields` → item details are absent
  from the rendered output even when the order has line items.
- An order where `is_cod()` is true and financial status is `"Pending"` → the rendered status
  line contains `"(Cash on Delivery)"`.
- An order where `is_cod()` is false → no `"(Cash on Delivery)"` text appears.

- [ ] **Step 10: Add a controls test to `backend/tests/admin/test_controls.py`**

Read the existing tests in this file for the reveal_fields validation pattern (search for
`reveal_fields` or `REVEAL_ALLOWED`). Add one test confirming `AdminControls(reveal_fields=["items"])`
validates successfully (no longer rejected as an unknown value), and one confirming the default
`AdminControls().reveal_fields` includes `"items"`.

- [ ] **Step 11: Run the full backend test suite, ruff, and mypy**

Run: `cd backend && python -m pytest -q`
Expected: all tests PASS.

Run: `cd backend && python -m ruff check .`
Expected: All checks passed!

Run: `cd backend && python -m mypy app`
Expected: Success, no issues found.

- [ ] **Step 12: Compliance grep**

Run (from `backend/`):
```
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/shopify/client.py app/shopify/models.py app/admin/controls.py app/agents/base.py app/agents/order_tracking.py
```
Expected: EMPTY output.

- [ ] **Step 13: Commit**

```bash
git add backend/app/shopify/client.py backend/app/shopify/models.py backend/app/admin/controls.py backend/app/admin/static/index.html backend/app/admin/static/admin.js backend/app/agents/base.py backend/app/agents/order_tracking.py backend/tests/test_client_reads.py backend/tests/agents/test_order_tracking.py backend/tests/admin/test_controls.py
git commit -m "feat(agents): reveal order line items with price, frame COD-pending status, and format order-detail replies more attractively"
```
