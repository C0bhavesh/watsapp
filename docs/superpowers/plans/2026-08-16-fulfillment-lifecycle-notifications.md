# Fulfillment Lifecycle Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send `order_shipped` when a fulfillment first gets tracking info, and `order_delivered` when Shopify's own `shipment_status` reports `"delivered"` — closing the gap where `fulfillments/create`/`fulfillments/update` webhooks are received and mirrored today but never trigger any customer notification.

**Architecture:** Generalize the existing single-purpose outbox (built for `cod_confirmation`'s fixed 6-field shape) into a generic envelope any template can use, reusing 100% of the already-shipped dedupe/atomic-claim/inline-send machinery. Detection lives in the existing `fulfillments/create`/`fulfillments/update` webhook branch; template params are sourced from the already-mirrored `Order` (no extra Shopify call).

**Tech Stack:** Python 3.12, FastAPI, pytest + pytest-asyncio, httpx `MockTransport`/monkeypatched fakes, existing `InMemoryIngestStore`/`ConfigService` test fixtures.

## Global Constraints

- Full type hints on every function signature; `mypy app` strict must stay clean (64 files today).
- `ruff check .` clean. No bare `except:`. No `print()` — use the existing `logging.getLogger("app.<module>")` pattern.
- `order_shipped` and `order_delivered` are Meta-approved in `en` ONLY (verified live during planning) — both pinned to `"en"`, never the customer's detected language.
- No live courier-tracking integration (a2ship or otherwise) — this feature only reacts to Shopify's own webhook-delivered `shipment_status`, confirmed present in a real production payload from this store.
- `backend/app/core/order_actions.py` must remain byte-identical throughout — this feature never touches the mutation-safety core. Verify via `git diff` at the end of every task.
- Every new outbound send must respect the `send_mode` kill switch — inherited for free here since `send_inline_outbound` already enforces it internally; no new gating code needed in this feature's own logic.
- Secrets/print/bare-except compliance grep (from `no-secrets.md`) must return empty on every touched file:
  `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" <file>`
- Do not push to git — commit locally only, per this repo's standing rule (owner approves pushes separately).

---

## File Structure

- **Modify** `backend/app/jobs/outbox_drain.py` — generalize `_TemplatePayload`/`_parse_payload`/`send_one_outbound` to a generic `template`/`language`/`body_params`(dict or list)/`image_url`?/`buttons`? envelope instead of `cod_confirmation`'s hardcoded 6 flat keys; the mapping-status-on-success side effect becomes conditional on the dedupe-key family (order-confirmation vs. fulfillment); `send_inline_outbound` gains an optional `timeout` override (defaults to today's constant, zero behavior change for the existing call site).
- **Modify** `backend/app/channels/shopify_webhook.py` — migrate the existing `cod_confirmation`/`prepaid_order` payload construction to the new envelope shape; add fulfillment shipped/delivered detection + notification in the existing `fulfillments/create`/`fulfillments/update` branch.
- **Modify** `backend/app/store/base.py`, `postgres.py`, `memory.py` — widen `enqueue_outbound`'s return type from `bool` to `int | None` (the new row's id, needed so a caller can inline-send it — `ingest_order_created` already does this for the original push; `enqueue_outbound` never needed to until now).
- **Modify** `backend/app/jobs/reminders.py` — one-line adjustment for the widened return type (its own behavior is otherwise unchanged).
- **Test files** (extend existing, no new files): `backend/tests/test_outbox_drain_job.py`, `backend/tests/test_shopify_webhook.py`, `backend/tests/store/test_reminders_store.py`, `backend/tests/store/test_reminders_pg.py`.

---

### Task 1: Generalize the outbox payload envelope

**Files:**
- Modify: `backend/app/jobs/outbox_drain.py:85-160` (`_TemplatePayload`, `_parse_payload`), `:230-247` (buttons/body_params in `send_one_outbound`), `:289-292` (mapping-status side effect)
- Modify: `backend/app/channels/shopify_webhook.py:333-353` (`orders/create` payload construction)
- Test: `backend/tests/test_outbox_drain_job.py`
- Test: `backend/tests/test_shopify_webhook.py`

