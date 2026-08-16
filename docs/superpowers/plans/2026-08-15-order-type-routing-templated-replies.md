# Order-Type Routing + Templated Confirm/Cancel Replies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route new-order push template by payment type (COD vs. prepaid), and replace the Confirm/Cancel button-tap replies with the owner's newly-approved branded templates (`cod_confirmmsg` on Confirm, `cod_cancel` once a cancellation is actually confirmed) instead of today's plain text.

**Architecture:** Three small, independent behavior changes on top of the existing outbox/button-dispatch/reconcile machinery — no new tables, no new outbox kind, no new job. `send_template` (the shared low-level sender) gains support for positional (non-named) template parameters, since `cod_confirmmsg`/`cod_cancel` use `{{1}}`/`{{2}}` placeholders unlike `cod_confirmation`'s named ones.

**Tech Stack:** Python 3.12, FastAPI, pytest + pytest-asyncio, httpx `MockTransport` for sender tests, existing `InMemoryIngestStore`/`ConfigService` test fixtures.

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean (64 files today).
- `ruff check .` clean. No bare `except:`. No `print()` — use the existing `logging.getLogger("app.<module>")` pattern.
- `cod_confirmmsg` and `cod_cancel` are Meta-approved in `en` ONLY (verified live against the WABA during planning, same situation as `cod_confirmation`/`prepaid_order`, Q19c) — both new template sends are pinned to `"en"`, never the customer's detected language.
- `backend/app/core/order_actions.py`'s cancel-flow *mutation logic* (two-phase gating, ownership checks, tag/mapping writes) must be byte-identical before/after — only the CONFIRM reply mechanism changes in this file. Verify via reading the diff, not just running tests.
- Every new outbound send must respect the `send_mode` kill switch (`off`/`shadow`/`allowlist`/`live`) via `app.core.send_policy.send_decision` — `jobs/reconcile.py` has never sent anything before, so it gets this gate added explicitly (it does not inherit it from anywhere).
- Secrets/print/bare-except compliance grep (from `no-secrets.md`) must return empty on every touched file:
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>`
- Do not push to git — commit locally only, per this repo's standing rule (owner approves pushes separately).

---

## File Structure

- **Modify** `backend/app/channels/whatsapp_sender.py` — widen `send_template`'s `body_params` to accept a positional `Sequence[str]` in addition to the existing named `Mapping[str, str]`.
- **Modify** `backend/app/channels/copy.py` — add the shared `EMPTY_PARAM_PLACEHOLDER` constant (relocated from `shopify_webhook.py`, which is the only other module that needs it after this feature — now three do).
- **Modify** `backend/app/channels/shopify_orders.py` — add `customer_display_name(order: Order) -> str`, a shared helper both `order_actions.py` and `reconcile.py` need to build the `{{1}}` name parameter for `cod_confirmmsg`/`cod_cancel`; import `EMPTY_PARAM_PLACEHOLDER` from `copy.py` instead of the old `shopify_webhook.py`-local constant.
- **Modify** `backend/app/channels/shopify_webhook.py` — drop the local `_EMPTY_PARAM_PLACEHOLDER` (now imported from `copy.py`); replace the single `TEMPLATE_NAME` constant with COD/prepaid routing at the `orders/create` call site.
- **Modify** `backend/app/jobs/outbox_drain.py` — only attach Confirm/Cancel buttons when the row's template is `cod_confirmation` (not `prepaid_order`).
- **Modify** `backend/app/core/order_actions.py` — add a `_safe_send_template` helper (mirrors `_safe_send_text`/`_safe_send_buttons`); `_handle_confirm` sends `cod_confirmmsg` instead of the `confirm_success`/`already_confirmed` plain-text replies.
- **Modify** `backend/app/jobs/reconcile.py` — after a cancellation is confirmed and finally tagged, send `cod_cancel` (new capability; gated by `send_mode`, degrades gracefully on a WhatsApp config or transport failure without affecting the tag/status write).
- **Test files** (extend existing, no new files): `backend/tests/test_whatsapp_sender.py`, `backend/tests/test_shopify_webhook.py`, `backend/tests/test_outbox_drain_job.py`, `backend/tests/core/test_button_dispatch.py`, `backend/tests/test_reconcile_cancels.py`.

---

### Task 1: Shared plumbing — positional template params, shared placeholder, display-name helper

**Files:**
- Modify: `backend/app/channels/whatsapp_sender.py:122-180` (`send_template`)
- Modify: `backend/app/channels/copy.py:1-4`
- Modify: `backend/app/channels/shopify_orders.py:1-16` (imports + new function)
- Test: `backend/tests/test_whatsapp_sender.py`
- Test: `backend/tests/test_shopify_orders.py`

**Interfaces:**
- Produces: `send_template(..., body_params: Mapping[str, str] | Sequence[str], ...)` — a `Mapping` builds NAMED body parameters (unchanged behavior); a `Sequence[str]` (e.g. a `list[str]`) builds POSITIONAL body parameters (no `parameter_name` key).
- Produces: `app.channels.copy.EMPTY_PARAM_PLACEHOLDER: Final[str] = "-"`.
- Produces: `app.channels.shopify_orders.customer_display_name(order: Order) -> str` — `"First Last"` from `order.customer`, or `EMPTY_PARAM_PLACEHOLDER` if there's no customer or both name fields are blank.
- Consumed by: Task 2 (`shopify_webhook.py` reuses `EMPTY_PARAM_PLACEHOLDER`), Task 3 (`order_actions.py` uses `send_template` positionally + `customer_display_name`), Task 4 (`reconcile.py` uses both the same way).

- [ ] **Step 1: Write the failing test for positional `send_template` params**

Add to `backend/tests/test_whatsapp_sender.py`, right after `test_send_template_builds_named_body_and_button_components` (currently ending around line 85):

```python
async def test_send_template_builds_positional_body_params() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"messages": [{"id": "wamid.T2"}]})

    await send_template(
        client_with(handler), CFG, "919999999999", "cod_confirmmsg", "en",
        body_params=["Bhavesh", "tavas3733"],
    )
    template = captured["body"]["template"]
    assert template["name"] == "cod_confirmmsg"
    body_component = next(c for c in template["components"] if c["type"] == "body")
    # Positional params: NO parameter_name key, order preserved.
    assert body_component["parameters"] == [
        {"type": "text", "text": "Bhavesh"},
        {"type": "text", "text": "tavas3733"},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_whatsapp_sender.py::test_send_template_builds_positional_body_params -v`
Expected: FAIL — a `list[str]` currently hits the `.items()` call in `send_template`'s named-param branch and raises `AttributeError: 'list' object has no attribute 'items'`.

- [ ] **Step 3: Widen `send_template` to accept positional or named params**

In `backend/app/channels/whatsapp_sender.py`, change the import line and signature/body:

```python
import re
from collections.abc import Mapping, Sequence
```//no change here, already present — confirm it's there.

Change the signature (currently `body_params: Mapping[str, str],`) to:

```python
async def send_template(
    http: httpx.AsyncClient,
    cfg: WhatsAppConfig,
    to: str,
    template_name: str,
    language: str,
    body_params: Mapping[str, str] | Sequence[str],
    button_payloads: Sequence[str] = (),
    header_image_url: str | None = None,
    timeout: float = 20.0,
) -> SendResult:
    """Send an approved template. ``body_params`` is either a NAMED mapping (name -> value, each
    parameter object carries ``parameter_name`` — used by templates with named placeholders like
    ``cod_confirmation``) or a POSITIONAL sequence (used by templates with ``{{1}}``/``{{2}}``-style
    placeholders like ``cod_confirmmsg``/``cod_cancel`` — no ``parameter_name`` key, order matters).
    ``header_image_url``, when a public https link, adds an IMAGE header component (the live
    product photo); omit it to send with no header. Components are ordered header -> body ->
    buttons, as Meta expects.
    """
```

Replace the existing body-building block (currently `if body_params: components.append({"type": "body", "parameters": [...]})`) with:

```python
    if body_params:
        if isinstance(body_params, Mapping):
            parameters = [
                {"type": "text", "parameter_name": name, "text": value}
                for name, value in body_params.items()
            ]
        else:
            parameters = [{"type": "text", "text": value} for value in body_params]
        components.append({"type": "body", "parameters": parameters})
```

- [ ] **Step 4: Run test to verify it passes, plus the full sender suite**

Run: `cd backend && python -m pytest tests/test_whatsapp_sender.py -v`
Expected: PASS — all tests including the new one and the existing `test_send_template_builds_named_body_and_button_components` (a `dict` is a `Mapping`, so that path is unaffected).

- [ ] **Step 5: Relocate `EMPTY_PARAM_PLACEHOLDER` to `copy.py`**

In `backend/app/channels/copy.py`, change lines 1-4 from:

```python
from typing import Final

SUPPORTED_LANGUAGES: Final = ("en", "hi", "hinglish", "gu")
DEFAULT_LANGUAGE: Final = "en"
```

to:

```python
from typing import Final

SUPPORTED_LANGUAGES: Final = ("en", "hi", "hinglish", "gu")
DEFAULT_LANGUAGE: Final = "en"
# Meta rejects an empty template body parameter (named or positional) — every template-sending
# call site substitutes this when a real value is unavailable, rather than sending "".
EMPTY_PARAM_PLACEHOLDER: Final = "-"
```

- [ ] **Step 6: Write the failing test for `customer_display_name`**

Add to `backend/tests/test_shopify_orders.py` (check existing imports at the top of that file first and reuse them — it already imports `Order`/`Customer` fixtures for other tests):

```python
from app.channels.copy import EMPTY_PARAM_PLACEHOLDER
from app.channels.shopify_orders import customer_display_name
from app.shopify.models import Customer, Order


def _bare_order(gid: str = "gid://shopify/Order/1", customer: Customer | None = None) -> Order:
    return Order(
        gid=gid, name="tavas1", email=None, phone="+919664290413", shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None, cancelled_at=None,
        tags=(), payment_gateway_names=(), total=None, customer_locale=None, customer=customer,
    )


def test_customer_display_name_from_first_and_last() -> None:
    customer = Customer(
        gid="gid://shopify/Customer/1", first_name="Suman", last_name="B", email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=None, postal_code=None,
        country=None,
    )
    assert customer_display_name(_bare_order(customer=customer)) == "Suman B"


def test_customer_display_name_placeholder_when_no_customer() -> None:
    assert customer_display_name(_bare_order(customer=None)) == EMPTY_PARAM_PLACEHOLDER


def test_customer_display_name_placeholder_when_names_blank() -> None:
    customer = Customer(
        gid="gid://shopify/Customer/1", first_name=None, last_name=None, email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=None, postal_code=None,
        country=None,
    )
    assert customer_display_name(_bare_order(customer=customer)) == EMPTY_PARAM_PLACEHOLDER
```

- [ ] **Step 7: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_shopify_orders.py -k customer_display_name -v`
Expected: FAIL with `ImportError: cannot import name 'customer_display_name'`.

- [ ] **Step 8: Implement `customer_display_name`**

In `backend/app/channels/shopify_orders.py`, add the import (extend the existing import block at the top):

```python
from app.channels.copy import EMPTY_PARAM_PLACEHOLDER
from app.core.phone import normalize_phone
from app.shopify.models import Customer, Fulfillment, LineItem, Money, Order
```

Add the function near the bottom of the file (after `customer_from_webhook_payload`, since it's conceptually the same "build a Customer-facing name" family):

```python
def customer_display_name(order: Order) -> str:
    """A template body-parameter name: "First Last" from the order's customer, or the shared
    empty-param placeholder when there is no customer or both name fields are blank."""
    if order.customer is not None:
        name = f"{order.customer.first_name or ''} {order.customer.last_name or ''}".strip()
        if name:
            return name
    return EMPTY_PARAM_PLACEHOLDER
```

- [ ] **Step 9: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_shopify_orders.py -v`
Expected: PASS — all tests including the three new ones.

- [ ] **Step 10: Run mypy + ruff + secrets grep on touched files**

Run:
```bash
cd backend
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/channels/whatsapp_sender.py app/channels/copy.py app/channels/shopify_orders.py
```
Expected: mypy clean, ruff clean, grep returns nothing.

- [ ] **Step 11: Commit**

```bash
git add backend/app/channels/whatsapp_sender.py backend/app/channels/copy.py backend/app/channels/shopify_orders.py backend/tests/test_whatsapp_sender.py backend/tests/test_shopify_orders.py
git commit -m "feat(whatsapp): support positional template params + shared display-name helper"
```

---

### Task 2: Order-creation routing — COD vs. prepaid template

**Files:**
- Modify: `backend/app/channels/shopify_webhook.py:20-49` (imports/constants), `:333-353` (`orders/create` handler)
- Modify: `backend/app/jobs/outbox_drain.py:230` (button attachment)
- Test: `backend/tests/test_shopify_webhook.py`
- Test: `backend/tests/test_outbox_drain_job.py`

**Interfaces:**
- Consumes: `EMPTY_PARAM_PLACEHOLDER` from Task 1 (`app.channels.copy`).
- Produces: the queued `payload_json`'s `"template"` field is now `"cod_confirmation"` for a COD order or `"prepaid_order"` for a prepaid order (both already handled identically downstream by `outbox_drain.py`'s `_parse_payload`/`_TemplatePayload` — no change needed there beyond the button gating in this task).

- [ ] **Step 1: Write the failing test for prepaid routing**

Add to `backend/tests/test_shopify_webhook.py`, right after `test_orders_create_ingests_and_queues` (ends around line 100 — check the exact next line by reading the file before inserting):

```python
async def test_prepaid_order_routes_to_prepaid_template() -> None:
    body_dict = payload()
    body_dict["tags"] = ""  # no "cod" tag
    body_dict["payment_gateway_names"] = ["Razorpay"]  # not a COD gateway
    body = json.dumps(body_dict).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    store = get_container().ingest
    draft = store.outbound["order_created:gid://shopify/Order/1"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "prepaid_order"
    assert params["language"] == "en"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py::test_prepaid_order_routes_to_prepaid_template -v`
Expected: FAIL — `params["template"] == "cod_confirmation"` today regardless of payment type.

- [ ] **Step 3: Implement COD/prepaid routing**

In `backend/app/channels/shopify_webhook.py`, remove the local placeholder constant and import the shared one. Change:

```python
TEMPLATE_NAME = "cod_confirmation"
# cod_confirmation is Meta-approved in `en` ONLY on the new WABA, so the template send is pinned to
# en regardless of the customer's detected locale — sending it as hi/gu (no such approved locale)
# would make Meta reject every non-en order, reintroducing the exact "every order fails to send"
# bug this change fixes. The customer's own language (mapping.language) still drives the free-form
# conversation replies; only this ONE template send is en. (Owner/client note: to localise the
# confirmation, hi/gu versions of cod_confirmation must first be approved in Meta.)
TEMPLATE_LANGUAGE = "en"
# Meta rejects an empty named body parameter, so any missing product/order field degrades to this
# placeholder rather than an empty string (keeps the send valid; Q19 degrade-gracefully posture).
_EMPTY_PARAM_PLACEHOLDER = "-"
```

to:

```python
TEMPLATE_NAME_COD = "cod_confirmation"
TEMPLATE_NAME_PREPAID = "prepaid_order"
# Both are Meta-approved in `en` ONLY on the new WABA, so the template send is pinned to en
# regardless of the customer's detected locale — sending hi/gu (no such approved locale) would
# make Meta reject the order, reintroducing the exact "every order fails to send" bug this change
# fixes. The customer's own language (mapping.language) still drives the free-form conversation
# replies; only this ONE template send is en. (Owner/client note: to localise the confirmation,
# hi/gu versions of both templates must first be approved in Meta.)
TEMPLATE_LANGUAGE = "en"
```

Add the import (extend the existing import block near the top of the file):

```python
from app.channels.copy import EMPTY_PARAM_PLACEHOLDER
```

Update the 6 usages of `_EMPTY_PARAM_PLACEHOLDER` (around lines 337-342) to `EMPTY_PARAM_PLACEHOLDER` (drop the leading underscore — `find/replace` across the file for that exact token).

Update the template-selection line. Change:

```python
        image_url = await _resolve_product_image(c, incoming.product_gid)
        template_params: dict[str, str] = {
            "template": TEMPLATE_NAME,
            "language": TEMPLATE_LANGUAGE,
```

to:

```python
        image_url = await _resolve_product_image(c, incoming.product_gid)
        template_name = TEMPLATE_NAME_COD if incoming.is_cod() else TEMPLATE_NAME_PREPAID
        template_params: dict[str, str] = {
            "template": template_name,
            "language": TEMPLATE_LANGUAGE,
```

- [ ] **Step 4: Run test to verify it passes, plus the full webhook suite**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py -v`
Expected: PASS — the new test, and `test_orders_create_ingests_and_queues` (COD payload) still asserts `"cod_confirmation"` since `payment_gateway_names: ["Cash on Delivery (COD)"]` in the shared `payload()` helper makes `is_cod()` true.

- [ ] **Step 5: Write the failing test for prepaid = no buttons in the drain**

Add to `backend/tests/test_outbox_drain_job.py`, right after `test_send_one_outbound_sends_and_transitions` (ends around line 115 — read the file to confirm the exact insertion point):

```python
async def test_prepaid_order_row_sends_with_no_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/2"
    await _seed_row(gid, payload={
        "template": "prepaid_order", "language": "en",
        "customer_name": "Suman", "order_id": "tavas3734",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
    })
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.2", error=None))
    _install_sender(monkeypatch, sender)

    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_one_outbound(c, cfg, controls, row)

    assert outcome == "sent"
    assert sender.calls[0]["template"] == "prepaid_order"
    assert sender.calls[0]["button_payloads"] == []
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py::test_prepaid_order_row_sends_with_no_buttons -v`
Expected: FAIL — `buttons` is built unconditionally today, so `button_payloads` would be `["order:confirm:...", "order:cancel:..."]`, not `[]`.

- [ ] **Step 7: Make button attachment conditional on template name**

In `backend/app/jobs/outbox_drain.py`, change line 230 from:

```python
    buttons = [f"order:confirm:{gid}", f"order:cancel:{gid}"]
```

to:

```python
    # prepaid_order has no BUTTONS component approved on the WABA (informational only, no
    # Confirm/Cancel step for a prepaid customer) -- only cod_confirmation gets the quick-reply
    # buttons. Everything else about the send (body params, header image, retry) is identical.
    buttons = (
        [f"order:confirm:{gid}", f"order:cancel:{gid}"] if payload.template == "cod_confirmation"
        else []
    )
```

- [ ] **Step 8: Run test to verify it passes, plus the full outbox drain suite**

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py -v`
Expected: PASS — the new test, and `test_send_one_outbound_sends_and_transitions`/`test_live_sends_marks_sent_and_status` (both seed `"template": "cod_confirmation"` via the default `_seed_row` payload) still assert both buttons present.

- [ ] **Step 9: Run mypy + ruff + secrets grep on touched files**

Run:
```bash
cd backend
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/channels/shopify_webhook.py app/jobs/outbox_drain.py
```
Expected: mypy clean, ruff clean, grep returns nothing.

- [ ] **Step 10: Confirm `order_actions.py` is untouched by this task**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output (this task never touches it).

- [ ] **Step 11: Commit**

```bash
git add backend/app/channels/shopify_webhook.py backend/app/jobs/outbox_drain.py backend/tests/test_shopify_webhook.py backend/tests/test_outbox_drain_job.py
git commit -m "feat(orders): route prepaid orders to prepaid_order template, no buttons"
```

---

### Task 3: Confirm tap sends `cod_confirmmsg`

**Files:**
- Modify: `backend/app/core/order_actions.py:1-30` (imports/constants), `:172-188` (`_handle_confirm`), add `_safe_send_template` helper near `_safe_send_buttons`
- Test: `backend/tests/core/test_button_dispatch.py`

**Interfaces:**
- Consumes: `send_template` (Task 1, positional-params support), `customer_display_name` (Task 1, from `app.channels.shopify_orders`).
- Produces: no new public interface — this is a leaf behavior change inside `dispatch_button`'s existing flow.

- [ ] **Step 1: Extend the `Sends` test fixture to capture template sends**

In `backend/tests/core/test_button_dispatch.py`, the `Sends` class (currently around line 103-111) and the `sends` fixture (around line 113-127) need a template-capture list, including the `language` argument (needed later in this task to prove the `en`-pin). Change:

```python
class Sends:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list[tuple[str, str]]]] = []

    @property
    def last_text(self) -> str:
        return self.texts[-1][1]
```

to:

```python
class Sends:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.templates: list[tuple[str, str, str, list[str]]] = []

    @property
    def last_text(self) -> str:
        return self.texts[-1][1]

    @property
    def last_template(self) -> tuple[str, str, str, list[str]]:
        """(to, template_name, language, body_params)."""
        return self.templates[-1]
```

Change the `sends` fixture (currently patches only `send_text`/`send_buttons`):

```python
@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> Sends:
    captured = Sends()

    async def _send_text(http, cfg, to, body, timeout=20.0) -> SendResult:
        captured.texts.append((to, body))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    async def _send_buttons(http, cfg, to, body_text, buttons, timeout=20.0) -> SendResult:
        captured.buttons.append((to, body_text, list(buttons)))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    async def _send_template(
        http, cfg, to, template_name, language, body_params, button_payloads=(),
        header_image_url=None, timeout=20.0,
    ) -> SendResult:
        captured.templates.append((to, template_name, language, list(body_params)))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    monkeypatch.setattr(order_actions, "send_text", _send_text)
    monkeypatch.setattr(order_actions, "send_buttons", _send_buttons)
    monkeypatch.setattr(order_actions, "send_template", _send_template)
    return captured
```

- [ ] **Step 2: Write the failing tests for the Confirm-tap template send**

Update the three existing confirm-success assertions and add one new test. In `backend/tests/core/test_button_dispatch.py`:

Change (there are two occurrences of `assert sends.last_text == copy_for("confirm_success", "en")`, at lines 260 and 276 — read the file first to confirm exact line numbers before editing, since earlier steps in this same task don't move these lines):

```python
    assert sends.last_text == copy_for("confirm_success", "en")
```

to (both occurrences):

```python
    assert sends.last_template == (OWNER_E164, "cod_confirmmsg", "en", ["-", "tavas1"])
```

(The order in `_order()`'s default has no `customer` set (`Order(...)` in this test file's `_order()` helper does not pass `customer=`, so it defaults to `None` per the `Order` dataclass) — so `customer_display_name` returns the `EMPTY_PARAM_PLACEHOLDER` `"-"`. `order.name` is `"tavas1"` per the `_order()` helper. `cod_confirmmsg` is pinned to `"en"` regardless of order language — see Step 4.)

Change `test_confirm_idempotent_on_already_confirmed` (around line 279-285):

```python
async def test_confirm_idempotent_on_already_confirmed(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(tags=("confirmed",)))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == []  # no second mutation
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("already_confirmed", "en")
```

to:

```python
async def test_confirm_idempotent_on_already_confirmed(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(tags=("confirmed",)))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == []  # no second mutation
    assert c.ingest.order_actions == []
    assert sends.last_template == (OWNER_E164, "cod_confirmmsg", "en", ["-", "tavas1"])
```

Change `test_reply_uses_order_language` (around line 475-479) — this test's whole point (confirming the reply uses the order's detected language) no longer applies to the Confirm-tap reply, since `cod_confirmmsg` is pinned to `"en"` regardless of order language. Replace it with a test proving exactly that pin:

```python
async def test_confirm_template_send_is_pinned_to_en_regardless_of_order_language(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order(locale="hi-IN"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    # cod_confirmmsg is Meta-approved in en only -- the confirm reply is pinned to en even for a
    # hi-IN order (unlike the OTHER replies in this file, which still use copy_for's language
    # detection -- this is a deliberate, template-specific exception, not a general regression).
    _to, template_name, language, _params = sends.last_template
    assert template_name == "cod_confirmmsg"
    assert language == "en"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/core/test_button_dispatch.py -k "confirm" -v`
Expected: FAIL — `_handle_confirm` still calls `_safe_send_text` with `copy_for(...)`, so `sends.templates` stays empty and `sends.last_template` raises `IndexError`.

- [ ] **Step 4: Implement the Confirm-tap template send**

In `backend/app/core/order_actions.py`, update the imports. Change:

```python
from app.admin.controls import AdminControls, load_controls
from app.channels.copy import copy_for
from app.channels.shopify_orders import choose_language
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_inbound import InboundButton, InboundInteractive
from app.channels.whatsapp_sender import WhatsAppSendError, send_buttons, send_text
```

to:

```python
from collections.abc import Sequence

from app.admin.controls import AdminControls, load_controls
from app.channels.copy import copy_for
from app.channels.shopify_orders import choose_language, customer_display_name
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_inbound import InboundButton, InboundInteractive
from app.channels.whatsapp_sender import WhatsAppSendError, send_buttons, send_template, send_text
```

Add two module-level constants right after the existing `_MUTATION_AUDIT_ACTION` dict (around line 44-45):

```python
_COD_CONFIRMMSG_TEMPLATE = "cod_confirmmsg"
# cod_confirmmsg is Meta-approved in `en` ONLY (checked live during planning, same situation as
# cod_confirmation/prepaid_order, Q19c) -- pinned regardless of the order's detected language.
_CONFIRM_TEMPLATE_LANGUAGE = "en"
```

Add `_safe_send_template` right after `_safe_send_buttons` (around line 106-107):

```python
async def _safe_send_template(
    c: Container, cfg: WhatsAppConfig, to: str, template_name: str, body_params: Sequence[str]
) -> None:
    """Send a template reply, swallowing a transport failure so dispatch never raises."""
    try:
        await send_template(c.http, cfg, to, template_name, _CONFIRM_TEMPLATE_LANGUAGE, body_params)
    except WhatsAppSendError:
        logger.warning("button dispatch: template send failed (transport)")
```

Update `_handle_confirm` (currently lines 172-188). Change:

```python
async def _handle_confirm(
    c: Container, cfg: WhatsAppConfig, event: Event, auth: AuthorizedOrder,
    controls: AdminControls, lang: str, gid: str,
) -> None:
    order = auth.order
    if order.is_cancelled():
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_cancelled", lang))
        return
    if _has_any_tag(order.tags, controls.tags.confirmed):
        # Idempotent re-tap: the confirmed tag is already on the live order -> no second mutation.
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_confirmed", lang))
        return
    await c.shopify.add_tags(auth, controls.tags.confirmed)
    await c.ingest.record_order_action(gid, "confirm", event.wa_id, event.message_id, "ok", None)
    await c.ingest.set_mapping_status(gid, "confirmed")
    await _safe_send_text(c, cfg, event.wa_id, copy_for("confirm_success", lang))
```

to:

```python
async def _handle_confirm(
    c: Container, cfg: WhatsAppConfig, event: Event, auth: AuthorizedOrder,
    controls: AdminControls, lang: str, gid: str,
) -> None:
    order = auth.order
    if order.is_cancelled():
        await _safe_send_text(c, cfg, event.wa_id, copy_for("already_cancelled", lang))
        return
    confirm_params = [customer_display_name(order), order.name]
    if _has_any_tag(order.tags, controls.tags.confirmed):
        # Idempotent re-tap: the confirmed tag is already on the live order -> no second mutation.
        # cod_confirmmsg's wording ("your order has been confirmed successfully") is still accurate
        # on a re-tap, so both branches converge on the same template send.
        await _safe_send_template(c, cfg, event.wa_id, _COD_CONFIRMMSG_TEMPLATE, confirm_params)
        return
    await c.shopify.add_tags(auth, controls.tags.confirmed)
    await c.ingest.record_order_action(gid, "confirm", event.wa_id, event.message_id, "ok", None)
    await c.ingest.set_mapping_status(gid, "confirmed")
    await _safe_send_template(c, cfg, event.wa_id, _COD_CONFIRMMSG_TEMPLATE, confirm_params)
```

Note: `lang` (the order's detected language) is still passed into `_handle_confirm` and still used for the `already_cancelled` branch above — only the confirm-success paths stop using it, matching the design's language-pin decision exactly.

- [ ] **Step 5: Run tests to verify they pass, plus the full button-dispatch suite**

Run: `cd backend && python -m pytest tests/core/test_button_dispatch.py -v`
Expected: PASS — all tests, including every cancel-path test (untouched by this task) and the confirm-path tests updated in Step 2.

- [ ] **Step 6: Confirm the cancel mutation logic is unchanged**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: the diff touches only the import block, the two new constants, the new `_safe_send_template` function, and `_handle_confirm`'s body — `_handle_cancel_request`, `_handle_cancel_confirm`, `dispatch_button`'s dispatch logic, `_is_dispatched`, `_has_any_tag`, `_parse_payload` must show NO changes.

- [ ] **Step 7: Run mypy + ruff + secrets grep on touched files**

Run:
```bash
cd backend
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/core/order_actions.py
```
Expected: mypy clean, ruff clean, grep returns nothing.

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/order_actions.py backend/tests/core/test_button_dispatch.py
git commit -m "feat(whatsapp): Confirm tap sends cod_confirmmsg template instead of plain text"
```

---

### Task 4: Cancel confirmation sends `cod_cancel` from the reconcile job

**Files:**
- Modify: `backend/app/jobs/reconcile.py` (whole file — imports, new constants, new `_notify_cancelled` helper, `run_reconcile_cancels` body)
- Test: `backend/tests/test_reconcile_cancels.py`

**Interfaces:**
- Consumes: `send_template` (Task 1), `customer_display_name` (Task 1), `send_decision` (existing, `app.core.send_policy`), `load_whatsapp_config` (existing, `app.channels.whatsapp_config`).
- Produces: no new public interface — `run_reconcile_cancels`'s return shape (`{"checked", "reconciled", "pending", "skipped"}`) is unchanged; this task only adds a side effect (a WhatsApp send) on the `reconciled` path.

- [ ] **Step 1: Write the failing test for the happy-path notification**

Add to `backend/tests/test_reconcile_cancels.py`. First, extend the `_fresh` fixture and `FakeShopify` setup to configure WhatsApp (mirroring `test_outbox_drain_job.py`'s `_fresh` fixture pattern) and add a template-capturing fake. Change the top of the file from:

```python
"""Cancel reconciliation job: apply the final `cancelled` tag once Shopify confirms cancelledAt."""

import httpx
import pytest

from app.core.order_resolver import authorize_own_order
from app.deps import get_container, reset_container
from app.jobs.reconcile import run_reconcile_cancels
from app.shopify.models import AuthorizedOrder, CancelRequested, Order

CRON = "topsecret-reconcile-1"  # >= 16 chars


@pytest.fixture(autouse=True)
def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", CRON)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    yield
    reset_container()
```

to:

```python
"""Cancel reconciliation job: apply the final `cancelled` tag once Shopify confirms cancelledAt,
and notify the customer with cod_cancel once that happens."""

import httpx
import pytest

from app.admin.controls import AdminControls, save_controls
from app.channels.whatsapp_sender import SendResult
from app.core.order_resolver import authorize_own_order
from app.deps import get_container, reset_container
from app.jobs.reconcile import run_reconcile_cancels
from app.shopify.models import AuthorizedOrder, CancelRequested, Customer, Order

CRON = "topsecret-reconcile-1"  # >= 16 chars


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", CRON)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", "sec")
    await c.config.set_secret("whatsapp:verify_token", "ver")
    await c.config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    await save_controls(c.config, AdminControls(send_mode="live"))
    yield
    reset_container()


class FakeSender:
    """Records send_template calls made by reconcile.py's cod_cancel notification."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append({"to": to, "template": template_name, "language": language,
                            "body_params": list(body_params)})
        return SendResult(ok=True, status_code=200, wamid="wamid.cancel", error=None)


def _install_sender(monkeypatch: pytest.MonkeyPatch) -> FakeSender:
    sender = FakeSender()
    monkeypatch.setattr("app.jobs.reconcile.send_template", sender)
    return sender
```

Note: `save_controls` must already exist for `AdminControls` (used elsewhere, e.g. `test_outbox_drain_job.py`) — reuse it, don't reimplement.

Now update `_order()` to optionally carry a `Customer` (needed to test `customer_display_name`'s non-placeholder branch):

```python
def _order(
    gid: str, phone: str | None = "+919664290413", cancelled_at: str | None = None,
    customer: Customer | None = None,
) -> Order:
    return Order(
        gid=gid, name="tavas1", email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, customer=customer,
    )
```

Add the new test after `test_cancelled_order_gets_final_tag` (currently ends around line 84):

```python
async def test_cancelled_order_sends_cod_cancel_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    customer = Customer(
        gid="gid://shopify/Customer/1", first_name="Suman", last_name="B", email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=None, postal_code=None,
        country=None,
    )
    shopify = FakeShopify(
        {GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z", customer=customer)}
    )
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert sender.calls == [
        {"to": "+919664290413", "template": "cod_cancel", "language": "en",
         "body_params": ["Suman B", "tavas1"]}
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_reconcile_cancels.py::test_cancelled_order_sends_cod_cancel_notification -v`
Expected: FAIL — `sender.calls` stays empty, `run_reconcile_cancels` never sends anything today.

- [ ] **Step 3: Implement the `cod_cancel` notification**

Replace the full contents of `backend/app/jobs/reconcile.py` with:

```python
"""Cancel reconciliation job — apply the FINAL `cancelled` tag once Shopify confirms, and notify
the customer with `cod_cancel` once that happens.

``orderCancel`` is async: the button-dispatch path (``core.order_actions``) requests it and writes
only the PROVISIONAL ``cancel_requested`` tag + mapping status, with a deliberately soft plain-text
reply ("we have requested cancellation..."). This job re-fetches each order still in
``cancel_requested`` and, only once Shopify actually reports it cancelled (``cancelledAt`` set),
applies the final ``cancelled`` tag, advances the mapping, and sends the ``cod_cancel`` template
(whose wording asserts completion, so it must never fire before this point). A false ``cancelled``
tag is therefore never written before Shopify has really cancelled the order (ADR-004 #3). An
order not yet cancelled is left in place for the next run; a transient Shopify error on any order
never aborts the whole job. The notification is best-effort: a missing WhatsApp config or a
transport failure is logged and skipped, never rolls back the tag/status write, and never aborts
the job for other orders in the batch.
"""

import logging
from typing import Any

from app.admin.controls import AdminControls, load_controls
from app.channels.shopify_orders import customer_display_name
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_sender import WhatsAppSendError, send_template
from app.config.crypto import VaultError
from app.core.order_resolver import authorize_own_order
from app.core.send_policy import send_decision
from app.deps import Container
from app.shopify.errors import ShopifyError
from app.shopify.models import AuthorizedOrder

logger = logging.getLogger("app.jobs.reconcile")

_RECONCILE_LIMIT = 50
_CANCEL_TEMPLATE = "cod_cancel"
# cod_cancel is Meta-approved in `en` ONLY (checked live during planning, same situation as
# cod_confirmmsg/cod_confirmation/prepaid_order, Q19c) -- pinned regardless of the order's language.
_CANCEL_TEMPLATE_LANGUAGE = "en"


async def _notify_cancelled(
    c: Container, cfg: WhatsAppConfig, controls: AdminControls, auth: AuthorizedOrder
) -> None:
    if send_decision(controls.send_mode, controls.allowlist_phones, auth.verified_phone) == "suppress":
        return
    try:
        await send_template(
            c.http, cfg, auth.verified_phone, _CANCEL_TEMPLATE, _CANCEL_TEMPLATE_LANGUAGE,
            [customer_display_name(auth.order), auth.order.name],
        )
    except WhatsAppSendError:
        logger.warning("reconcile cancels: cod_cancel notification failed (transport)")


async def run_reconcile_cancels(c: Container) -> dict[str, Any]:
    controls = await load_controls(c.config)
    # Loaded ONCE per run (not per order) -- config doesn't change mid-run, and a corrupt/missing
    # WhatsApp config must never crash this job (which has never touched WhatsApp before): it just
    # means notifications are skipped for this run while reconciliation proceeds normally.
    try:
        cfg = await load_whatsapp_config(c.config)
    except VaultError:
        logger.warning(
            "reconcile cancels: whatsapp config unreadable; notifications skipped this run"
        )
        cfg = None
    gids = await c.ingest.orders_awaiting_cancel_reconcile(_RECONCILE_LIMIT)
    reconciled = pending = skipped = 0
    for gid in gids:
        try:
            order = await c.shopify.get_order(gid)
            if order is None:
                skipped += 1
                continue
            if not order.is_cancelled():
                pending += 1  # still awaiting Shopify -> leave for the next run
                continue
            auth = authorize_own_order(order)
            if auth is None:
                # No phone on the order to satisfy the ownership invariant -> cannot tag it.
                skipped += 1
                continue
            await c.shopify.add_tags(auth, controls.tags.cancelled)
            await c.ingest.set_mapping_status(gid, "cancelled")
            await c.ingest.record_order_action(gid, "cancelled", "system", None, "ok", None)
            if cfg is not None:
                await _notify_cancelled(c, cfg, controls, auth)
            reconciled += 1
        except ShopifyError:
            # Transient Shopify failure on this order: it stays in cancel_requested (status not
            # advanced) so the next run retries it.
            logger.warning("reconcile cancels: shopify error for %s (will retry next run)", gid)
            pending += 1
    return {
        "checked": len(gids),
        "reconciled": reconciled,
        "pending": pending,
        "skipped": skipped,
    }
```

- [ ] **Step 4: Run test to verify it passes, plus the full reconcile suite**

Run: `cd backend && python -m pytest tests/test_reconcile_cancels.py -v`
Expected: PASS — the new test, and every pre-existing test (`test_not_yet_cancelled_left_untouched`, `test_missing_order_skipped`, `test_cancelled_order_without_phone_skipped`, `test_job_registered_and_runs_via_cron_endpoint`) still passes since none of them reach the notification branch (not cancelled / no order / no phone / nothing to reconcile).

- [ ] **Step 5: Write the failing test for `send_mode` suppression**

Add after the new happy-path test:

```python
async def test_reconciled_notification_suppressed_by_send_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    await save_controls(c.config, AdminControls(send_mode="off"))
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    # The kill switch affects notification only -- the tag/status write still happens.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert sender.calls == []
```

- [ ] **Step 6: Run test to verify it fails, then passes**

Run: `cd backend && python -m pytest tests/test_reconcile_cancels.py::test_reconciled_notification_suppressed_by_send_mode -v`
Expected: FAIL first (before Step 3's implementation existed this test couldn't even be written meaningfully — but since Step 3 is already implemented from Steps 3-4 above, this should already PASS once added; if it fails, it means `send_decision`'s `"off"` handling isn't wired correctly — re-check the `_notify_cancelled` gate).
If it fails: re-verify `_notify_cancelled`'s `send_decision(...)` call happens BEFORE the `send_template` call, and that `"off"` maps to `"suppress"` (confirmed in `app/core/send_policy.py` — `off` falls through to the final `return "suppress"`).
Expected after fix (if any needed): PASS.

- [ ] **Step 7: Write the failing test for a corrupted WhatsApp config**

Add after the suppression test:

```python
async def test_reconcile_survives_corrupt_whatsapp_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    # Simulate a master-key rotation / corrupted encrypted secret (same technique used in
    # test_whatsapp_webhook.py's VaultError tests): a raw config_repo write bypasses encryption,
    # so decrypt later raises VaultError.
    await c.config_repo.set("whatsapp:app_secret", "gAAAAAcorrupt")
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    # Reconciliation (the primary job) is unaffected by a broken WhatsApp config -- only the
    # notification is skipped.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert sender.calls == []
```

- [ ] **Step 8: Run test to verify it fails, then passes**

Run: `cd backend && python -m pytest tests/test_reconcile_cancels.py::test_reconcile_survives_corrupt_whatsapp_config -v`
Expected: this should already PASS given Step 3's `try/except VaultError` around `load_whatsapp_config`. If it instead raises `VaultError` out of the test, re-check that `load_whatsapp_config` is called inside the `try` block (not `load_controls`, which must stay outside — `AdminControls`' tags are needed regardless of WhatsApp config health).

- [ ] **Step 9: Write the failing test for a transport failure during the send itself**

This is distinct from Step 7's corrupted-config test: here the config is healthy and `send_template` is actually called, but the call raises `WhatsAppSendError` (e.g. a network timeout) — `_notify_cancelled`'s own `try/except` must swallow it without affecting the reconciled count. Add after the corrupt-config test:

```python
async def test_reconcile_survives_transport_failure_during_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.whatsapp_sender import WhatsAppSendError

    c = get_container()

    async def _raising_send_template(*args, **kwargs):
        raise WhatsAppSendError("timed out")

    monkeypatch.setattr("app.jobs.reconcile.send_template", _raising_send_template)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    # The tag/status write already happened before the notification attempt -- a transport
    # failure sending cod_cancel must not undo it or affect the job's own success count.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert c.ingest._mapping_status[GID] == "cancelled"  # type: ignore[attr-defined]
```

- [ ] **Step 10: Run test to verify it fails, then passes**

Run: `cd backend && python -m pytest tests/test_reconcile_cancels.py::test_reconcile_survives_transport_failure_during_notification -v`
Expected: this should already PASS given Step 3's `try/except WhatsAppSendError` inside `_notify_cancelled`. If it instead raises `WhatsAppSendError` out of the test, re-check that the `send_template` call in `_notify_cancelled` is wrapped in its own `try/except`, distinct from the outer per-order `except ShopifyError` in `run_reconcile_cancels` (a `WhatsAppSendError` is not a `ShopifyError` subtype, so the outer handler would NOT catch it — it must be caught locally in `_notify_cancelled`).

- [ ] **Step 11: Run the full reconcile suite one more time**

Run: `cd backend && python -m pytest tests/test_reconcile_cancels.py -v`
Expected: PASS — all 9 tests (5 original + 4 new).

- [ ] **Step 12: Run mypy + ruff + secrets grep on touched files**

Run:
```bash
cd backend
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/jobs/reconcile.py
```
Expected: mypy clean, ruff clean, grep returns nothing.

- [ ] **Step 13: Confirm `order_actions.py` is untouched by this task**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: no output relative to the state after Task 3's commit (this task doesn't touch it at all).

- [ ] **Step 14: Run the FULL test suite to confirm no cross-task regressions**

Run: `cd backend && python -m pytest -q`
Expected: PASS, with a total count higher than the pre-feature baseline (verify against the last known-good count reported before this feature started — check `docs/FR/_pipeline_status.md`'s most recent entry for the current baseline number).

- [ ] **Step 15: Commit**

```bash
git add backend/app/jobs/reconcile.py backend/tests/test_reconcile_cancels.py
git commit -m "feat(whatsapp): send cod_cancel from reconcile once cancellation is confirmed"
```

---

## Post-Implementation

After all four tasks are committed:
- Update `docs/FR/_pipeline_status.md` and `docs/memory/{component_registry,api_registry,error_learnings}.md` per this repo's standing protocol (the `developer` agent handles this as part of its normal workflow).
- Route to `code-reviewer`, then `security-reviewer` (this touches the outbound-send path and a job that sends WhatsApp messages for the first time — sensitive surface per the routing rules), per `.claude/rules/common/agents.md`.
- Do NOT push — commits stay local until the owner approves, per this repo's standing rule.
