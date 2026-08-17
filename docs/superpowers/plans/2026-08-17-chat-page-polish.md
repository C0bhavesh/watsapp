# Chat Page Polish (sub-project 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the already-shipped dedicated WhatsApp-style chat page: show customer names, always show the order number, add product details, render clean bubble text with a separate delivery-status label, add thread search, and replace manual-only refresh with a 3-second diff-checked poll.

**Architecture:** Two existing admin endpoints (`GET /admin/conversations`, `GET /admin/conversations/{thread_id}`) gain response fields only — no new routes, no schema changes. `chats.js` gains rendering/search/poll logic consuming those new fields. Backend and frontend changes are independently testable and reviewable.

**Tech Stack:** Python 3.12 / FastAPI (backend), vanilla JS (frontend, no framework, matching this admin panel's existing convention), pytest + `TestClient` (backend tests), Python `TestClient`-based markup/JS-substring assertions (frontend smoke tests — no browser test runner in this repo).

## Global Constraints

- Admin-only surface — no new auth mechanism; every route keeps its existing `Depends(require_admin)`.
- No schema/migration changes — every field added is derived from data already fetched (`Order.line_items`, `Order.customer`, `outbound_messages.state`).
- `backend/app/core/order_actions.py` is never touched by any task in this plan.
- No new Shopify API calls, no new backend routes — both existing endpoints gain response fields only.
- Full type hints, `mypy`/`ruff` clean, no bare `except`, `async def` for I/O — matching `d:\bhvaesh_automation\.claude\rules\python\python-style.md`.
- Design source of truth: `docs/superpowers/specs/2026-08-17-chat-page-polish-design.md`.

---

### Task 1: Backend — `_order_summary()` gains `line_items`

**Files:**
- Modify: `backend/app/admin/router.py:683-702` (`_order_summary`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `Order.line_items: tuple[LineItem, ...]` (already on `app.shopify.models.Order`); `LineItem` fields `title: str`, `quantity: int`, `variant_title: str | None`, `price: Money | None` (`Money.amount: str`, `Money.currency: str`), `sku: str | None` (`app/shopify/models.py:12-18`).
- Produces: `_order_summary()`'s return dict gains a `"line_items"` key: `list[dict[str, object]]`, each entry `{"title": str, "quantity": int, "variant_title": str | None, "price_amount": str | None, "price_currency": str | None}`. Later tasks (frontend Task 3) read exactly this shape.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_views.py` (near the other `test_conversation_thread_*` tests, after `test_conversation_thread_includes_order_summary`):

```python
def test_conversation_thread_order_summary_includes_line_items(client: TestClient) -> None:
    login(client)
    normalized = "+919876500001"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/lineitems1",
        "name": "tavas600",
        "phone": normalized,
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "899.00",
        "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
        "line_items": [
            {"title": "Classic Kurta", "quantity": 2, "variant_title": "Blue / L",
             "price": "349.50", "sku": "KUR-BLU-L"},
            {"title": "Cotton Scarf", "quantity": 1, "variant_title": None,
             "price": None, "sku": None},
        ],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    line_items = resp.json()["orders"][0]["line_items"]
    assert len(line_items) == 2
    assert line_items[0] == {
        "title": "Classic Kurta", "quantity": 2, "variant_title": "Blue / L",
        "price_amount": "349.50", "price_currency": "INR",
    }
    assert line_items[1] == {
        "title": "Cotton Scarf", "quantity": 1, "variant_title": None,
        "price_amount": None, "price_currency": None,
    }


def test_conversation_thread_order_summary_empty_line_items(client: TestClient) -> None:
    login(client)
    normalized = "+919876500002"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/nolineitems",
        "name": "tavas601", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "100.00", "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.json()["orders"][0]["line_items"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k line_items -v`
Expected: FAIL with `KeyError: 'line_items'`

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

Run: `cd backend && python -m pytest tests/admin/test_views.py -k line_items -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures.

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): add line_items to conversation thread's order summary"
```

---

### Task 2: Backend — `list_conversations()` gains `customer_name` and `order_names`

**Files:**
- Modify: `backend/app/admin/router.py:619-680` (`list_conversations`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `c.ingest.find_mirrored_orders_by_phone(norm: str) -> list[Order]` (already exists, `app/store/base.py:250`, implemented in both `memory.py:241` and `postgres.py:446`); `Order.customer: Customer | None`, `Order.updated_at: str | None`, `Order.name: str`.
- Produces: each row in `GET /admin/conversations`'s response list gains `"customer_name": str | None` and `"order_names": list[str]`. Frontend Task 4 (thread list name display) and Task 5 (search) read exactly these two keys.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_views.py`:

```python
def test_conversations_list_includes_customer_name_and_order_names(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500010"
    older = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/listname-older",
        "name": "tavas700", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "100.00", "currency": "INR",
        "updated_at": "2026-08-01T00:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/1",
            "first_name": "Priya", "last_name": "Shah",
        },
    })
    newer = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/listname-newer",
        "name": "tavas701", "phone": normalized, "financial_status": "pending",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "200.00", "currency": "INR",
        "updated_at": "2026-08-15T00:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/1",
            "first_name": "Priya", "last_name": "Shah",
        },
    })
    assert older is not None and newer is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(older))
    asyncio.run(get_container().ingest.upsert_order_mirror(newer))
    _send_ai_message(normalized, "hi", "hello")  # so this phone is listed at all

    rows = client.get("/admin/conversations").json()

    row = next(r for r in rows if r["phone"] == normalized)
    assert row["customer_name"] == "Priya Shah"
    assert set(row["order_names"]) == {"tavas700", "tavas701"}


def test_conversations_list_no_orders_has_null_name_empty_order_names(
    client: TestClient,
) -> None:
    login(client)
    _send_ai_message("+919876500011", "hi", "hello")

    rows = client.get("/admin/conversations").json()

    row = next(r for r in rows if r["phone"] == "+919876500011")
    assert row["customer_name"] is None
    assert row["order_names"] == []


def test_conversations_list_order_with_no_customer_name_parts(client: TestClient) -> None:
    login(client)
    normalized = "+919876500012"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/noname",
        "name": "tavas702", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "50.00", "currency": "INR",
        "updated_at": "2026-08-17T00:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))
    _send_ai_message(normalized, "hi", "hello")

    rows = client.get("/admin/conversations").json()

    row = next(r for r in rows if r["phone"] == normalized)
    assert row["customer_name"] is None
    assert row["order_names"] == ["tavas702"]
```

Note: `order_from_webhook_payload` reads `customer.first_name`/`customer.last_name` via
`_customer_from_order_payload` (`app/channels/shopify_orders.py:197-216`), which requires
`customer.admin_graphql_api_id` to be a non-empty string or the whole `Customer` is dropped (`None`)
— the fixtures above include it for that reason.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "customer_name or order_names or noname" -v`
Expected: FAIL with `KeyError: 'customer_name'`

- [ ] **Step 3: Implement**

In `backend/app/admin/router.py`, modify the per-phone loop inside `list_conversations()`:

```python
    result: list[dict[str, object]] = []
    for norm in ordered_phones:
        thread_id = await c.conversations.get_or_create(norm)
        recent = await c.conversations.find_messages_by_user_id(norm, limit=1)
        preview = recent[-1].content[:120] if recent else ""
        last_active = recent[-1].created_at if recent else last_active_by_phone.get(norm)

        orders_for_phone = await c.ingest.find_mirrored_orders_by_phone(norm)
        orders_sorted_for_name = sorted(
            orders_for_phone, key=lambda o: str(o.updated_at or ""), reverse=True
        )
        customer_name: str | None = None
        if orders_sorted_for_name and orders_sorted_for_name[0].customer:
            cust = orders_sorted_for_name[0].customer
            name = " ".join(p for p in (cust.first_name, cust.last_name) if p).strip()
            customer_name = name or None
        order_names = [o.name for o in orders_for_phone]

        result.append(
            {"thread_id": thread_id, "phone": norm, "last_active_at": last_active,
             "preview": preview, "customer_name": customer_name, "order_names": order_names}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "customer_name or order_names or noname" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures. In particular re-run
`test_conversations_list_shows_recent_threads` and
`test_conversations_list_includes_outbound_only_customer` to confirm the two new keys don't break
their existing assertions (they don't assert on the full dict shape, only specific keys, so they
should pass unmodified).

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): add customer_name and order_names to the conversation thread list"
```

---

### Task 3: Backend — clean bubble text + separated delivery status

**Files:**
- Modify: `backend/app/admin/router.py:590-750` (new `_TEMPLATE_MESSAGE_TEMPLATES` + `_template_message_text`, and the `template_sent` entry-building loop inside `get_conversation_thread`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `row.payload_json: str` (JSON: `{"template": str, "language": str, "body_params": dict[str, str] | list[str], ...}` — the outbox envelope shape, already parsed the same way by `_template_sent_text`), `row.state: str` (`"queued" | "sent" | "suppressed" | "failed" | "undeliverable"`, from `OutboundView`/`find_outbound_by_phone`).
- Produces: each `template_sent` entry in `GET /admin/conversations/{thread_id}`'s `entries` list changes shape from `{"type", "timestamp", "text"}` to `{"type", "timestamp", "text", "status"}`, where `text` is now the reconstructed message (or the existing `_template_sent_text()` fallback) and `status` is `row.state` verbatim. `customer_message`/`ai_reply`/`button_tap` entries are unchanged (no `status` key). Frontend Task 5 reads `entry.status`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_views.py`:

```python
def test_conversation_thread_template_entry_has_clean_text_and_status(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500020"
    _seed_outbound_at(
        normalized, "gid://shopify/Order/cleanbubble",
        json.dumps({
            "template": "order_shipped", "language": "en",
            "body_params": ["Chiranjiv", "tavas4029", "Delhivery Surface",
                             "https://ad2ship.com/track-order/57143610408612"],
        }),
    )

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["status"] == "queued"
    assert "Chiranjiv" in entry["text"]
    assert "tavas4029" in entry["text"]
    assert "Delhivery Surface" in entry["text"]
    assert "https://ad2ship.com/track-order/57143610408612" in entry["text"]
    # The raw internal dump format ("template → param1, param2") must be gone.
    assert "→" not in entry["text"]
    assert "order_shipped" not in entry["text"]


def test_conversation_thread_unmapped_template_falls_back_cleanly(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500021"
    _seed_outbound_at(
        normalized, "gid://shopify/Order/unmapped",
        json.dumps({
            "template": "some_future_template", "language": "en",
            "body_params": ["a", "b"],
        }),
    )

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["status"] == "queued"
    # Falls back to the pre-existing "template → params" format, still correct/non-crashing.
    assert entry["text"] == "some_future_template → a, b"


def test_conversation_thread_template_param_count_mismatch_falls_back(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500022"
    # order_shipped's map expects 4 positional params; this payload supplies only 1.
    _seed_outbound_at(
        normalized, "gid://shopify/Order/mismatch",
        json.dumps({
            "template": "order_shipped", "language": "en",
            "body_params": ["OnlyOneParam"],
        }),
    )

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["text"] == "order_shipped → OnlyOneParam"


def test_conversation_thread_non_template_entries_have_no_status_key(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500023"
    raw_wa_id = normalized.lstrip("+")
    _send_ai_message(normalized, "where is my order", "let me check")
    _record_button_tap("gid://shopify/Order/nostatus", raw_wa_id)

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    for entry in resp.json()["entries"]:
        if entry["type"] != "template_sent":
            assert "status" not in entry
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "clean_text or unmapped_template or param_count_mismatch or no_status_key" -v`
Expected: FAIL — `entry["status"]` raises `KeyError`, and `entry["text"]` still contains the raw `→` dump.

- [ ] **Step 3: Implement**

In `backend/app/admin/router.py`, add below `_template_sent_text()` (after line 612):

```python
# Best-effort reconstruction of each approved template's approximate customer-facing wording, for
# admin-display purposes only (never sent anywhere) -- Meta's literal approved copy isn't stored
# in this codebase. Positional templates use a numbered-args format string; named templates use
# the exact payload_json body_params keys each one is built with (see
# docs/superpowers/specs/2026-08-15-order-type-routing-templated-replies-design.md and
# 2026-08-16-fulfillment-lifecycle-notifications-design.md for each template's param shape).
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


def _template_message_text(payload_json: str) -> str:
    """Reconstruct an approximate customer-facing message for a template_sent bubble.

    Falls back to _template_sent_text()'s raw "template -> params" format whenever the template
    name isn't mapped, the payload isn't a dict, or substitution fails (wrong param count/shape)
    -- this must never raise, a malformed row should degrade, not 500 the whole thread view.
    """
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return _template_sent_text(payload_json)
    if not isinstance(data, dict):
        return _template_sent_text(payload_json)
    template = data.get("template")
    fmt = _TEMPLATE_MESSAGE_TEMPLATES.get(str(template)) if isinstance(template, str) else None
    if fmt is None:
        return _template_sent_text(payload_json)
    body_params = data.get("body_params")
    try:
        if isinstance(body_params, dict):
            return fmt.format(**body_params)
        if isinstance(body_params, list):
            return fmt.format(*body_params)
    except (KeyError, IndexError):
        return _template_sent_text(payload_json)
    return _template_sent_text(payload_json)
```

Then modify the `template_sent` entry-building loop inside `get_conversation_thread()`
(`router.py:724-729`):

```python
    for row in await c.ingest.find_outbound_by_phone(user_id, limit=200):
        entries.append({
            "type": "template_sent",
            "timestamp": row.created_at,
            "text": _template_message_text(row.payload_json),
            "status": row.state,
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "clean_text or unmapped_template or param_count_mismatch or no_status_key" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Confirm the pre-existing template test still passes with the new text**

`test_conversation_thread_merges_all_three_sources` (`test_views.py:144-175`) asserts
`"cod_confirmation" in template_entry["text"]` on a `cod_confirmation` payload with
`body_params={"customer_name": "Suman", "order_id": "tavaschat"}`. `cod_confirmation`'s map entry
uses `{product_name}`/`{product_color}`/`{product_size}`/`{product_amount}` placeholders that
aren't present in that fixture's `body_params`, so `fmt.format(**body_params)` raises `KeyError`
and correctly falls back to `_template_sent_text()`'s output (`"cod_confirmation → Suman,
tavaschat"`), which still contains the substring `"cod_confirmation"` — the existing assertion
keeps passing unmodified. Run it explicitly to confirm:

Run: `cd backend && python -m pytest tests/admin/test_views.py -k merges_all_three_sources -v`
Expected: PASS

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): reconstruct approximate template wording, separate send status from text"
```

---

### Task 4: Frontend — order panel shows order number always + product list

**Files:**
- Modify: `backend/app/admin/static/chats.html` (add a products container + order-number header element)
- Modify: `backend/app/admin/static/chats.js` (`renderOrderDetail`)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `order.order_name: string`, `order.line_items: Array<{title, quantity, variant_title, price_amount, price_currency}>` (from Task 1's backend response shape).
- Produces: element ids `order-number` and `order-products` (new), for Task 5/6 and the smoke test to reference.

- [ ] **Step 1: Write the failing smoke test**

Add to `backend/tests/admin/test_static_mount.py` (matching this file's existing convention of
asserting served content contains expected markup/ids — read the file first to match exact
helper/style):

```python
def test_chats_page_has_order_number_and_products_containers(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="order-number"' in resp.text
    assert 'id="order-products"' in resp.text


def test_chats_js_renders_order_number_and_line_items(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "order-number" in resp.text
    assert "order-products" in resp.text
    assert "line_items" in resp.text
```

(No `login(client)` call — confirmed by reading `test_static_mount.py`'s existing tests
(`test_chats_page_served`, `test_chats_js_served_and_calls_the_conversations_api`, etc.): every
`/admin/ui/*` static-file test in this file hits the endpoint directly with no login step, since
static files are served unauthenticated by the `StaticFiles` mount — only the JSON API routes are
behind `require_admin`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k order_number -v`
Expected: FAIL — `order-number`/`order-products` not present in the served files yet.

- [ ] **Step 3: Implement — HTML**

In `backend/app/admin/static/chats.html`, replace the `#order-panel` div's contents:

```html
    <div id="order-panel">
      <h3>Order</h3>
      <div id="order-empty">No orders for this customer</div>
      <select id="order-select" style="display:none"></select>
      <div id="order-number"></div>
      <div id="order-detail"></div>
      <div id="order-products"></div>
    </div>
```

Add corresponding CSS near the existing `.order-field`/`#order-empty` rules:

```css
    #order-number { font-size: .95rem; font-weight: 700; margin-bottom: .5rem; }
    #order-products { margin-top: .6rem; border-top: 1px solid #e9edef; padding-top: .6rem; }
    #order-products h4 { font-size: .78rem; color: #667781; margin-bottom: .4rem; }
    .product-row { font-size: .78rem; color: #3b4a54; margin-bottom: .3rem; }
```

- [ ] **Step 4: Implement — JS**

In `backend/app/admin/static/chats.js`, modify `renderOrderDetail()`:

```js
function renderOrderDetail(order) {
  el("order-number").textContent = order.order_name;
  const container = el("order-detail");
  container.innerHTML = "";
  const fields = [
    ["Status", order.financial_status || "-"],
    ["Fulfillment", order.fulfillment_status || "not dispatched"],
    ["Cancelled", order.cancelled_at ? "yes" : "no"],
    ["Payment", order.is_cod ? "COD" : "prepaid"],
    ["Amount", order.total_amount ? order.total_amount + " " + (order.total_currency || "") : "-"],
    ["Tags", order.tags && order.tags.length ? order.tags.join(", ") : "-"],
    ["Courier", order.tracking_company || "-"],
    ["Tracking #", order.tracking_number || "-"],
  ];
  for (const [label, value] of fields) {
    const row = document.createElement("div");
    row.className = "order-field";
    row.innerHTML = "<span class='label'>" + label + ":</span> ";
    row.appendChild(document.createTextNode(value));
    container.appendChild(row);
  }
  if (order.tracking_url && /^https?:\/\//i.test(order.tracking_url)) {
    const link = document.createElement("a");
    link.href = order.tracking_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Track shipment";
    link.style.fontSize = ".8rem";
    container.appendChild(link);
  }

  const productsContainer = el("order-products");
  productsContainer.innerHTML = "";
  const items = order.line_items || [];
  if (items.length) {
    const heading = document.createElement("h4");
    heading.textContent = "Products";
    productsContainer.appendChild(heading);
    for (const li of items) {
      const row = document.createElement("div");
      row.className = "product-row";
      let text = li.quantity + "\u00d7 " + li.title;
      if (li.variant_title) text += " (" + li.variant_title + ")";
      if (li.price_amount) text += " \u2014 " + li.price_amount + " " + (li.price_currency || "");
      row.textContent = text;
      productsContainer.appendChild(row);
    }
  }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k order_number -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): always show order number, add product list to chat page order panel"
```

---

### Task 5: Frontend — bubble delivery-status label

**Files:**
- Modify: `backend/app/admin/static/chats.js` (`renderBubble`)
- Modify: `backend/app/admin/static/chats.html` (CSS for the new status label)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `entry.status: string | undefined` (from Task 3's backend response shape — present only on `template_sent` entries).
- Produces: a `.bubble-status` element under any `template_sent` bubble whose `status !== "sent"`.

- [ ] **Step 1: Write the failing smoke test**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_js_renders_bubble_status_label(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "entry.status" in resp.text
    assert "bubble-status" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k bubble_status -v`
Expected: FAIL

- [ ] **Step 3: Implement — JS**

In `backend/app/admin/static/chats.js`, modify `renderBubble()`:

```js
const STATUS_LABELS = {
  suppressed: "Not delivered — skipped by send policy",
  failed: "Failed to send",
  undeliverable: "Undeliverable",
  queued: "Queued",
};

function renderBubble(entry) {
  const div = document.createElement("div");
  const side = entry.type === "customer_message" ? "bubble-in" : "bubble-out";
  div.className = "bubble " + side;
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent = entry.type.replace("_", " ");
  const text = document.createElement("div");
  text.textContent = entry.text;
  const ts = document.createElement("div");
  ts.className = "bubble-ts";
  ts.textContent = entry.timestamp || "";
  div.appendChild(label);
  div.appendChild(text);
  if (entry.status && entry.status !== "sent") {
    const status = document.createElement("div");
    status.className = "bubble-status";
    if (entry.status === "failed" || entry.status === "undeliverable") {
      status.classList.add("bubble-status-error");
    }
    status.textContent = STATUS_LABELS[entry.status] || entry.status;
    div.appendChild(status);
  }
  div.appendChild(ts);
  return div;
}
```

- [ ] **Step 4: Implement — CSS**

In `backend/app/admin/static/chats.html`, add near `.bubble-ts`:

```css
    .bubble-status { font-size: .68rem; color: #8696a0; margin-top: .15rem; }
    .bubble-status-error { color: #dc2626; }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k bubble_status -v`
Expected: PASS

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): show a small delivery-status label under non-delivered template bubbles"
```

---

### Task 6: Frontend — thread list customer name + client-side search

**Files:**
- Modify: `backend/app/admin/static/chats.html` (add a search input, thread-row name display)
- Modify: `backend/app/admin/static/chats.js` (`loadThreadList`, new `filterThreads`, name rendering)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `t.customer_name: string | null`, `t.order_names: string[]` (from Task 2's backend response shape).
- Produces: element id `thread-search` (new input); a module-level `allThreads` array (raw fetched list, so filtering re-renders from cache without a new request) — Task 7's poll reuses this same variable.

- [ ] **Step 1: Write the failing smoke test**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_page_has_search_input(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="thread-search"' in resp.text


def test_chats_js_filters_threads_by_name_phone_and_order_number(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "customer_name" in resp.text
    assert "order_names" in resp.text
    assert "normalize_order_name" in resp.text or "tavas" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "search_input or filters_threads" -v`
Expected: FAIL

- [ ] **Step 3: Implement — HTML**

In `backend/app/admin/static/chats.html`, add a search input inside `#thread-list-pane`, between
the header and the list:

```html
      <div id="thread-list-header">
        <span>Chats</span>
        <button id="refresh-btn">Refresh</button>
      </div>
      <input id="thread-search" type="text" placeholder="Search name, phone, or order #" />
      <div id="thread-list"></div>
```

CSS:

```css
    #thread-search { border: none; border-bottom: 1px solid #e9edef; padding: .6rem 1rem;
      font-size: .82rem; outline: none; }
```

- [ ] **Step 4: Implement — JS**

In `backend/app/admin/static/chats.js`, replace `loadThreadList()` and add filtering:

```js
let allThreads = [];

function normalizeOrderQuery(query) {
  // Mirrors app/shopify/models.py::normalize_order_name's isdigit() branch: bare digits like
  // "3589" should match the store's "tavas3589" order-name format.
  return /^\d+$/.test(query) ? "tavas" + query : query;
}

function threadMatchesQuery(thread, query) {
  if (!query) return true;
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if ((thread.phone || "").toLowerCase().includes(q)) return true;
  if ((thread.customer_name || "").toLowerCase().includes(q)) return true;
  const orderNames = (thread.order_names || []).map((n) => n.toLowerCase());
  if (orderNames.some((n) => n.includes(q))) return true;
  const normalized = normalizeOrderQuery(q);
  return orderNames.some((n) => n.includes(normalized));
}

function renderThreadRows(threads) {
  const list = el("thread-list");
  list.innerHTML = "";
  for (const t of threads) {
    const row = document.createElement("div");
    row.className = "thread-row";
    row.dataset.threadId = String(t.thread_id);
    if (t.thread_id === currentThreadId) row.classList.add("active");
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = t.last_active_at ? t.last_active_at.slice(0, 10) : "";
    const phone = document.createElement("div");
    phone.className = "phone";
    phone.textContent = t.customer_name ? t.customer_name + " (" + t.phone + ")" : t.phone;
    phone.appendChild(ts);
    const preview = document.createElement("div");
    preview.className = "preview";
    preview.textContent = t.preview || "";
    row.appendChild(phone);
    row.appendChild(preview);
    row.addEventListener("click", () => loadThread(t.thread_id, t.phone));
    list.appendChild(row);
  }
}

async function loadThreadList() {
  try {
    allThreads = await api("/admin/conversations");
    renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  }
}

el("thread-search").addEventListener("input", () => {
  renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
});
```

Remove the old `loadThreadList()` body's inline row-building loop (now in `renderThreadRows`) —
the function list in the file should end up with `normalizeOrderQuery`, `threadMatchesQuery`,
`renderThreadRows`, and a slimmer `loadThreadList` replacing the single old function.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "search_input or filters_threads" -v`
Expected: PASS

- [ ] **Step 6: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): show customer name in the chat thread list, add client-side search"
```

---

### Task 7: Frontend — 3-second diff-checked poll

**Files:**
- Modify: `backend/app/admin/static/chats.js` (new `pollTick`, `setInterval`, snapshot tracking)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `allThreads` (Task 6), `currentThreadId`/`currentPhone` (existing module-level state), `loadThread`/`loadThreadList` (existing functions, reused for the actual re-render step so poll and manual refresh share one code path).
- Produces: nothing consumed by a later task — this is the final task in the plan.

- [ ] **Step 1: Write the failing smoke test**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_js_polls_every_three_seconds(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "setInterval" in resp.text
    assert "3000" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k polls_every_three -v`
Expected: FAIL

- [ ] **Step 3: Implement**

In `backend/app/admin/static/chats.js`, add near the bottom of the file (after the existing
`refresh-btn` listener, before the final `loadThreadList()` call):

```js
let listSnapshotKey = "";
let threadSnapshotKey = "";

function threadListKey(threads) {
  return threads.map((t) => t.thread_id + ":" + (t.last_active_at || "")).join("|");
}

function threadEntriesKey(entries) {
  if (!entries.length) return "empty";
  const last = entries[entries.length - 1];
  return entries.length + ":" + (last.timestamp || "");
}

async function pollTick() {
  try {
    const threads = await api("/admin/conversations");
    const nextListKey = threadListKey(threads);
    if (nextListKey !== listSnapshotKey) {
      allThreads = threads;
      listSnapshotKey = nextListKey;
      renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
    }
    if (currentThreadId !== null) {
      const data = await api("/admin/conversations/" + encodeURIComponent(currentThreadId));
      const nextThreadKey = threadEntriesKey(data.entries);
      if (nextThreadKey !== threadSnapshotKey) {
        threadSnapshotKey = nextThreadKey;
        await loadThread(currentThreadId, currentPhone);
      }
    }
  } catch (e) {
    // Silent -- a transient poll failure shouldn't overwrite list-status/thread-status, which are
    // reserved for explicit user-triggered load errors.
  }
}

setInterval(pollTick, 3000);
```

`loadThread()` already sets `threadSnapshotKey`-relevant data via `data.entries` internally on
every call it makes (including the manual refresh button's), but does not itself update
`threadSnapshotKey` — add one line at the end of the existing `loadThread()` function's try block
(after `renderOrderPanel(data.orders);`) so a manual refresh or thread switch keeps the poll's
snapshot in sync and doesn't immediately re-trigger itself on the next tick:

```js
    renderOrderPanel(data.orders);
    threadSnapshotKey = threadEntriesKey(data.entries);
    el("thread-status").textContent = "";
```

Similarly, update the `refresh-btn` click handler and the initial `loadThreadList()` call's effect
on `listSnapshotKey`: since `loadThreadList()` already reassigns `allThreads`, add
`listSnapshotKey = threadListKey(allThreads);` at the end of `loadThreadList()`'s try block (after
the `renderThreadRows(...)` call) so the poll doesn't immediately re-render a list it just rendered
itself.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k polls_every_three -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite + lint**

Run: `cd backend && python -m pytest -q && python -m ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): poll every 3s with diff-check so new activity appears without a manual refresh"
```

---

## Post-Implementation Notes

- No task in this plan touches `backend/app/core/order_actions.py` — verify with
  `git diff <base-commit> HEAD -- backend/app/core/order_actions.py` returning empty before
  handing off to review.
- Manual browser verification is still required after this plan (same gap noted for the original
  dedicated chat page): the diff-checked poll's actual "no flicker when idle" behavior, the search
  box's live filtering, and the product-list/order-number rendering all need a real owner pass —
  no browser test runner exists in this repo.
- Route to `code-reviewer` then `security-reviewer` after all 7 tasks land, per this project's
  standard post-feature process (`.claude/rules/common/agents.md`).