**Interfaces:**
- Produces: `payload_json` envelope shape `{"template": str, "language": str, "body_params": dict[str,str] | list[str], "image_url"?: str, "buttons"?: list[str]}` — the shape every future outbox row (including Task 3's fulfillment notifications) must write.
- Produces: `_TemplatePayload(template: str, language: str, body_params: dict[str,str] | list[str], image_url: str | None, buttons: list[str])` — consumed by Task 3 indirectly (Task 3 never reads `_TemplatePayload` directly, only writes payload_json in the shape above).
- Consumed by: Task 3 (writes `payload_json` in this shape for `fulfillment_shipped`/`fulfillment_delivered` rows).

- [ ] **Step 1: Write the failing tests for the generalized `_parse_payload`**

Add to `backend/tests/test_outbox_drain_job.py`, right after the `_install_sender` helper (currently ends around line 85 — read the file first to confirm the exact insertion point):

```python
async def test_positional_body_params_send_without_parameter_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Fulfillment/1"
    await _seed_row(gid, payload={
        "template": "order_shipped", "language": "en",
        "body_params": ["Bhavesh", "tavas4119", "Delhivery", "https://track/AWB1"],
    }, dedupe_key=f"fulfillment_shipped:{gid}")
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_one_outbound(c, cfg, controls, row)

    assert outcome == "sent"
    assert sender.calls[0]["body_params"] == [
        "Bhavesh", "tavas4119", "Delhivery", "https://track/AWB1",
    ]
    assert sender.calls[0]["button_payloads"] == []


async def test_row_without_buttons_field_sends_no_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Fulfillment/2"
    await _seed_row(gid, payload={
        "template": "order_delivered", "language": "en",
        "body_params": ["Bhavesh", "tavas4120"],
    }, dedupe_key=f"fulfillment_delivered:{gid}")
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_one_outbound(c, cfg, controls, row)

    assert outcome == "sent"
    assert sender.calls[0]["button_payloads"] == []
    # A fulfillment notification's success must NOT touch order_mappings.status -- it has nothing
    # to do with the confirm/cancel state machine (unlike order_created:/order_reminder: rows).
    mappings = {m.order_gid: m for m in await c.ingest.recent_mappings(10)}
    assert gid not in mappings


async def test_cod_confirmation_buttons_now_come_from_the_payload_not_a_template_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression proof for the button-derivation change: cod_confirmation still gets buttons, but
    # now because payload.buttons carries them, not a `template == "cod_confirmation"` string check.
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en",
        "body_params": {"customer_name": "Suman", "order_id": "tavas1"},
        "buttons": [f"order:confirm:{gid}", f"order:cancel:{gid}"],
    })
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_one_outbound(c, cfg, controls, row)

    assert outcome == "sent"
    assert sender.calls[0]["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py -k "positional_body_params or without_buttons_field or buttons_now_come_from" -v`
Expected: FAIL. `_parse_payload` today requires the 6 flat keys (`customer_name`, `order_id`, etc.) at the top level — a payload with only `template`/`language`/`body_params` fails to parse (`None`), so every new test's `send_one_outbound` call returns `"undeliverable"`, not `"sent"`.

- [ ] **Step 3: Update `FakeSender` and `_seed_row` in the test file to match the new envelope**

In `backend/tests/test_outbox_drain_job.py`, change `FakeSender.__call__` (currently around lines 40-54) from:

```python
    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {
                "to": to,
                "template": template_name,
                "language": language,
                "body_params": dict(body_params),
                "button_payloads": list(button_payloads),
                "header_image_url": header_image_url,
                "timeout": timeout,
            }
        )
```

to:

```python
    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {
                "to": to,
                "template": template_name,
                "language": language,
                # Stored AS-IS (not coerced to dict): body_params is either a named dict
                # (cod_confirmation/prepaid_order) or a positional list (order_shipped/
                # order_delivered), matching send_template's own dual-mode support.
                "body_params": body_params,
                "button_payloads": list(button_payloads),
                "header_image_url": header_image_url,
                "timeout": timeout,
            }
        )
```

Change `_seed_row`'s default payload (currently around lines 62-71) from:

```python
async def _seed_row(gid: str, phone: str = PHONE, payload: dict[str, str] | None = None,
                    dedupe_key: str | None = None) -> None:
    c = get_container()
    payload = payload or {
        "template": "cod_confirmation", "language": "en",
        "customer_name": "Suman", "order_id": "tavas3733",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
        "image_url": "https://cdn.shopify.com/s/files/1/x.jpg",
    }
```

to:

```python
async def _seed_row(gid: str, phone: str = PHONE, payload: dict[str, object] | None = None,
                    dedupe_key: str | None = None) -> None:
    c = get_container()
    payload = payload or {
        "template": "cod_confirmation", "language": "en",
        "body_params": {
            "customer_name": "Suman", "order_id": "tavas3733",
            "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
            "product_amount": "949",
        },
        "image_url": "https://cdn.shopify.com/s/files/1/x.jpg",
        "buttons": [f"order:confirm:{gid}", f"order:cancel:{gid}"],
    }
```

(`dict[str, object]` because `body_params` can now be a nested `dict` or `list`, not a flat `str` value — widen the type hint accordingly.)

- [ ] **Step 4: Update the 4 existing tests that build a custom flat-shape payload**

These four tests in `backend/tests/test_outbox_drain_job.py` currently pass a flat 6-key `payload=` dict — each needs its payload restructured to nest the product fields under `body_params`. Read the file first to find their exact current line numbers (they may have shifted slightly), then apply these exact changes:

`test_prepaid_order_row_sends_with_no_buttons` — change:
```python
    await _seed_row(gid, payload={
        "template": "prepaid_order", "language": "en",
        "customer_name": "Suman", "order_id": "tavas3734",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
    })
```
to:
```python
    await _seed_row(gid, payload={
        "template": "prepaid_order", "language": "en",
        "body_params": {
            "customer_name": "Suman", "order_id": "tavas3734",
            "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
            "product_amount": "949",
        },
        # No "buttons" key at all -- prepaid_order has no Confirm/Cancel component on the WABA.
    })
```

`test_payload_without_image_url_sends_no_header` — change:
```python
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en", "customer_name": "A",
        "order_id": "tavas1", "product_name": "P", "product_color": "C",
        "product_size": "S", "product_amount": "10",
    })
```
to:
```python
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en",
        "body_params": {
            "customer_name": "A", "order_id": "tavas1", "product_name": "P",
            "product_color": "C", "product_size": "S", "product_amount": "10",
        },
    })
```

`test_non_https_image_url_is_dropped` — change:
```python
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en", "customer_name": "A",
        "order_id": "tavas1", "product_name": "P", "product_color": "C",
        "product_size": "S", "product_amount": "10", "image_url": "http://insecure/x.jpg",
    })
```
to:
```python
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en",
        "body_params": {
            "customer_name": "A", "order_id": "tavas1", "product_name": "P",
            "product_color": "C", "product_size": "S", "product_amount": "10",
        },
        "image_url": "http://insecure/x.jpg",
    })
```

`test_media_error_without_image_does_not_retry` — change:
```python
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en", "customer_name": "A",
        "order_id": "tavas1", "product_name": "P", "product_color": "C",
        "product_size": "S", "product_amount": "10",
    })
```
to (identical restructure as `test_payload_without_image_url_sends_no_header` above):
```python
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en",
        "body_params": {
            "customer_name": "A", "order_id": "tavas1", "product_name": "P",
            "product_color": "C", "product_size": "S", "product_amount": "10",
        },
    })
```

Leave `test_legacy_old_shape_payload_is_undeliverable` UNCHANGED — its payload (`template`, `language`, `customer_name`, `order_name`, `amount`, no `body_params` key at all) already lacks the new required `body_params` key, so it will correctly keep failing to parse under the new rules too (still proving the same "legacy shape -> undeliverable" behavior, now for a different reason that happens to produce the identical observable outcome).

- [ ] **Step 5: Implement the generalized `_TemplatePayload`/`_parse_payload`**

In `backend/app/jobs/outbox_drain.py`, replace the `_REQUIRED_PAYLOAD_KEYS` constant and `_TemplatePayload`/`_parse_payload` (currently lines 85-159) with:

```python
# Every payload_json must carry these three top-level keys; body_params is a NAMED dict (Meta
# named-placeholder templates, e.g. cod_confirmation) or a POSITIONAL list (Meta {{n}}-placeholder
# templates, e.g. order_shipped/order_delivered) -- mirrors send_template's own Mapping | Sequence
# duality. image_url and buttons are both optional (absence means "no header" / "no buttons").
_REQUIRED_PAYLOAD_KEYS = ("template", "language")


@dataclass(frozen=True)
class _TemplatePayload:
    template: str
    language: str
    body_params: dict[str, str] | list[str]
    image_url: str | None = None
    buttons: list[str] = field(default_factory=list)


def _gid_from_dedupe_key(dedupe_key: str) -> str | None:
    """Strip a known dedupe_key prefix and return the trailing id; else None.

    The trailing id's MEANING differs by prefix family: for 'order_created:'/'order_reminder:' it
    is the ORDER gid (used below to advance order_mappings.status on a successful send); for
    'fulfillment_shipped:'/'fulfillment_delivered:' it is the FULFILLMENT gid (used only as an
    opaque validity check here -- the fulfillment-notification success path has no mapping-status
    side effect at all, see the dedupe-family gate near the end of send_one_outbound). Any
    prefix in _DEDUPE_PREFIXES is accepted; an unrecognized prefix (corrupt/garbage dedupe_key)
    returns None, which send_one_outbound treats as a terminal bad_dedupe_key failure.
    """
    for prefix in _DEDUPE_PREFIXES:
        if dedupe_key.startswith(prefix):
            gid = dedupe_key[len(prefix):]
            return gid if gid.startswith("gid://") else None
    return None


def _parse_payload(payload_json: str) -> _TemplatePayload | None:
    """Parse the generic outbox payload envelope; None on any bad/missing/malformed field.

    None -> the row is marked undeliverable (it can never render), which also terminally drops any
    legacy row still queued under an OLDER flat-key shape (no top-level ``body_params``) -- an
    already-retired template shape, so retrying it forever would be pointless.
    ``image_url`` is optional: only a public https link is kept (Meta rejects anything else),
    otherwise the header is simply skipped (Q19a graceful degradation). ``buttons`` is optional:
    absence or a non-list value means no button components (e.g. prepaid_order, order_shipped,
    order_delivered all have no Confirm/Cancel component on the WABA).
    """
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fields: dict[str, str] = {}
    for key in _REQUIRED_PAYLOAD_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return None
        fields[key] = value
    raw_body_params = data.get("body_params")
    body_params: dict[str, str] | list[str]
    if isinstance(raw_body_params, dict):
        if not raw_body_params or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_body_params.items()
        ):
            return None
        body_params = raw_body_params
    elif isinstance(raw_body_params, list):
        if not raw_body_params or not all(isinstance(v, str) for v in raw_body_params):
            return None
        body_params = raw_body_params
    else:
        return None
    image = data.get("image_url")
    image_url = image if isinstance(image, str) and image.startswith("https://") else None
    raw_buttons = data.get("buttons")
    buttons = (
        raw_buttons
        if isinstance(raw_buttons, list) and all(isinstance(b, str) for b in raw_buttons)
        else []
    )
    return _TemplatePayload(
        template=fields["template"],
        language=fields["language"],
        body_params=body_params,
        image_url=image_url,
        buttons=buttons,
    )
```

Add `field` to the existing `from dataclasses import dataclass` import (becomes `from dataclasses import dataclass, field`).

Add the two dedupe-prefix-family constants right after the existing `_DEDUPE_PREFIXES` definition (currently line 45). Change:

```python
_DEDUPE_PREFIXES = ("order_created:", "order_reminder:")
```

to:

```python
# The order-confirmation family (original push + its 1-hour reminder) is the ONLY family whose
# successful send advances order_mappings.status -- see the dedupe-family gate in
# send_one_outbound. Fulfillment notifications (Task 3) share none of that state machine.
_ORDER_CONFIRMATION_DEDUPE_PREFIXES = ("order_created:", "order_reminder:")
_FULFILLMENT_DEDUPE_PREFIXES = ("fulfillment_shipped:", "fulfillment_delivered:")
_DEDUPE_PREFIXES = _ORDER_CONFIRMATION_DEDUPE_PREFIXES + _FULFILLMENT_DEDUPE_PREFIXES
```

- [ ] **Step 6: Update `send_one_outbound`'s button/body_params construction and the mapping-status gate**

In `backend/app/jobs/outbox_drain.py`, replace the button/body_params block (currently lines 230-247) from:

```python
    # prepaid_order has no BUTTONS component approved on the WABA (informational only, no
    # Confirm/Cancel step for a prepaid customer) -- only cod_confirmation gets the quick-reply
    # buttons. Everything else about the send (body params, header image, retry) is identical.
    buttons = (
        [f"order:confirm:{gid}", f"order:cancel:{gid}"] if payload.template == "cod_confirmation"
        else []
    )
    # The cod_confirmation template uses NAMED body params (name -> value); the header image (the
    # live product photo) is pre-resolved at ingest and carried in payload.image_url, or None (fetch
    # failed) -> send with no header rather than block the confirmation.
    body_params = {
        "customer_name": payload.customer_name,
        "order_id": payload.order_id,
        "product_name": payload.product_name,
        "product_color": payload.product_color,
        "product_size": payload.product_size,
        "product_amount": payload.product_amount,
    }
```

to:

```python
    # buttons/body_params now come straight from the payload envelope (Task 1's generalization) --
    # no per-template string check here anymore. Any template with no Confirm/Cancel component on
    # the WABA (prepaid_order, order_shipped, order_delivered) simply carries an empty/absent
    # "buttons" list, which _parse_payload already normalized to [].
    buttons = payload.buttons
    body_params = payload.body_params
```

Update the `send_one_outbound` docstring's line "Nothing here mutates a Shopify order" paragraph is unaffected; but the mapping-status side effect (currently lines 289-292) changes from:

```python
    if result.ok:
        await c.ingest.mark_outbound_sent(row.id, result.wamid)
        await c.ingest.set_mapping_status(gid, "template_sent")
        return OUTCOME_SENT
```

to:

```python
    if result.ok:
        await c.ingest.mark_outbound_sent(row.id, result.wamid)
        # Only the order-confirmation family (original push + its 1-hour reminder) has anything to
        # do with order_mappings' confirm/cancel state machine. A fulfillment notification's `gid`
        # here is a FULFILLMENT gid, not an order gid -- writing it into order_mappings would be
        # wrong even if it happened to not error, so this is gated on the dedupe_key's own prefix
        # family, not on `gid`'s mere presence.
        if row.dedupe_key.startswith(_ORDER_CONFIRMATION_DEDUPE_PREFIXES):
            await c.ingest.set_mapping_status(gid, "template_sent")
        return OUTCOME_SENT
```

- [ ] **Step 7: Run the outbox drain test suite to verify Steps 1-6 pass**

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py -v`
Expected: PASS — every test in the file, including the 3 new ones from Step 1 and the 4 restructured ones from Step 4.

- [ ] **Step 8: Migrate `shopify_webhook.py`'s existing payload construction to the new envelope**

In `backend/app/channels/shopify_webhook.py`, change the `orders/create` template-params block (currently lines 332-353) from:

```python
        image_url = await _resolve_product_image(c, incoming.product_gid)
        template_name = TEMPLATE_NAME_COD if incoming.is_cod() else TEMPLATE_NAME_PREPAID
        template_params: dict[str, str] = {
            "template": template_name,
            "language": TEMPLATE_LANGUAGE,
            "customer_name": customer_name or EMPTY_PARAM_PLACEHOLDER,
            "order_id": order_name or EMPTY_PARAM_PLACEHOLDER,
            "product_name": incoming.product_name or EMPTY_PARAM_PLACEHOLDER,
            "product_color": incoming.product_color or EMPTY_PARAM_PLACEHOLDER,
            "product_size": incoming.product_size or EMPTY_PARAM_PLACEHOLDER,
            "product_amount": amount or EMPTY_PARAM_PLACEHOLDER,
        }
        # Only carry image_url when resolved: its absence is the drain/inline sender's signal to
        # send with no header (a stored non-https value would be dropped there anyway).
        if image_url is not None:
            template_params["image_url"] = image_url
        outbound = OutboundDraft(
            dedupe_key=f"order_created:{incoming.gid}",
            kind="order_confirmation",
            phone_e164=incoming.phone_e164,
            payload_json=json.dumps(template_params),
        )
```

to:

```python
        image_url = await _resolve_product_image(c, incoming.product_gid)
        template_name = TEMPLATE_NAME_COD if incoming.is_cod() else TEMPLATE_NAME_PREPAID
        template_params: dict[str, object] = {
            "template": template_name,
            "language": TEMPLATE_LANGUAGE,
            "body_params": {
                "customer_name": customer_name or EMPTY_PARAM_PLACEHOLDER,
                "order_id": order_name or EMPTY_PARAM_PLACEHOLDER,
                "product_name": incoming.product_name or EMPTY_PARAM_PLACEHOLDER,
                "product_color": incoming.product_color or EMPTY_PARAM_PLACEHOLDER,
                "product_size": incoming.product_size or EMPTY_PARAM_PLACEHOLDER,
                "product_amount": amount or EMPTY_PARAM_PLACEHOLDER,
            },
        }
        # Only carry image_url when resolved: its absence is the drain/inline sender's signal to
        # send with no header (a stored non-https value would be dropped there anyway).
        if image_url is not None:
            template_params["image_url"] = image_url
        # prepaid_order has no BUTTONS component approved on the WABA -- only cod_confirmation gets
        # the quick-reply buttons, baked in HERE (ingest time, when incoming.gid is known) rather
        # than derived at send time from a template-name string check.
        if template_name == TEMPLATE_NAME_COD:
            template_params["buttons"] = [
                f"order:confirm:{incoming.gid}", f"order:cancel:{incoming.gid}",
            ]
        outbound = OutboundDraft(
            dedupe_key=f"order_created:{incoming.gid}",
            kind="order_confirmation",
            phone_e164=incoming.phone_e164,
            payload_json=json.dumps(template_params),
        )
```

- [ ] **Step 9: Update `shopify_webhook.py`'s existing payload_json assertions in its test file**

In `backend/tests/test_shopify_webhook.py`, every assertion of the shape `params["X"]` where `X` is one of `customer_name`, `order_id`, `product_name`, `product_color`, `product_size`, `product_amount` must become `params["body_params"]["X"]` — `params["template"]`, `params["language"]`, `params["image_url"]` stay as top-level flat access, UNCHANGED (they remain top-level envelope fields, not nested). This is the exact same mechanical rule Step 4 already applied to production code, applied here to test assertions.

Read the file and locate every such assertion (a `grep -n 'params\["' backend/tests/test_shopify_webhook.py` run during planning found matches around lines 90-101, 120-121, 156-159, 181, 1042 — re-verify the exact current line numbers before editing, they may have shifted). Two fully worked examples covering the two distinct shapes present:

Around what was line 88-101 (a full assertion block for the primary `orders/create` test) — change:
```python
    assert params["template"] == "cod_confirmation"
    # cod_confirmation is en-only on the WABA, so the template send is pinned to en even for a
    # hi-IN customer locale (the free-form conversation still uses the customer's language).
    assert params["language"] == "en"
    assert params["customer_name"] == "Suman B"
    assert params["order_id"] == "tavas3733"
    assert params["product_amount"] == "949.00"
    ...
    assert params["product_name"] == "-"
    assert params["product_color"] == "-"
    assert params["product_size"] == "-"
```
to:
```python
    assert params["template"] == "cod_confirmation"
    # cod_confirmation is en-only on the WABA, so the template send is pinned to en even for a
    # hi-IN customer locale (the free-form conversation still uses the customer's language).
    assert params["language"] == "en"
    assert params["body_params"]["customer_name"] == "Suman B"
    assert params["body_params"]["order_id"] == "tavas3733"
    assert params["body_params"]["product_amount"] == "949.00"
    ...
    assert params["body_params"]["product_name"] == "-"
    assert params["body_params"]["product_color"] == "-"
    assert params["body_params"]["product_size"] == "-"
```

Around what was line 156-159 (an image-header-focused test) — change:
```python
    assert params["product_name"] == "Chic Kurta Set"
    assert params["product_color"] == "Cream"
    assert params["product_size"] == "M"
    assert params["image_url"] == "https://cdn.shopify.com/s/files/1/kurta.jpg"
```
to:
```python
    assert params["body_params"]["product_name"] == "Chic Kurta Set"
    assert params["body_params"]["product_color"] == "Cream"
    assert params["body_params"]["product_size"] == "M"
    assert params["image_url"] == "https://cdn.shopify.com/s/files/1/kurta.jpg"
```

Two remaining standalone occurrences (what were lines 181 and 1042) are each exactly this one-line change, in two different test functions — change:
```python
    assert params["product_name"] == "Chic Kurta Set"
    assert "image_url" not in params
```
to:
```python
    assert params["body_params"]["product_name"] == "Chic Kurta Set"
    assert "image_url" not in params
```
(applied independently at both locations — the surrounding lines at each site are unaffected).

Also add ONE new assertion proving buttons now come from the payload. In `test_orders_create_ingests_and_queues` (the primary `orders/create` test — currently lines 82-103, uses `payload()`'s default gid `"gid://shopify/Order/1"`), add this line right after the existing `assert params["language"] == "en"`:
```python
    assert params["buttons"] == [
        "order:confirm:gid://shopify/Order/1", "order:cancel:gid://shopify/Order/1",
    ]
```
And in `test_prepaid_order_routes_to_prepaid_template` (currently lines 106-121, asserts `params["template"] == "prepaid_order"`), add right after the existing `assert params["language"] == "en"`:
```python
    assert "buttons" not in params
```

- [ ] **Step 10: Run the full webhook test suite**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py -v`
Expected: PASS — every test, including the updated assertions from Step 9.

- [ ] **Step 11: Run the full suite + mypy + ruff + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/jobs/outbox_drain.py app/channels/shopify_webhook.py
```
Expected: full suite green, mypy clean, ruff clean, grep empty.

- [ ] **Step 12: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 13: Commit**

```bash
git add backend/app/jobs/outbox_drain.py backend/app/channels/shopify_webhook.py backend/tests/test_outbox_drain_job.py backend/tests/test_shopify_webhook.py
git commit -m "refactor(outbox): generalize payload envelope beyond cod_confirmation's fixed shape"
```

---

### Task 2: Widen `enqueue_outbound` to return the new row's id

**Files:**
- Modify: `backend/app/store/base.py` (Protocol signature)
- Modify: `backend/app/store/postgres.py:743-757` (`enqueue_outbound`)
- Modify: `backend/app/store/memory.py:338-339` (`enqueue_outbound`)
- Modify: `backend/app/jobs/reminders.py:57` (call site)
- Test: `backend/tests/store/test_reminders_store.py`
- Test: `backend/tests/store/test_reminders_pg.py`

**Interfaces:**
- Produces: `IngestStore.enqueue_outbound(outbound: OutboundDraft) -> int | None` — the new row's `id` on a fresh insert, `None` on a dedupe-key conflict (nothing queued). Consumed by Task 3 (`send_inline_outbound(c, outbound_id)` needs exactly this).

- [ ] **Step 1: Write the failing test for the widened return type (in-memory)**

In `backend/tests/store/test_reminders_store.py`, find `test_enqueue_outbound_is_idempotent_on_dedupe_key` (currently around line 141-149) and change:

```python
    assert await store.enqueue_outbound(reminder) is True
    ...
    assert await store.enqueue_outbound(reminder) is False
```

to:

```python
    first_id = await store.enqueue_outbound(reminder)
    assert isinstance(first_id, int)
    ...
    assert await store.enqueue_outbound(reminder) is None
```

(Read the file first to see the exact surrounding lines/variable names before editing — the two `enqueue_outbound` calls are on the same `reminder` object, first insert then a repeat.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/store/test_reminders_store.py::test_enqueue_outbound_is_idempotent_on_dedupe_key -v`
Expected: FAIL — `enqueue_outbound` currently returns `True`/`False`, not an `int`/`None`.

- [ ] **Step 3: Implement the in-memory widening**

In `backend/app/store/memory.py`, change (currently lines 338-339):

```python
    async def enqueue_outbound(self, outbound: OutboundDraft) -> bool:
        return self._enqueue(outbound) is not None
```

to:

```python
    async def enqueue_outbound(self, outbound: OutboundDraft) -> int | None:
        # `_enqueue` already returns the new row's id (or None on a dedupe_key conflict) --
        # `ingest_order_created` has relied on this since the ADR-001 inline-send amendment; this
        # method just needed to stop discarding that value.
        return self._enqueue(outbound)
```

- [ ] **Step 4: Update the Protocol signature**

In `backend/app/store/base.py`, change (currently line 146):

```python
    async def enqueue_outbound(self, outbound: OutboundDraft) -> bool: ...
```

to:

```python
    async def enqueue_outbound(self, outbound: OutboundDraft) -> int | None: ...
```

- [ ] **Step 5: Run the in-memory test to verify it passes**

Run: `cd backend && python -m pytest tests/store/test_reminders_store.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Update the call site in `reminders.py`**

In `backend/app/jobs/reminders.py`, change (currently line 57):

```python
        if await c.ingest.enqueue_outbound(reminder):
            queued += 1
```

to:

```python
        if await c.ingest.enqueue_outbound(reminder) is not None:
            queued += 1
```

- [ ] **Step 7: Run the reminders job test suite**

Run: `cd backend && python -m pytest tests/test_reminders_job.py -v`
Expected: PASS — unchanged behavior (a non-None int is exactly as truthy as the old `True`, so `queued`'s counting logic is unaffected).

- [ ] **Step 8: Write the failing Postgres-gated test for the widened return type**

In `backend/tests/store/test_reminders_pg.py`, find `test_enqueue_outbound_on_conflict_do_nothing` (currently around line 151-160) and change:

```python
    assert await store.enqueue_outbound(reminder) is True
    assert await store.enqueue_outbound(reminder) is False  # UNIQUE dedupe_key = exactly-once
```

to:

```python
    first_id = await store.enqueue_outbound(reminder)
    assert isinstance(first_id, int)
    assert await store.enqueue_outbound(reminder) is None  # UNIQUE dedupe_key = exactly-once
```

- [ ] **Step 9: Implement the Postgres widening**

In `backend/app/store/postgres.py`, change `enqueue_outbound` (currently lines 743-757) from:

```python
    async def enqueue_outbound(self, outbound: OutboundDraft) -> bool:
        # Same ON CONFLICT (dedupe_key) DO NOTHING idempotency ingest_order_created uses: the UNIQUE
        # dedupe_key constraint IS the exactly-once guarantee, so the reminder sweep can run every
        # tick (or overlap) and still queue at most one reminder row per order. Returns whether a
        # fresh row was inserted.
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO outbound_messages (dedupe_key, kind, phone_e164, payload_json)"
                " VALUES ($1, $2, $3, $4) ON CONFLICT (dedupe_key) DO NOTHING",
                outbound.dedupe_key,
                outbound.kind,
                outbound.phone_e164,
                outbound.payload_json,
            )
        return _rows_affected(result) > 0
