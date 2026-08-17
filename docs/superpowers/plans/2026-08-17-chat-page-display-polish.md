# Chat Page Display Polish (sub-project 1d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A batch of display fixes to the admin chat page requested directly by the owner after using the page live: thread list shows name only, chat header shows name+number, bubble timestamps are 12h time-only, delivery-failure status text is red, and the order panel gets a customer-details section, a reordered layout, and a copy-friendly tracking link.

**Architecture:** One backend addition (customer address fields on `_order_summary`) plus frontend-only changes to `chats.js`/`chats.html`. No new endpoints, no schema changes.

**Explicitly OUT OF SCOPE:** WhatsApp-style delivery/read tick marks (grey single tick = sent, grey double = delivered, blue double = read). This requires new Meta message-status webhook handling and new storage that don't exist yet — a separate sub-project, to be scoped after this one ships.

## Global Constraints

- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- No schema/migration changes — `order.customer`'s address fields already exist on the `Customer` dataclass (`app/shopify/models.py`), already populated by `find_mirrored_orders_by_phone`.
- `backend/app/core/order_actions.py` is never touched.
- Every API-sourced/dynamic value in `chats.js` renders via `.textContent`/`createTextNode`, never `.innerHTML` with dynamic content — this page's established, security-reviewed convention.
- No new backend routes.

---

### Task 1: Backend — customer address fields on `_order_summary`

