# Admin Emoji Picker + Template Resend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an emoji picker and a template-resend dialog to the admin chat page's reply bar, matching the mockup (two icons to the left of the text input).

**Architecture:** A new template registry module (`template_catalog.py`) is the single source of truth for what templates exist and how their fields map to order data — both new backend endpoints and the frontend dialog are generic over it, so a future template is a one-entry addition. Template sends reuse the existing `send_template`/`enqueue_outbound`/`send_inline_outbound` pipeline exactly as `shopify_webhook.py`'s automatic fulfillment notifications already do.

**Tech Stack:** Python 3.12 / FastAPI (backend), vanilla JS (frontend), pytest (backend tests), Python `TestClient`-based markup/JS-substring assertions (frontend smoke tests — no browser test runner in this repo).

## Global Constraints

- Admin-only surface — `require_admin` unchanged, no new auth mechanism.
- `backend/app/core/order_actions.py` is never touched.
- The order `gid` used for `cod_confirmation`'s Confirm/Cancel button payloads is ALWAYS resolved server-side from the thread's own mirrored orders (`find_mirrored_orders_by_phone`) — never accepted from the request body. The request body carries only the human-readable `order_name` plus admin-edited field values.
- The template-resend endpoint (unlike the manual-reply feature's free text) goes through the EXISTING `enqueue_outbound` + `send_inline_outbound` pipeline unchanged, and therefore RESPECTS `send_decision`/`send_mode`/`allowlist_phones` — do NOT bypass the kill switch here. `send_inline_outbound` internally checks `send_mode` and leaves the row queued (for the backstop `outbox_drain` job) when it's `"off"`; do not attempt to call `send_template` directly instead, which would risk a double-send once the backstop job also picks up the same row.
- All four templates are pinned to `language="en"` — a pre-existing, deliberate constraint (Meta-approved in `en` only on this WABA). Do not add language selection.
- Design source of truth: `docs/superpowers/specs/2026-08-19-admin-emoji-template-picker-design.md`.
- This feature sends real outbound WhatsApp messages with mutation-adjacent button payloads — a `security-reviewer` pass is required after `code-reviewer`.

---

### Task 1: Template catalog + reusable default-value resolver

**Files:**
- Create: `backend/app/admin/template_catalog.py`
- Modify: `backend/app/channels/shopify_orders.py` (rename `_split_variant_options` → public `split_variant_options`; update its one internal call site)
- Test: `backend/tests/admin/test_template_catalog.py`

**Interfaces:**
- Consumes: `Order`/`LineItem` (`app/shopify/models.py`, unchanged), `EMPTY_PARAM_PLACEHOLDER` (`app/channels/copy.py`, unchanged).
- Produces: `TemplateField`, `TemplateDef`, `TEMPLATE_CATALOG: dict[str, TemplateDef]`, and `resolve_template_defaults(order: Order) -> dict[str, str]` — all in `template_catalog.py`, consumed by Task 2's endpoints.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/admin/test_template_catalog.py`:

```python
from app.channels.shopify_orders import split_variant_options
from app.shopify.models import LineItem, Money, Order
from app.admin.template_catalog import TEMPLATE_CATALOG, resolve_template_defaults


def _order(**overrides: object) -> Order:
    defaults: dict[str, object] = dict(
        gid="gid://shopify/Order/1",
        name="tavas4142",
        email=None,
        phone=None,
        shipping_phone="+919664290413",
        billing_phone=None,
        financial_status="pending",
        fulfillment_status=None,
        cancelled_at=None,
        tags=(),
        payment_gateway_names=("Cash on Delivery (COD)",),
        total=Money(amount="999.00", currency="INR"),
        customer_locale="en",
        line_items=(
            LineItem(
                title="Black Premium Cotton Co-Ord Set",
                quantity=1,
                variant_title="Black / XL",
                price=Money(amount="899.00", currency="INR"),
            ),
        ),
        customer=None,
        fulfillments=(),
    )
    defaults.update(overrides)
    return Order(**defaults)  # type: ignore[arg-type]


def test_catalog_has_all_four_known_templates() -> None:
    assert set(TEMPLATE_CATALOG) == {
        "cod_confirmation", "prepaid_order", "order_shipped", "order_delivered",
    }


def test_cod_confirmation_has_confirm_cancel_buttons() -> None:
    assert TEMPLATE_CATALOG["cod_confirmation"].has_confirm_cancel_buttons is True


def test_prepaid_order_has_no_buttons() -> None:
    assert TEMPLATE_CATALOG["prepaid_order"].has_confirm_cancel_buttons is False


def test_shipped_and_delivered_have_no_buttons() -> None:
    assert TEMPLATE_CATALOG["order_shipped"].has_confirm_cancel_buttons is False
    assert TEMPLATE_CATALOG["order_delivered"].has_confirm_cancel_buttons is False


def test_split_variant_options_is_public_and_correct() -> None:
    assert split_variant_options("Black / XL") == ("Black", "XL")
    assert split_variant_options(None) == (None, None)


def test_resolve_template_defaults_derives_product_fields_from_first_line_item() -> None:
    defaults = resolve_template_defaults(_order())
    assert defaults["order_name"] == "tavas4142"
    assert defaults["product_name"] == "Black Premium Cotton Co-Ord Set"
    assert defaults["product_color"] == "Black"
    assert defaults["product_size"] == "XL"
    assert defaults["product_amount"] == "899.00"


def test_resolve_template_defaults_tracking_link_prefers_url_over_number() -> None:
    order = _order(
        fulfillments=(
            __import__("app.shopify.models", fromlist=["Fulfillment"]).Fulfillment(
                gid="gid://shopify/Fulfillment/1",
                tracking_company="Delhivery",
                tracking_number="AB123",
                tracking_url="https://track.example/AB123",
            ),
        ),
    )
    defaults = resolve_template_defaults(order)
    assert defaults["tracking_company"] == "Delhivery"
    assert defaults["tracking_link"] == "https://track.example/AB123"


def test_resolve_template_defaults_tracking_link_falls_back_to_number() -> None:
    order = _order(
        fulfillments=(
            __import__("app.shopify.models", fromlist=["Fulfillment"]).Fulfillment(
                gid="gid://shopify/Fulfillment/1",
                tracking_company="Delhivery",
                tracking_number="AB123",
                tracking_url=None,
            ),
        ),
    )
    assert resolve_template_defaults(order)["tracking_link"] == "AB123"


def test_resolve_template_defaults_blank_when_no_line_items_or_fulfillments() -> None:
    order = _order(line_items=(), fulfillments=())
    defaults = resolve_template_defaults(order)
    assert defaults["product_name"] == ""
    assert defaults["tracking_company"] == ""
    assert defaults["tracking_link"] == ""
```

Check `Fulfillment`'s actual field names in `backend/app/shopify/models.py` before pasting (`gid`, `tracking_company`, `tracking_number`, `tracking_url` are assumed here, matching their use at `shopify_webhook.py:766-768`) — adapt if the real dataclass differs. Same for `LineItem`/`Money` — confirm their exact field names by reading `backend/app/shopify/models.py:1-70` first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_template_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.admin.template_catalog'` and `ImportError: cannot import name 'split_variant_options'`.

- [ ] **Step 3: Make `split_variant_options` public**

In `backend/app/channels/shopify_orders.py`, rename `_split_variant_options` to `split_variant_options` (drop the leading underscore) at its definition and its one call site (inside `parse_order_created`). Do not change its body or docstring — only the name, so `template_catalog.py` (Task 1) can import it without needing its own copy (DRY, same rationale as the auto-retry feature's earlier `_TemplatePayload` → `TemplatePayload` rename).

- [ ] **Step 4: Create `template_catalog.py`**

```python
"""Registry of the store's approved WhatsApp templates, for the admin manual-resend dialog.

Adding a template Meta approves in the future is a ONE-ENTRY addition to TEMPLATE_CATALOG below
-- no frontend change and no send_template change is needed, since both are already generic over
this registry (see docs/superpowers/specs/2026-08-19-admin-emoji-template-picker-design.md).

Mirrors the exact template shapes app/channels/shopify_webhook.py already sends automatically:
cod_confirmation/prepaid_order use NAMED body params, order_shipped/order_delivered use
POSITIONAL body params (see send_template's body_params: Mapping[str, str] | Sequence[str]).
"""

from dataclasses import dataclass

from app.channels.shopify_orders import split_variant_options
from app.shopify.models import Order


@dataclass(frozen=True)
class TemplateField:
    key: str
    label: str
    default_from: str  # key into the dict resolve_template_defaults() returns; "" = no default


@dataclass(frozen=True)
class TemplateDef:
    label: str
    language: str
    param_style: str  # "named" or "positional"
    fields: tuple[TemplateField, ...]
    has_confirm_cancel_buttons: bool = False
    supports_image_header: bool = False


_CONFIRMATION_FIELDS = (
    TemplateField("customer_name", "Customer name", "customer_name"),
    TemplateField("order_id", "Order ID", "order_name"),
    TemplateField("product_name", "Product name", "product_name"),
    TemplateField("product_color", "Color", "product_color"),
    TemplateField("product_size", "Size", "product_size"),
    TemplateField("product_amount", "Amount", "product_amount"),
)

TEMPLATE_CATALOG: dict[str, TemplateDef] = {
    "cod_confirmation": TemplateDef(
        label="COD Confirmation", language="en", param_style="named",
        fields=_CONFIRMATION_FIELDS,
        has_confirm_cancel_buttons=True, supports_image_header=True,
    ),
    "prepaid_order": TemplateDef(
        label="Prepaid Order Confirmation", language="en", param_style="named",
        fields=_CONFIRMATION_FIELDS, supports_image_header=True,
    ),
    "order_shipped": TemplateDef(
        label="Shipped Notice", language="en", param_style="positional",
        fields=(
            TemplateField("name", "Customer name", "customer_name"),
            TemplateField("order_name", "Order #", "order_name"),
            TemplateField("tracking_company", "Courier", "tracking_company"),
            TemplateField("tracking_link", "Tracking link/number", "tracking_link"),
        ),
    ),
    "order_delivered": TemplateDef(
        label="Delivered Notice", language="en", param_style="positional",
        fields=(
            TemplateField("name", "Customer name", "customer_name"),
            TemplateField("order_name", "Order #", "order_name"),
        ),
    ),
}


def resolve_template_defaults(order: Order) -> dict[str, str]:
    """Derive every TemplateField.default_from key any catalog entry might reference, from ONE
    order. Always returns every key with a string value ("" when nothing is available) so a
    caller never needs a presence check -- this mirrors shopify_webhook.py's own EMPTY_PARAM_
    PLACEHOLDER-style "never leave a template param structurally missing" posture, except here an
    empty string (not the placeholder) is correct: the admin sees a blank, editable field, not a
    literal "-" they'd have to notice and clear.
    """
    first_item = order.line_items[0] if order.line_items else None
    product_color, product_size = (
        split_variant_options(first_item.variant_title) if first_item else (None, None)
    )
    first_fulfillment = order.fulfillments[0] if order.fulfillments else None
    tracking_link = ""
    tracking_company = ""
    if first_fulfillment is not None:
        tracking_company = first_fulfillment.tracking_company or ""
        tracking_link = first_fulfillment.tracking_url or first_fulfillment.tracking_number or ""
    customer_name = ""
    if order.customer is not None:
        customer_name = " ".join(
            p for p in (order.customer.first_name, order.customer.last_name) if p
        ).strip()
    return {
        "customer_name": customer_name,
        "order_name": order.name,
        "product_name": (first_item.title if first_item else "") or "",
        "product_color": product_color or "",
        "product_size": product_size or "",
        "product_amount": (first_item.price.amount if first_item and first_item.price else "") or "",
        "tracking_company": tracking_company,
        "tracking_link": tracking_link,
    }
```

Before pasting, confirm `Customer`'s field names (`first_name`/`last_name`, used above) and `LineItem.price`'s type (`Money | None`, with `.amount`) against `backend/app/shopify/models.py` — adapt if they differ from what `_order_summary` in `router.py` already assumes (that function is the existing precedent for this exact shape of field access).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_template_catalog.py -v`
Expected: PASS (9 tests).

- [ ] **Step 6: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures. Confirm nothing outside `shopify_orders.py`'s one renamed identifier and its one call site changed behavior.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/template_catalog.py backend/app/channels/shopify_orders.py backend/tests/admin/test_template_catalog.py
git commit -m "feat(admin): add template catalog + default-value resolver for manual resends"
```

---

### Task 2: Backend endpoints — list templates, send a template

**Files:**
- Modify: `backend/app/jobs/outbox_drain.py` (register a new dedupe-key prefix for admin resends, in the fulfillment-style family with no mapping-status side effect)
- Modify: `backend/app/admin/router.py` (two new endpoints, near `send_manual_reply`)
- Test: `backend/tests/admin/test_views.py`

**Interfaces:**
- Consumes: `TEMPLATE_CATALOG`, `resolve_template_defaults` (Task 1). `IngestStore.find_mirrored_orders_by_phone(user_id) -> list[Order]` (existing, already used by `get_conversation_thread`). `IngestStore.enqueue_outbound(OutboundDraft) -> int | None` and `send_inline_outbound(c, outbound_id, timeout=...) -> str | None` (existing, `app/jobs/outbox_drain.py` — returns one of `OUTCOME_SENT`/`OUTCOME_SUPPRESSED`/`OUTCOME_UNDELIVERABLE`/`OUTCOME_FAILED`/`OUTCOME_RETRY`, or `None` when nothing was attempted at all — e.g. `send_mode == "off"`, no WhatsApp config, or the row's claim was lost).
- Produces: `GET /admin/conversations/{thread_id}/templates` → `{"orders": [{"order_name": str, "templates": [{"key": str, "label": str, "has_buttons": bool, "fields": [{"key": str, "label": str, "value": str}, ...]}, ...]}, ...]}`. `POST /admin/conversations/{thread_id}/templates` (body `{"order_name": str, "template": str, "values": dict[str, str]}`) → `{"ok": true}` (200, including when the send was merely QUEUED for the backstop drain rather than sent immediately — enqueuing successfully is not a failure) or `{"ok": false, "error": str}` (non-2xx, only for a genuine terminal failure: unknown thread/template/order, or `send_inline_outbound` reporting `OUTCOME_FAILED`/`OUTCOME_UNDELIVERABLE`).

**A real bug caught while writing this plan, fixed here — read before implementing:** `send_one_outbound` (`app/jobs/outbox_drain.py`) rejects any `dedupe_key` whose prefix isn't in its own internal `_DEDUPE_PREFIXES` whitelist (`_gid_from_dedupe_key`, `outbox_drain.py:106-121`) — an unrecognized prefix is treated as a corrupt/terminal `bad_dedupe_key` failure, so a naively-chosen `dedupe_key` string would make EVERY admin template resend silently fail. `_gid_from_dedupe_key` also requires the text immediately after the matched prefix to start with `gid://` (it doesn't need to be nothing BUT the gid — trailing text like `:template_key:uuid` is fine — but the gid must come first, right after the prefix). Step 1 below adds a new registered prefix for this feature.

- [ ] **Step 1: Register a new dedupe-key prefix for admin resends**

In `backend/app/jobs/outbox_drain.py`, find `_ORDER_CONFIRMATION_DEDUPE_PREFIXES` and `_FULFILLMENT_DEDUPE_PREFIXES` (around line 48-50). Add a new tuple and include it in `_DEDUPE_PREFIXES`, in the SAME family as `_FULFILLMENT_DEDUPE_PREFIXES` (no `order_mappings.status` side effect — an admin resend must never touch order-confirmation state, only the fulfillment family gets the "opaque validity check only" treatment used at `outbox_drain.py:300`, and this must too):

```python
_ORDER_CONFIRMATION_DEDUPE_PREFIXES = ("order_created:", "order_reminder:")
_FULFILLMENT_DEDUPE_PREFIXES = ("fulfillment_shipped:", "fulfillment_delivered:")
_ADMIN_RESEND_DEDUPE_PREFIXES = ("admin_resend:",)
_DEDUPE_PREFIXES = (
    _ORDER_CONFIRMATION_DEDUPE_PREFIXES + _FULFILLMENT_DEDUPE_PREFIXES + _ADMIN_RESEND_DEDUPE_PREFIXES
)
```

Confirm the success-path gate at `outbox_drain.py:300` (`if row.dedupe_key.startswith(_ORDER_CONFIRMATION_DEDUPE_PREFIXES): await c.ingest.set_mapping_status(...)`) is unaffected — `admin_resend:` is not in that tuple, so no behavior change there; it's purely a widened acceptance list in `_gid_from_dedupe_key`.

Add one test to whichever existing test module covers `_gid_from_dedupe_key`/`_DEDUPE_PREFIXES` (grep `backend/tests/test_outbox_drain_job.py` for an existing test of this function and mirror its style):

```python
def test_gid_from_dedupe_key_accepts_admin_resend_prefix() -> None:
    from app.jobs.outbox_drain import _gid_from_dedupe_key

    result = _gid_from_dedupe_key("admin_resend:gid://shopify/Order/123:cod_confirmation:abc-uuid")
    assert result == "gid://shopify/Order/123:cod_confirmation:abc-uuid"
```

(The function only checks the extracted remainder STARTS WITH `gid://` — it does not parse out just the gid portion — so the assertion above is correct: the full remainder, trailing template-key/uuid included, is what's returned. This is fine; nothing downstream of `_gid_from_dedupe_key` for the `admin_resend:` family does anything with `gid` beyond the truthy/None check, matching the fulfillment family's existing "opaque validity check" posture.)

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py -k "admin_resend" -v` — expect PASS immediately (this is additive, no other code changed yet), then run the FULL `test_outbox_drain_job.py` suite to confirm nothing regressed: `cd backend && python -m pytest tests/test_outbox_drain_job.py -v`.

Commit this step on its own before moving to Step 2:
```bash
git add backend/app/jobs/outbox_drain.py backend/tests/test_outbox_drain_job.py
git commit -m "feat(outbox): register admin_resend: dedupe-key prefix for template resends"
```

- [ ] **Step 2: Write the failing backend tests**

Add to `backend/tests/admin/test_views.py` (reuse the existing `_seed_whatsapp_config()` helper already in this file from the manual-reply feature — read it first). The template send's actual WhatsApp call happens INSIDE `send_inline_outbound` → `send_one_outbound`, both in `app/jobs/outbox_drain.py`, which imports `send_template` directly into ITS OWN module namespace (`from app.channels.whatsapp_sender import ... send_template` at the top of `outbox_drain.py`) — so tests must monkeypatch `"app.jobs.outbox_drain.send_template"`, NOT `"app.admin.router.send_template"` (the admin router never calls `send_template` itself for this endpoint). Check `backend/tests/test_outbox_drain_job.py` for the established fake-sender class used against this exact patch target and reuse/adapt it rather than writing a new one from scratch if a suitable one already exists there — importing it into `test_views.py` if it's already generically shaped, or copying its shape if it's file-local.

```python
async def _seed_order_for_thread(
    order_name: str = "tavas5001", phone: str = "+919876500050",
) -> int:
    from app.store.base import MappingUpsert

    thread_id = asyncio.run(get_container().conversations.get_or_create(phone))
    mapping = MappingUpsert(
        order_gid="gid://shopify/Order/50001",
        order_name=order_name,
        order_number_int=5001,
        phone_e164=phone,
        customer_name="Test Customer",
        email="t@example.com",
        language="en",
    )
    asyncio.run(get_container().ingest.ingest_order_created(
        "wh-template-test-1", "orders/create", mapping, None,
    ))
    return thread_id


def test_list_templates_requires_auth(client: TestClient) -> None:
    resp = client.get("/admin/conversations/1/templates")
    assert resp.status_code == 401


def test_list_templates_unknown_thread_returns_404(client: TestClient) -> None:
    login(client)
    resp = client.get("/admin/conversations/900000000003/templates")
    assert resp.status_code == 404


def test_list_templates_returns_all_four_with_defaults(client: TestClient) -> None:
    login(client)
    thread_id = _seed_order_for_thread()
    resp = client.get(f"/admin/conversations/{thread_id}/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["orders"]) == 1
    keys = {t["key"] for t in data["orders"][0]["templates"]}
    assert keys == {"cod_confirmation", "prepaid_order", "order_shipped", "order_delivered"}
    cod = next(t for t in data["orders"][0]["templates"] if t["key"] == "cod_confirmation")
    assert cod["has_buttons"] is True
    order_id_field = next(f for f in cod["fields"] if f["key"] == "order_id")
    assert order_id_field["value"] == "tavas5001"


def test_send_template_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/admin/conversations/1/templates",
        json={"order_name": "tavas1", "template": "order_shipped", "values": {}},
    )
    assert resp.status_code == 401


def test_send_template_rejects_unknown_template_key(client: TestClient) -> None:
    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(phone="+919876500051")
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5001", "template": "not_a_real_template", "values": {}},
    )
    assert resp.status_code == 400