```

to:

```python
    async def enqueue_outbound(self, outbound: OutboundDraft) -> int | None:
        # Same ON CONFLICT (dedupe_key) DO NOTHING idempotency ingest_order_created uses: the UNIQUE
        # dedupe_key constraint IS the exactly-once guarantee. RETURNING id gives the freshly-queued
        # row's id (None on a conflict) so a caller (e.g. an inline-send-eligible notification) can
        # claim exactly it via claim_outbound_by_id -- ingest_order_created already relies on this
        # same pattern for the original push.
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO outbound_messages (dedupe_key, kind, phone_e164, payload_json)"
                " VALUES ($1, $2, $3, $4) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
                outbound.dedupe_key,
                outbound.kind,
                outbound.phone_e164,
                outbound.payload_json,
            )
```

- [ ] **Step 10: Run the Postgres-gated test (if `TEST_DATABASE_URL` is available)**

Run: `cd backend && TEST_DATABASE_URL=<your-test-db-url> python -m pytest tests/store/test_reminders_pg.py -v`
Expected: PASS if a test database is available; the test correctly SKIPS (not fails) if `TEST_DATABASE_URL` is unset — either outcome is fine, just don't mistake a skip for a failure.

- [ ] **Step 11: Run mypy + ruff + secrets grep on touched files**

Run:
```bash
cd backend
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/store/base.py app/store/postgres.py app/store/memory.py app/jobs/reminders.py
```
Expected: mypy clean, ruff clean, grep empty.

- [ ] **Step 12: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 13: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/app/jobs/reminders.py backend/tests/store/test_reminders_store.py backend/tests/store/test_reminders_pg.py
git commit -m "feat(store): enqueue_outbound returns the new row's id, not just a bool"
```