**Files:**
- Modify: `backend/app/admin/router.py:754-783` (`_order_summary`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `Order.customer: Customer | None`, with `Customer.first_name`, `last_name`, `address_line1`, `address_line2`, `city`, `state`, `postal_code`, `country` (all `str | None`, `app/shopify/models.py`).
- Produces: `_order_summary()`'s dict gains `"customer_name"`, `"address_line1"`, `"address_line2"`, `"city"`, `"state"`, `"postal_code"` (all `str | None`; `customer_name` is `first_name + " " + last_name` stripped/joined, `None` if no name parts). All `None` when `order.customer` is `None`.

- [ ] **Step 1: Write the failing test**

```python
def test_conversation_thread_order_summary_includes_customer_address(client: TestClient) -> None:
    login(client)
    normalized = "+919876500040"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/addr1",
        "name": "tavas800", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "500.00", "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/40",
            "first_name": "Neha", "last_name": "Verma",
        },
        "shipping_address": {
            "address1": "12 MG Road", "address2": "Flat 3B",
            "city": "Pune", "province": "Maharashtra", "zip": "411001",
        },
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    summary = resp.json()["orders"][0]
    assert summary["customer_name"] == "Neha Verma"
    assert summary["address_line1"] == "12 MG Road"
    assert summary["address_line2"] == "Flat 3B"
    assert summary["city"] == "Pune"
    assert summary["state"] == "Maharashtra"
    assert summary["postal_code"] == "411001"


def test_conversation_thread_order_summary_no_customer_has_null_address(client: TestClient) -> None:
    login(client)
    normalized = "+919876500041"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/noaddr",
        "name": "tavas801", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "500.00", "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    summary = resp.json()["orders"][0]
    assert summary["customer_name"] is None
    assert summary["address_line1"] is None
    assert summary["city"] is None
```

(Note: `order_from_webhook_payload`'s `_customer_from_order_payload` reads shipping address from the payload's top-level `shipping_address`, per `app/channels/shopify_orders.py:197-216` — confirm this by reading that function before writing the fixture; adapt the fixture shape if the current parser reads address fields differently than assumed here.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "customer_address" -v`
Expected: FAIL with `KeyError: 'customer_name'`

- [ ] **Step 3: Implement**

In `backend/app/admin/router.py`, modify `_order_summary()`:

```python
def _order_summary(order: Order) -> dict[str, object]:
    tracking_company = tracking_number = tracking_url = None
    if order.fulfillments:
        first = order.fulfillments[0]
        tracking_company = first.tracking_company
        tracking_number = first.tracking_number
        tracking_url = first.tracking_url
    customer_name = None
    address_line1 = address_line2 = city = state = postal_code = None
    if order.customer:
        name = " ".join(
            p for p in (order.customer.first_name, order.customer.last_name) if p
        ).strip()
        customer_name = name or None
        address_line1 = order.customer.address_line1
        address_line2 = order.customer.address_line2
        city = order.customer.city
        state = order.customer.state
        postal_code = order.customer.postal_code
    return {
        "order_name": order.name,
        "financial_status": order.financial_status,
        "fulfillment_status": order.fulfillment_status,
        "cancelled_at": order.cancelled_at,
        "is_cod": order.is_cod(),
        "total_amount": order.total.amount if order.total else None,
        "total_currency": order.total.currency if order.total else None,
        "tags": list(order.tags),
        "tracking_company": tracking_company,
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "customer_name": customer_name,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "line_items": [
            {
                "title": li.title,
                "quantity": li.quantity,
                "variant_title": li.variant_title,
                "price_amount": li.price.amount if li.price else None,
                "price_currency": li.price.currency if li.price else None,
            }
            for li in order.line_items
        ],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "customer_address" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): add customer name/address to the conversation thread's order summary"
```

---

### Task 2: Frontend — thread list name-only, chat header name+number

**Files:**
- Modify: `backend/app/admin/static/chats.js` (`renderThreadRows`, `loadThread`)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `t.customer_name`, `t.phone` (thread-list rows, already present); `allThreads` (module-level cache, already present) for `loadThread`'s header lookup.
- Produces: no new interfaces for later tasks.

- [ ] **Step 1: Write the failing smoke test**

```python
def test_chats_js_thread_list_shows_name_only(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    # The old "(" + phone + ")" suffix pattern must be gone from the thread-row rendering.
    assert 'customer_name + " (" + t.phone + ")"' not in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k thread_list_shows_name_only -v`
Expected: FAIL

- [ ] **Step 3: Implement — thread list name-only**

In `backend/app/admin/static/chats.js`, in `renderThreadRows()`, change:

```js
    phone.textContent = t.customer_name ? t.customer_name + " (" + t.phone + ")" : t.phone;
```
to:
```js
    phone.textContent = t.customer_name || t.phone;
```

- [ ] **Step 4: Implement — chat header shows name + number**

In `loadThread()`, change:
```js
  el("chat-header-phone").textContent = phone || "";
```
to look up the cached thread's name from `allThreads` (no new parameter needed on `loadThread`, no signature change, so every existing call site — click handler, refresh button, resume button, `pollTick`'s silent reload — keeps working unmodified):
```js
  const threadMeta = allThreads.find((t) => t.thread_id === threadId);
  const headerName = threadMeta && threadMeta.customer_name;
  el("chat-header-phone").textContent = headerName ? headerName + " (" + (phone || "") + ")" : (phone || "");
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k thread_list_shows_name_only -v`

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): thread list shows customer name only, chat header shows name and number"
```

---

### Task 3: Frontend — 12h bubble timestamps, red status text

**Files:**
- Modify: `backend/app/admin/static/chats.js` (`renderBubble`)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `entry.timestamp` (ISO-8601 string, already present), `entry.status` (already present).
- Produces: no new interfaces for later tasks.

- [ ] **Step 1: Write the failing smoke test**

```python
def test_chats_js_formats_bubble_timestamp_as_12h(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "formatBubbleTime" in js
    assert "hour12" in js or "toLocaleTimeString" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k formats_bubble_timestamp -v`
Expected: FAIL

- [ ] **Step 3: Implement — 12h timestamp**

In `backend/app/admin/static/chats.js`, add a new function above `renderBubble()`:

```js
function formatBubbleTime(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true }).toLowerCase();
}
```

Then in `renderBubble()`, change:
```js
  ts.textContent = entry.timestamp || "";
```
to:
```js
  ts.textContent = formatBubbleTime(entry.timestamp);
```

`toLocaleTimeString` with `hour12: true` renders e.g. `"5:20 PM"`; `.toLowerCase()` gives `"5:20 pm"`, matching the requested format (no leading zero on the hour, lowercase am/pm, no seconds/date/timezone).

- [ ] **Step 4: Implement — red status text**

In `renderBubble()`, the existing block:
```js
  if (entry.status && entry.status !== "sent") {
    const status = document.createElement("div");
    status.className = "bubble-status";
    if (entry.status === "failed" || entry.status === "undeliverable") {
      status.classList.add("bubble-status-error");
    }
    status.textContent = STATUS_LABELS[entry.status] || entry.status;
    div.appendChild(status);
  }
```
Add `"suppressed"` to the error-styled statuses (the owner specifically named the `"Not delivered — skipped by send policy"` text as needing to be red — that's the `suppressed` status's label in `STATUS_LABELS`), leaving `"queued"` as the only non-error (neutral/grey) non-`"sent"` status since it's a normal transitional state, not a problem:
```js
    if (entry.status === "failed" || entry.status === "undeliverable" || entry.status === "suppressed") {
      status.classList.add("bubble-status-error");
    }
```

Confirm `.bubble-status-error`'s CSS in `chats.html` is a red color (it already is, from an earlier task — `color: #dc2626` — read the current CSS to confirm before assuming, don't duplicate the rule if it's already correct).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k formats_bubble_timestamp -v`

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): 12h bubble timestamps, red text for not-delivered/failed sends"
```

---

### Task 4: Frontend — order panel redesign (customer details, section reorder, copyable tracking link)

**Files:**
- Modify: `backend/app/admin/static/chats.html` (new `#order-customer` container, heading text, CSS)
- Modify: `backend/app/admin/static/chats.js` (`renderOrderDetail`)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `order.customer_name`, `order.address_line1`, `order.address_line2`, `order.city`, `order.state`, `order.postal_code` (Task 1's new backend fields).
- Produces: element id `order-customer` (new), consumed only by this task.

- [ ] **Step 1: Write the failing smoke test**

```python
def test_chats_page_has_customer_details_container(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert 'id="order-customer"' in resp.text
    assert "Order Details" in resp.text


def test_chats_js_renders_customer_address_fields(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "customer_name" in js
    assert "address_line1" in js
    assert "postal_code" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "customer_details_container or renders_customer_address" -v`
Expected: FAIL

- [ ] **Step 3: Implement — HTML**

In `backend/app/admin/static/chats.html`, read the CURRENT `#order-panel` structure first (it has been modified by several earlier tasks today — do not assume exact current markup). Change the panel's heading from `<h3>Order</h3>` to `<h3>Order Details</h3>`, and add a new `<div id="order-customer"></div>` container positioned between `#order-number` and `#order-products` (so the render order becomes: order number → customer details → products → fulfillment fields, matching the requested Order Details → order number → customer details → product details → fulfillment details sequence — note `#order-detail`, the existing fulfillment-fields container, must be moved to render AFTER `#order-products` in the DOM if it currently comes before it; check current markup order and reorder if needed).

Add CSS for the new container, matching the existing `.product-row`/`#order-products h4` pattern:
```css
    #order-customer { margin-top: .6rem; border-top: 1px solid #e9edef; padding-top: .6rem; }
    #order-customer h4 { font-size: .78rem; color: #667781; margin-bottom: .4rem; }
```

Also add a "Fulfillment Details" `<h4>` heading before the existing `#order-detail` fields block (it currently has no heading of its own — the "Order" `<h3>` was serving as the only heading; now that `<h3>` says "Order Details" for the whole panel, the fulfillment fields need their own `<h4>` subheading, matching the "Products"/new "Customer Details" pattern). This can be a static `<h4>Fulfillment Details</h4>` element in the HTML directly above `#order-detail`, OR rendered from JS inside `renderOrderDetail()` — your choice, whichever fits the current markup more cleanly; if done in JS, make sure it's not re-appended on every render (check for the element first, or clear-and-rebuild the whole block consistently like the other sections already do).

- [ ] **Step 4: Implement — JS**

In `backend/app/admin/static/chats.js`, modify `renderOrderDetail()` to add customer-details rendering. Add this block (position it so the DOM insertion order matches: after setting `#order-number`, before building `#order-products`):

```js
  const customerContainer = el("order-customer");
  customerContainer.innerHTML = "";
  if (order.customer_name || order.address_line1 || order.city) {
    const heading = document.createElement("h4");
    heading.textContent = "Customer Details";
    customerContainer.appendChild(heading);
    const custFields = [
      ["Name", order.customer_name],
      ["Address", [order.address_line1, order.address_line2].filter(Boolean).join(", ")],
      ["City", order.city],
      ["State", order.state],
      ["Pincode", order.postal_code],
    ];
    for (const [label, value] of custFields) {
      if (!value) continue;
      const row = document.createElement("div");
      row.className = "order-field";
      row.innerHTML = "<span class='label'>" + label + ":</span> ";
      row.appendChild(document.createTextNode(value));
      customerContainer.appendChild(row);
    }
  }
```

For the tracking link (in the existing fulfillment-fields block), change the "Track shipment" link so its VISIBLE TEXT is the URL itself (so the owner can select/copy it directly), while remaining clickable:
```js
  if (order.tracking_url && /^https?:\/\//i.test(order.tracking_url)) {
    const link = document.createElement("a");
    link.href = order.tracking_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = order.tracking_url;
    link.style.fontSize = ".75rem";
    link.style.wordBreak = "break-all";
    container.appendChild(link);
  }
```
(`word-break: break-all` keeps a long URL from overflowing the narrow order panel — read the current CSS/layout first to confirm this doesn't conflict with an existing rule.)

Read the CURRENT full `renderOrderDetail()` function before editing (it has been modified by 2+ earlier tasks today) and integrate these additions into its actual current structure rather than assuming the exact current code shown in earlier plan files.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "customer_details_container or renders_customer_address" -v`

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): redesign order panel with customer details, reordered sections, copyable tracking link"
```

---

## Post-Implementation Notes

- No task touches `backend/app/core/order_actions.py` — verify empty diff before handoff to review.
- Manual browser verification is still required (same known gap as every prior frontend change on this page): confirm the reordered panel layout actually reads well, the 12h timestamp renders correctly across browsers, and the tracking link is genuinely copyable.
- Read/delivered tick marks are explicitly deferred to a separate future sub-project requiring new Meta message-status webhook handling — not part of this plan.
- Route to `code-reviewer` after all 4 tasks land, per this project's standard process.