def test_send_template_rejects_unknown_order_name(client: TestClient) -> None:
    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(phone="+919876500052")
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas_does_not_exist", "template": "order_shipped", "values": {}},
    )
    assert resp.status_code == 404


def test_send_template_positional_shipped_sends_and_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(order_name="tavas5002", phone="+919876500053")

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL1", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={
            "order_name": "tavas5002", "template": "order_shipped",
            "values": {"tracking_company": "Delhivery"},
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["template"] == "order_shipped"
    assert isinstance(call["body_params"], list)
    assert call["body_params"][1] == "tavas5002"  # order_name field, positional index 1
    assert call["body_params"][2] == "Delhivery"  # admin-edited override
    assert call["button_payloads"] == []


def test_send_template_cod_confirmation_uses_server_resolved_gid_for_buttons(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(order_name="tavas5003", phone="+919876500054")

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL2", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    # Request tries to smuggle an unrelated gid inside `values` -- it must be ignored, since gid
    # is never read from the request body at all, only re-resolved server-side by order_name.
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={
            "order_name": "tavas5003", "template": "cod_confirmation",
            "values": {"gid": "gid://shopify/Order/ATTACKER"},
        },
    )
    assert resp.status_code == 200
    call = fake.calls[0]
    assert call["button_payloads"] == [
        "order:confirm:gid://shopify/Order/50001", "order:cancel:gid://shopify/Order/50001",
    ]