---

### Task 3: Shipped/delivered detection + notification

**Files:**
- Modify: `backend/app/jobs/outbox_drain.py` (`send_inline_outbound` gains an optional `timeout` parameter)
- Modify: `backend/app/channels/shopify_webhook.py` (new constants, new `_notify_fulfillment_events` function, wired into the existing `fulfillments/create`/`fulfillments/update` branch)
- Test: `backend/tests/test_outbox_drain_job.py`
- Test: `backend/tests/test_shopify_webhook.py`

**Interfaces:**
- Consumes: `send_inline_outbound(c, outbound_id, timeout=...)` (Task 1's/this task's widened signature), `enqueue_outbound(...) -> int | None` (Task 2), `get_mirrored_order(gid) -> Order | None` (existing), `customer_display_name(order) -> str` (existing, from `app.channels.shopify_orders`).
- Produces: no new public interface — this is the feature's leaf behavior.

- [ ] **Step 1: Write the failing test for `send_inline_outbound`'s optional timeout**

Add to `backend/tests/test_outbox_drain_job.py`, right after `test_send_one_outbound_inline_uses_a_short_distinct_timeout` (read the file first to find the current exact line — Task 1 added tests earlier in this same file, so line numbers have shifted):

```python
async def test_send_inline_outbound_accepts_a_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fulfillment notifications (Task 3) pass a SMALLER timeout than the order-confirmation inline
    # send's default, since up to two of them (shipped + delivered) can fire in one webhook
    # invocation and neither has an image-fetch step ahead of it. The default (no override) must
    # stay IDENTICAL to today's behavior for the order-confirmation call site.
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_inline_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_inline_outbound(c, row.id, timeout=1.5)

    assert outcome == "sent"
    assert sender.calls[0]["timeout"] == 1.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py::test_send_inline_outbound_accepts_a_timeout_override -v`