def test_send_template_kill_switch_off_leaves_it_queued_not_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike send_manual_reply's free text, a template resend respects send_mode -- it goes
    through the same enqueue_outbound + send_inline_outbound pipeline as every automatic template
    send, and send_inline_outbound leaves an 'off'-mode row queued for the backstop drain rather
    than sending immediately or failing. Enqueuing successfully is reported as {"ok": true}: it
    is not a failure, the row is just not sent YET."""
    from app.admin.controls import AdminControls, save_controls

    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(order_name="tavas5004", phone="+919876500055")
    asyncio.run(save_controls(
        get_container().config,
        AdminControls(
            send_mode="off", allowlist_phones=[], owner_alert_number="", default_language="en",
        ),
    ))

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL3", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5004", "template": "order_delivered", "values": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(fake.calls) == 0  # never reached Meta -- send_mode="off" left the row queued
```

Check `IngestStore.ingest_order_created`'s exact signature (`webhook_id, topic, mapping, outbound`) against its current definition in `base.py`/`postgres.py`/`memory.py` before pasting — this mirrors the pattern already used elsewhere in `test_views.py` for seeding an order (search the file for an existing helper like `_ingest_one` and prefer adapting it over writing a new one from scratch, if a suitable one is present). `SendResult` needs importing (`from app.channels.whatsapp_sender import SendResult`) if not already imported at module scope in `test_views.py`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "list_templates or send_template" -v`
Expected: FAIL — routes don't exist yet (404/405).

- [ ] **Step 4: Implement the backend**

In `backend/app/admin/router.py`, add to the imports:

```python
import uuid

from app.admin.template_catalog import TEMPLATE_CATALOG, resolve_template_defaults
from app.jobs.outbox_drain import OUTCOME_SENT, send_inline_outbound
from app.store.base import OutboundDraft
```

(`OutboundDraft` may already be imported for other purposes — check the existing import line and extend it rather than duplicating. Same for checking whether `uuid` is already imported anywhere in this file. This endpoint does NOT need `send_template`/`WhatsAppSendError` imported into `router.py` at all — it never calls the sender directly, only via `send_inline_outbound`.)

Add a request model near `ManualReplyRequest`:

```python
class TemplateSendRequest(BaseModel):
    order_name: str
    template: str
    values: dict[str, str] = Field(default_factory=dict)
```

Add two endpoints directly below `send_manual_reply()`:

```python
@admin_router.get(
    "/conversations/{thread_id}/templates", dependencies=[Depends(require_admin)]
)
async def list_templates(thread_id: int) -> dict[str, object]:
    """List every order for this thread's customer with each catalog template's fields
    pre-filled from that order's current data -- purely a read, no send happens here."""
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")
    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    order_payloads: list[dict[str, object]] = []
    for order in orders:
        defaults = resolve_template_defaults(order)
        templates = [
            {
                "key": key,
                "label": tmpl.label,
                "has_buttons": tmpl.has_confirm_cancel_buttons,
                "fields": [
                    {"key": f.key, "label": f.label, "value": defaults.get(f.default_from, "")}
                    for f in tmpl.fields
                ],
            }
            for key, tmpl in TEMPLATE_CATALOG.items()
        ]
        order_payloads.append({"order_name": order.name, "templates": templates})
    return {"orders": order_payloads}


@admin_router.post(
    "/conversations/{thread_id}/templates", dependencies=[Depends(require_admin)]
)
@limiter.limit("30/minute")
async def send_admin_template(
    request: Request, thread_id: int, body: TemplateSendRequest, response: Response
) -> dict[str, object]:
    """Resend one of the store's approved templates for a specific order, with admin-editable
    field values, via the SAME enqueue_outbound + send_inline_outbound pipeline every automatic
    template send already uses (order confirmation, shipped, delivered) -- so it respects
    send_decision/send_mode/allowlist_phones exactly like those, UNLIKE send_manual_reply's free
    text (which deliberately bypasses the kill switch; a template resend does not, since it's the
    same category of message as every other automatic template send).

    The order's gid (used for cod_confirmation's Confirm/Cancel buttons) is ALWAYS resolved here,
    server-side, from find_mirrored_orders_by_phone -- `body` never carries a gid at all, only the
    human-readable order_name, so there is no way for a request to target a button payload at an
    order it didn't already have visibility into via this same thread.
    """
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=404, detail="thread not found")

    tmpl = TEMPLATE_CATALOG.get(body.template)
    if tmpl is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=400, detail="unknown template")

    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    order = next((o for o in orders if o.name == body.order_name), None)
    if order is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=404, detail="order not found for this customer")

    defaults = resolve_template_defaults(order)
    resolved: dict[str, str] = {
        f.key: (body.values.get(f.key) or defaults.get(f.default_from, "") or "")
        for f in tmpl.fields
    }
    body_params: dict[str, str] | list[str]
    if tmpl.param_style == "named":
        body_params = resolved
    else:
        body_params = [resolved[f.key] for f in tmpl.fields]
    button_payloads = (
        [f"order:confirm:{order.gid}", f"order:cancel:{order.gid}"]
        if tmpl.has_confirm_cancel_buttons else []
    )

    # Prefix is "admin_resend:" (registered in outbox_drain.py's _DEDUPE_PREFIXES), followed
    # immediately by the order's gid (required: _gid_from_dedupe_key needs the text right after
    # the matched prefix to start with "gid://"), then the template key and a fresh uuid so a
    # repeat resend of the SAME template for the SAME order never collides on dedupe_key -- this
    # is a deliberate repeat, not a dedupe-guarded automatic trigger.
    draft = OutboundDraft(
        dedupe_key=f"admin_resend:{order.gid}:{body.template}:{uuid.uuid4()}",
        kind="admin_template_resend",
        phone_e164=user_id,
        payload_json=json.dumps(
            {"template": body.template, "language": tmpl.language, "body_params": body_params,
             "buttons": button_payloads}
        ),
    )
    outbound_id = await c.ingest.enqueue_outbound(draft)
    if outbound_id is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        response.status_code = 502
        return {"ok": False, "error": "failed to queue template"}

    outcome = await send_inline_outbound(c, outbound_id)
    # None means nothing was attempted (send_mode="off", no WhatsApp config, or the row's claim
    # was lost to a race) -- the row stays queued for the backstop drain, which is a SUCCESSFUL
    # enqueue, not a failure the admin needs to retry. Only a terminal outcome the row can never
    # recover from on its own (send_inline_outbound never returns "retry" -- that's the cron-drain
    # path's own bump_outbound_attempt state, not reachable from a single inline attempt) counts
    # as a failure here.
    if outcome not in (None, OUTCOME_SENT):
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        response.status_code = 502
        return {"ok": False, "error": f"send failed: {outcome}"}

    _audit("admin_template_resend", "success", resource=f"thread:{thread_id}")
    return {"ok": True}