Expected: FAIL — `send_inline_outbound` today accepts only `(c, outbound_id)`, so passing `timeout=1.5` raises `TypeError: send_inline_outbound() got an unexpected keyword argument 'timeout'`.

- [ ] **Step 3: Widen `send_inline_outbound`'s signature**

In `backend/app/jobs/outbox_drain.py`, change the function signature and body (currently around what was lines 333-387, shifted slightly by Task 1's edits — read the file to confirm the exact current lines) from:

```python
async def send_inline_outbound(c: Container, outbound_id: int | None) -> str | None:
    """Send the JUST-QUEUED order-confirmation row inline, bounded by a short timeout.
```

to:

```python
async def send_inline_outbound(
    c: Container, outbound_id: int | None, timeout: float = _INLINE_SEND_TIMEOUT_SECONDS,
) -> str | None:
    """Send the JUST-QUEUED row inline, bounded by ``timeout`` (defaults to
    ``_INLINE_SEND_TIMEOUT_SECONDS``, the order-confirmation call site's existing behavior,
    unchanged for any caller that doesn't pass an override).
```

Update the two internal uses of the module-level constant inside this function's body — change:

```python
        return await asyncio.wait_for(
            send_one_outbound(c, cfg, controls, claim, timeout=_INLINE_SEND_TIMEOUT_SECONDS),
            timeout=_INLINE_SEND_TIMEOUT_SECONDS,
        )
```

to:

```python
        return await asyncio.wait_for(
            send_one_outbound(c, cfg, controls, claim, timeout=timeout),
            timeout=timeout,
        )
```

(The existing `orders/create` call site in `shopify_webhook.py`, `await send_inline_outbound(c, result.outbound_id)`, passes no `timeout` argument, so it keeps using `_INLINE_SEND_TIMEOUT_SECONDS` exactly as before — zero behavior change there, confirmed by Step 1's test asserting the override case, not the default case, which the PRE-EXISTING `test_send_one_outbound_inline_uses_a_short_distinct_timeout`-style tests already cover.)

- [ ] **Step 4: Run test to verify it passes, plus the full outbox drain suite**

Run: `cd backend && python -m pytest tests/test_outbox_drain_job.py -v`
Expected: PASS — all tests, including every test from Task 1.

- [ ] **Step 5: Write the failing tests for shipped/delivered detection**

First, read `backend/tests/test_shopify_webhook.py`'s current `fulfillment_payload` helper and `test_fulfillments_topic_populates_the_mirror` (grounded above at what was lines 305-344) to confirm they haven't shifted from Task 1's edits, then add these tests right after `test_fulfillments_topic_populates_the_mirror`:

```python
async def test_fulfillment_with_tracking_sends_order_shipped() -> None:
    order_gid = "gid://shopify/Order/500"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-500"))

    fulfillment_gid = "gid://shopify/Fulfillment/500"
    fbody = json.dumps(
        fulfillment_payload(order_gid, fulfillment_gid)
    ).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-500"))

    assert resp.status_code == 200
    store = get_container().ingest
    assert f"fulfillment_shipped:{fulfillment_gid}" in store.outbound  # type: ignore[attr-defined]
    draft = store.outbound[f"fulfillment_shipped:{fulfillment_gid}"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "order_shipped"
    assert params["language"] == "en"
    assert params["body_params"] == [
        "Suman B", "tavas3733", "Delhivery", "https://www.delhivery.com/track/AWB0099887766",
    ]
    assert "buttons" not in params
    # No delivered notification -- shipment_status is absent from this payload.
    assert f"fulfillment_delivered:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]


async def test_fulfillment_replay_does_not_re_enqueue_shipped() -> None:
    # A fulfillments/update replay with tracking ALREADY present must not queue a second
    # order_shipped -- the dedupe key (fulfillment_gid-keyed) is the entire guarantee.
    order_gid = "gid://shopify/Order/501"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-501"))

    fulfillment_gid = "gid://shopify/Fulfillment/501"
    fbody = json.dumps(fulfillment_payload(order_gid, fulfillment_gid)).encode()
    await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-501a"))
    await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-501b"))

    store = get_container().ingest
    shipped_rows = [
        k for k in store.outbound  # type: ignore[attr-defined]
        if k == f"fulfillment_shipped:{fulfillment_gid}"
    ]
    assert len(shipped_rows) == 1


async def test_fulfillment_shipment_status_delivered_sends_order_delivered() -> None:
    order_gid = "gid://shopify/Order/502"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-502"))

    fulfillment_gid = "gid://shopify/Fulfillment/502"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "delivered"
    fbody = json.dumps(fpayload).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-502"))

    assert resp.status_code == 200
    store = get_container().ingest
    assert f"fulfillment_delivered:{fulfillment_gid}" in store.outbound  # type: ignore[attr-defined]
    draft = store.outbound[f"fulfillment_delivered:{fulfillment_gid}"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "order_delivered"
    assert params["language"] == "en"
    assert params["body_params"] == ["Suman B", "tavas3733"]
    assert "buttons" not in params
    # This payload ALSO has tracking info (from fulfillment_payload's defaults) -> shipped fires too.
    assert f"fulfillment_shipped:{fulfillment_gid}" in store.outbound  # type: ignore[attr-defined]


async def test_fulfillment_non_delivered_shipment_status_does_not_send_delivered() -> None:
    order_gid = "gid://shopify/Order/503"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-503"))

    fulfillment_gid = "gid://shopify/Fulfillment/503"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "in_transit"
    fbody = json.dumps(fpayload).encode()
    await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-503"))

    store = get_container().ingest
    assert f"fulfillment_delivered:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]


async def test_fulfillment_no_mirrored_order_skips_notification() -> None:
    # A fulfillment for an order this bot never saw orders/create for -- no mirror row, no
    # notification (never a 500; the webhook still acks 200 normally).
    order_gid = "gid://shopify/Order/999999"  # deliberately never posted via orders/create
    fulfillment_gid = "gid://shopify/Fulfillment/999999"
    fbody = json.dumps(fulfillment_payload(order_gid, fulfillment_gid)).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-999999"))

    assert resp.status_code == 200
    store = get_container().ingest
    assert f"fulfillment_shipped:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]


async def test_split_shipment_fulfillments_get_independent_notifications() -> None:
    order_gid = "gid://shopify/Order/504"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-504"))

    gid_a = "gid://shopify/Fulfillment/504a"
    gid_b = "gid://shopify/Fulfillment/504b"
    fbody_a = json.dumps(fulfillment_payload(order_gid, gid_a)).encode()
    fbody_b = json.dumps(fulfillment_payload(order_gid, gid_b)).encode()
    await post(fbody_a, headers(fbody_a, topic="fulfillments/create", webhook_id="wh-f-504a"))
    await post(fbody_b, headers(fbody_b, topic="fulfillments/create", webhook_id="wh-f-504b"))

    store = get_container().ingest
    assert f"fulfillment_shipped:{gid_a}" in store.outbound  # type: ignore[attr-defined]
    assert f"fulfillment_shipped:{gid_b}" in store.outbound  # type: ignore[attr-defined]
```

Also update `fulfillment_payload`'s signature (currently lines 305-320) to accept an explicit `fulfillment_gid` positionally as used above — it already does (`fulfillment_gid: str = "gid://shopify/Fulfillment/1"` is its second parameter), so no change needed there; the new tests above just pass it explicitly rather than relying on the default.

Finally, update the EXISTING `test_fulfillments_topic_populates_the_mirror` (currently lines 323-344) — its default `fulfillment_payload()` already includes tracking info, so this feature now ALSO queues a `fulfillment_shipped` row for it, which its current assertion doesn't expect. Change:

```python
    # The fulfillment delivery itself must NOT queue an outbound (only the original orders/create
    # push exists). A second queued row here would re-send the confirmation template.
    store = get_container().ingest
    assert list(store.outbound) == [f"order_created:{order_gid}"]  # type: ignore[attr-defined]
```

to:

```python
    # The fulfillment mirror write itself never re-sends the ORIGINAL confirmation template -- but
    # it DOES now queue its own order_shipped notification (this feature), since the default
    # fulfillment_payload() already carries tracking info.
    store = get_container().ingest
    fulfillment_gid = fulfillment_payload(order_gid).get("admin_graphql_api_id")
    assert set(store.outbound) == {  # type: ignore[attr-defined]
        f"order_created:{order_gid}", f"fulfillment_shipped:{fulfillment_gid}",
    }
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py -k fulfillment -v`
Expected: FAIL for every new test (no `_notify_fulfillment_events` exists yet, so nothing gets queued beyond the mirror write); FAIL for the updated `test_fulfillments_topic_populates_the_mirror` too (its old assertion no longer matches what Step 5 expects, and the new implementation doesn't exist yet either way).

- [ ] **Step 7: Implement fulfillment detection + notification**

In `backend/app/channels/shopify_webhook.py`, add the import for `customer_display_name` — change the existing `shopify_orders` import block (currently lines 11-19):

```python
from app.channels.shopify_orders import (
    choose_language,
    clip,
    customer_from_webhook_payload,
    fulfillment_from_webhook_payload,
    is_eligible_for_push,
    order_from_webhook_payload,
    parse_order_created,
)
```

to:

```python
from app.channels.shopify_orders import (
    choose_language,
    clip,
    customer_display_name,
    customer_from_webhook_payload,
    fulfillment_from_webhook_payload,
    is_eligible_for_push,
    order_from_webhook_payload,
    parse_order_created,
)
```

Change the `send_inline_outbound` import (currently line 23) — no change needed to the import itself, but note it's now called with an extra `timeout` kwarg at the new call sites below.

Add new module-level constants near the existing `TEMPLATE_NAME_COD`/`TEMPLATE_NAME_PREPAID`/`TEMPLATE_LANGUAGE` block (currently around lines 31-38):

```python
TEMPLATE_NAME_SHIPPED = "order_shipped"
TEMPLATE_NAME_DELIVERED = "order_delivered"
# Both Meta-approved in `en` ONLY (checked live during planning, same situation as every other
# template this app sends) -- pinned regardless of the order's detected language.
FULFILLMENT_TEMPLATE_LANGUAGE = "en"
# Bounds each fulfillment-notification inline send. Smaller than the order-confirmation inline
# send's _INLINE_SEND_TIMEOUT_SECONDS (3.0s in outbox_drain.py) because up to TWO of these can fire
# in one webhook invocation (shipped AND delivered both newly true in the same event) and neither
# has an image-fetch step ahead of it (unlike cod_confirmation) -- 2.0 + 2.0 = 4.0s worst case
# combined, matching the same "leaves real margin under Shopify's 5s ack ceiling" precedent already
# established for the order-confirmation inline path.
_FULFILLMENT_INLINE_SEND_TIMEOUT_SECONDS = 2.0
```

Add the new `_notify_fulfillment_events` function, right after `_mirror_fulfillment` (currently ends around line 133):

```python
async def _notify_fulfillment_events(
    c: Container, order_gid: str, fulfillment: Fulfillment, raw_payload: dict
) -> None:
    """Best-effort: enqueue+inline-send order_shipped/order_delivered when newly triggered by this
    event. Never raises, never blocks the webhook ack -- any failure here degrades to "notification
    not sent this time", never a 500 (mirrors _mirror_fulfillment's own posture).

    Shipped = this fulfillment now has both a tracking company AND a tracking number/url (checked
    on every create/update event; the per-fulfillment dedupe key, not an in-code flag, is what
    prevents re-sending on a replay). Delivered = the RAW webhook payload's shipment_status is
    exactly "delivered" (Shopify's own enum, confirmed present and reliable for this store's
    courier via a real production payload captured during planning -- read directly from
    raw_payload since fulfillment_from_webhook_payload's Fulfillment model intentionally does not
    carry this field, see fulfillment_from_webhook_payload's own docstring).
    """
    is_shipped = bool(fulfillment.tracking_company) and bool(
        fulfillment.tracking_number or fulfillment.tracking_url
    )
    is_delivered = raw_payload.get("shipment_status") == "delivered"
    if not is_shipped and not is_delivered:
        return
    order = await c.ingest.get_mirrored_order(order_gid)
    phone = order.best_phone() if order is not None else None
    if order is None or phone is None:
        logger.info("fulfillment notify: no mirrored order/phone for %s; skipped", order_gid)
        return
    name = customer_display_name(order)
    if is_shipped:
        await _enqueue_and_send_fulfillment_notification(
            c,
            dedupe_key=f"fulfillment_shipped:{fulfillment.gid}",
            phone=phone,
            template=TEMPLATE_NAME_SHIPPED,
            body_params=[
                name,
                order.name,
                fulfillment.tracking_company or EMPTY_PARAM_PLACEHOLDER,
                fulfillment.tracking_url or fulfillment.tracking_number or EMPTY_PARAM_PLACEHOLDER,
            ],
        )
    if is_delivered:
        await _enqueue_and_send_fulfillment_notification(
            c,
            dedupe_key=f"fulfillment_delivered:{fulfillment.gid}",
            phone=phone,
            template=TEMPLATE_NAME_DELIVERED,
            body_params=[name, order.name],
        )


async def _enqueue_and_send_fulfillment_notification(
    c: Container, dedupe_key: str, phone: str, template: str, body_params: list[str]
) -> None:
    draft = OutboundDraft(
        dedupe_key=dedupe_key,
        kind=dedupe_key.split(":", 1)[0],
        phone_e164=phone,
        payload_json=json.dumps(
            {"template": template, "language": FULFILLMENT_TEMPLATE_LANGUAGE,
             "body_params": body_params}
        ),
    )
    outbound_id = await c.ingest.enqueue_outbound(draft)
    await send_inline_outbound(c, outbound_id, timeout=_FULFILLMENT_INLINE_SEND_TIMEOUT_SECONDS)
```

Wire it into the existing fulfillment branch — change (currently lines 279-286):

```python
    if topic in ("fulfillments/create", "fulfillments/update"):
        parsed = fulfillment_from_webhook_payload(payload)
        if parsed is None:
            return JSONResponse({"ok": True, "ignored": True})
        order_gid, fulfillment = parsed
        return JSONResponse(
            {"ok": True, "ignored": not await _mirror_fulfillment(c, order_gid, fulfillment)}
        )
```

to:

```python
    if topic in ("fulfillments/create", "fulfillments/update"):
        parsed = fulfillment_from_webhook_payload(payload)
        if parsed is None:
            return JSONResponse({"ok": True, "ignored": True})
        order_gid, fulfillment = parsed
        mirrored = await _mirror_fulfillment(c, order_gid, fulfillment)
        await _notify_fulfillment_events(c, order_gid, fulfillment, payload)
        return JSONResponse({"ok": True, "ignored": not mirrored})
```

- [ ] **Step 8: Run tests to verify they pass, plus the full webhook suite**

Run: `cd backend && python -m pytest tests/test_shopify_webhook.py -v`
Expected: PASS — every test, including all 6 new ones from Step 5 and the updated `test_fulfillments_topic_populates_the_mirror`.

- [ ] **Step 9: Run the full suite + mypy + ruff + secrets grep**

Run:
```bash
cd backend
python -m pytest -q
python -m mypy app
python -m ruff check .
grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" app/jobs/outbox_drain.py app/channels/shopify_webhook.py
```
Expected: full suite green, mypy clean, ruff clean, grep empty.

- [ ] **Step 10: Confirm `order_actions.py` is untouched**

Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 11: Commit**

```bash
git add backend/app/jobs/outbox_drain.py backend/app/channels/shopify_webhook.py backend/tests/test_outbox_drain_job.py backend/tests/test_shopify_webhook.py
git commit -m "feat(fulfillments): send order_shipped/order_delivered on tracking/shipment_status"
```

---

## Post-Implementation

After all three tasks are committed:
- Update `docs/FR/_pipeline_status.md` and `docs/memory/{component_registry,api_registry,error_learnings}.md` per this repo's standing protocol (the `developer` agent handles this, or route to `doc-updater` after review).
- Route to `code-reviewer`, then `security-reviewer` (this touches the outbound-send path and a webhook handler that now sends messages based on Shopify-supplied `shipment_status` — sensitive surface per the routing rules), per `.claude/rules/common/agents.md`.
- Do NOT push — commits stay local until the owner approves, per this repo's standing rule.