```

Read the CURRENT `send_manual_reply` implementation first (it may have shifted slightly since this plan was written) to confirm `load_whatsapp_config`/`_audit`/`HTTPException` usage patterns are unchanged, and adapt the above to match reality rather than pasting blind. Also re-read `send_inline_outbound`'s CURRENT docstring/return type in `app/jobs/outbox_drain.py` before finalizing the `if outcome not in (None, OUTCOME_SENT):` check — the plan's design was verified against the code at plan-writing time (returns `str | None`: `OUTCOME_SENT`/`OUTCOME_SUPPRESSED`/`OUTCOME_UNDELIVERABLE`/`OUTCOME_FAILED`, or `None` when nothing was attempted), but confirm this hasn't changed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_views.py -k "list_templates or send_template" -v`
Expected: PASS (9 tests).

- [ ] **Step 6: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures. Confirm `backend/app/core/order_actions.py` is untouched: `git diff -- backend/app/core/order_actions.py` returns empty.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): add template listing + resend endpoints for admin chat"
```

---
### Task 3: Frontend — emoji picker + template dialog

**Files:**
- Modify: `backend/app/admin/static/chats.html` (two icon buttons, emoji popup markup, template dialog markup, CSS)
- Modify: `backend/app/admin/static/chats.js` (emoji grid + insert-at-cursor, template dialog wiring)
- Test: `backend/tests/admin/test_static_mount.py`

**Interfaces:**
- Consumes: `GET /admin/conversations/{thread_id}/templates` and `POST /admin/conversations/{thread_id}/templates` (Task 2).
- Produces: no new backend interface — purely presentational.

- [ ] **Step 1: Write the failing frontend smoke tests**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_page_has_emoji_and_template_buttons(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="emoji-btn"' in resp.text
    assert 'id="template-btn"' in resp.text
    assert 'id="emoji-popup"' in resp.text
    assert 'id="template-dialog"' in resp.text


def test_chats_js_wires_emoji_insert_and_template_send(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/templates" in js
    assert "emoji-btn" in js
    assert "template-btn" in js
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "emoji or template_btn or template_send" -v`
Expected: FAIL.

- [ ] **Step 3: Implement the frontend — HTML**

In `backend/app/admin/static/chats.html`, replace the existing `#reply-bar` block:

```html
      <div id="reply-bar">
        <input id="reply-input" type="text" placeholder="Type a message" />
        <button id="reply-send-btn">Send</button>
      </div>
```

with:

```html
      <div id="reply-bar">
        <button id="emoji-btn" type="button" title="Emoji">😊</button>
        <button id="template-btn" type="button" title="Send a template">📋</button>
        <input id="reply-input" type="text" placeholder="Type a message" />
        <button id="reply-send-btn">Send</button>
      </div>
      <div id="emoji-popup" style="display:none"></div>
      <div id="template-dialog" style="display:none">
        <div id="template-dialog-inner">
          <div id="template-dialog-header">
            <span>Send a Template</span>
            <button id="template-dialog-close" type="button">×</button>
          </div>
          <div id="template-dialog-body"></div>
        </div>
      </div>
```

CSS, near the existing `#reply-bar`/`#reply-input` rules:

```css
    #emoji-btn, #template-btn { background: none; border: none; font-size: 1.3rem;
      cursor: pointer; padding: .2rem .4rem; }
    #emoji-popup { position: absolute; bottom: 4.2rem; left: 1.2rem; background: #fff;
      border: 1px solid #d1d7db; border-radius: 8px; padding: .5rem; box-shadow: 0 2px 8px rgba(0,0,0,.15);
      display: grid; grid-template-columns: repeat(8, 1fr); gap: .2rem; z-index: 10; }
    #emoji-popup button { background: none; border: none; font-size: 1.15rem; cursor: pointer;
      padding: .2rem; border-radius: 4px; }
    #emoji-popup button:hover { background: #f0f2f5; }
    #template-dialog { position: fixed; inset: 0; background: rgba(0,0,0,.35);
      display: flex; align-items: center; justify-content: center; z-index: 20; }
    #template-dialog-inner { background: #fff; border-radius: 10px; width: 420px; max-height: 80vh;
      overflow-y: auto; padding: 1rem; }
    #template-dialog-header { display: flex; justify-content: space-between; align-items: center;
      font-weight: 600; margin-bottom: .7rem; }
    #template-dialog-close { background: none; border: none; font-size: 1.2rem; cursor: pointer; }
    .template-order-heading, .template-pick-heading { font-size: .82rem; font-weight: 600;
      color: #667781; margin: .6rem 0 .4rem; }
    .template-pick-row { padding: .5rem .6rem; border: 1px solid #e9edef; border-radius: 6px;
      margin-bottom: .4rem; cursor: pointer; font-size: .85rem; }
    .template-pick-row:hover { background: #f5f6f6; }
    .template-field-row { margin-bottom: .5rem; }
    .template-field-row label { display: block; font-size: .75rem; color: #667781;
      margin-bottom: .2rem; }
    .template-field-row input { width: 100%; padding: .4rem .5rem; border: 1px solid #d1d7db;
      border-radius: 6px; font-size: .82rem; }
    #template-send-btn { background: #00a884; color: #fff; border: none; border-radius: 6px;
      padding: .5rem 1rem; font-size: .82rem; cursor: pointer; margin-top: .5rem; }
```

`#reply-bar` already has `position: relative`? Check the existing rule — if not, add `position: relative;` to `#reply-bar`'s existing CSS block (read the current rule first) so `#emoji-popup`'s `position: absolute` anchors correctly relative to the reply bar rather than the page.

- [ ] **Step 4: Implement the frontend — JS: emoji picker**

Add near the top of `chats.js`, after the existing constants:

```javascript
const EMOJI_LIST = [
  "😀", "😂", "😊", "😍", "🙏", "👍", "👎", "🙌",
  "🎉", "❤️", "🔥", "✅", "❌", "⏳", "📦", "🚚",
  "😢", "😡", "🤔", "😅", "🙂", "😎", "💯", "🤝",
  "📞", "📍", "💳", "🛍️", "⭐", "🙁", "👋", "🎁",
];

function buildEmojiPopup() {
  const popup = el("emoji-popup");
  popup.innerHTML = "";
  for (const emoji of EMOJI_LIST) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = emoji;
    btn.addEventListener("click", () => insertAtCursor(el("reply-input"), emoji));
    popup.appendChild(btn);
  }
}

function insertAtCursor(input, text) {
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  const newPos = start + text.length;
  input.focus();
  input.setSelectionRange(newPos, newPos);
}

buildEmojiPopup();

el("emoji-btn").addEventListener("click", () => {
  const popup = el("emoji-popup");
  popup.style.display = popup.style.display === "none" ? "grid" : "none";
});

document.addEventListener("click", (e) => {
  const popup = el("emoji-popup");
  if (popup.style.display === "none") return;
  if (e.target === el("emoji-btn") || popup.contains(e.target)) return;
  popup.style.display = "none";
});
```

- [ ] **Step 5: Implement the frontend — JS: template dialog**

Add near the emoji code:

```javascript
function closeTemplateDialog() {
  el("template-dialog").style.display = "none";
}

function renderTemplateFieldForm(orderName, tmpl) {
  const body = el("template-dialog-body");
  body.innerHTML = "";
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = "← Back";
  back.addEventListener("click", () => openTemplateDialog());
  body.appendChild(back);

  const heading = document.createElement("div");
  heading.className = "template-order-heading";
  heading.textContent = tmpl.label + " — " + orderName;
  body.appendChild(heading);

  const inputs = {};
  for (const field of tmpl.fields) {
    const row = document.createElement("div");
    row.className = "template-field-row";
    const label = document.createElement("label");
    label.textContent = field.label;
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || "";
    inputs[field.key] = input;
    row.appendChild(label);
    row.appendChild(input);
    body.appendChild(row);
  }

  const sendBtn = document.createElement("button");
  sendBtn.id = "template-send-btn";
  sendBtn.type = "button";
  sendBtn.textContent = "Send";
  sendBtn.addEventListener("click", async () => {
    if (currentThreadId === null) return;
    sendBtn.disabled = true;
    const values = {};
    for (const key in inputs) values[key] = inputs[key].value;
    try {
      await api(
        "/admin/conversations/" + encodeURIComponent(currentThreadId) + "/templates",
        "POST",
        { order_name: orderName, template: tmpl.key, values }
      );
      closeTemplateDialog();
      await loadThread(currentThreadId, currentPhone);
    } catch (e) {
      el("reply-status").textContent = e.message;
    } finally {
      sendBtn.disabled = false;
    }
  });
  body.appendChild(sendBtn);
}

function renderTemplatePickList(data) {
  const body = el("template-dialog-body");
  body.innerHTML = "";
  for (const orderEntry of data.orders) {
    const heading = document.createElement("div");
    heading.className = "template-pick-heading";
    heading.textContent = orderEntry.order_name;
    body.appendChild(heading);
    for (const tmpl of orderEntry.templates) {
      const row = document.createElement("div");
      row.className = "template-pick-row";
      row.textContent = tmpl.label;
      row.addEventListener("click", () => renderTemplateFieldForm(orderEntry.order_name, tmpl));
      body.appendChild(row);
    }
  }
  if (!data.orders.length) {
    body.textContent = "No orders found for this customer.";
  }
}

async function openTemplateDialog() {
  if (currentThreadId === null) return;
  el("template-dialog").style.display = "flex";
  el("template-dialog-body").textContent = "Loading…";
  try {
    const data = await api(
      "/admin/conversations/" + encodeURIComponent(currentThreadId) + "/templates"
    );
    renderTemplatePickList(data);
  } catch (e) {
    el("template-dialog-body").textContent = e.message;
  }
}

el("template-btn").addEventListener("click", openTemplateDialog);
el("template-dialog-close").addEventListener("click", closeTemplateDialog);
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -k "emoji or template_btn or template_send" -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`
Expected: all green, no new failures.

- [ ] **Step 8: Manual browser verification**

No browser test runner exists in this repo. Before handing off to review, manually verify: the emoji button toggles the popup and clicking an emoji inserts it at the cursor position (not just appended) and keeps focus in the input; clicking outside the popup closes it; the template button opens the dialog, shows the customer's order(s), picking a template shows a pre-filled editable form, editing a field and sending actually sends (check a real/sandboxed number) and the dialog closes and the thread reloads showing the new template message with a tick mark; the "← Back" link returns to the template list without closing the dialog.

- [ ] **Step 9: Commit**

```bash
git add backend/app/admin/static/chats.html backend/app/admin/static/chats.js backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): add emoji picker + template resend dialog to chat page"
```

---

## Post-Implementation Notes

- No task in this plan touches `backend/app/core/order_actions.py` — verify with `git diff <base-commit> HEAD -- backend/app/core/order_actions.py` returning empty before handing off to review.
- The `security-reviewer` pass should specifically confirm: (1) the order `gid` used for `cod_confirmation`'s buttons is never taken from the request body under any code path (both the happy path and the "unknown order_name" 404 path); (2) `values` dict keys not present in the picked template's `fields` are never passed through to `send_template` (the resolution loop in Task 2 only iterates `tmpl.fields`, so an extra/unexpected key in `values` is structurally ignored — confirm this holds); (3) the template-resend endpoint genuinely respects `send_decision`/`send_mode`/`allowlist_phones` (unlike the manual-reply feature's free text) — it must not be possible to reach a real Meta send from this endpoint while `send_mode == "off"`.
- No schema/migration is needed for this feature — it reuses `outbound_messages`/`conversations`/mirrored `orders` exactly as they already exist.
