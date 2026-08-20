# Plain-Text Order Confirm/Cancel Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Meta-template send on order-confirm tap and the vague "we'll confirm later" wording on cancel-confirm tap with direct plain-text messages naming the order, removing the customer-facing dependency on the never-scheduled `reconcile_cancels` cron and on template approval for this specific automatic reply.

**Architecture:** Two `app/channels/copy.py` entries change (`cancel_requested` reworded + order-name placeholder; new `order_confirmed` key). `app/core/order_actions.py`'s `_handle_confirm` and `_handle_cancel_confirm` interpolate `order.name` into the copy and send via the existing `_safe_send_text` helper instead of `_safe_send_template`.

**Tech Stack:** Python 3.12+, pytest + pytest-asyncio.

## Global Constraints

- Full type hints; `mypy app` strict clean; `ruff check .` clean.
- No change to mutation logic, tag writes, `record_order_action`, or `set_mapping_status` calls — only the outbound message changes.
- No change to any OTHER branch's copy (`already_cancelled`, `cancel_too_late`, `cancel_failed`, `cancel_kept`).
- No change to the admin panel's manual template-resend feature, the `cod_confirmmsg`/`cod_cancel` templates themselves, or the `reconcile_cancels` scheduling problem (separate, still-open decision).
- `_COD_CONFIRMMSG_TEMPLATE`'s module-level constant in `order_actions.py` must be checked for other references before removal — do not delete it if anything else in that file still uses it.

---

### Task 1: Plain-text confirm/cancel replies

**Files:**
- Modify: `backend/app/channels/copy.py`
- Modify: `backend/app/core/order_actions.py`
- Test: `backend/tests/core/test_button_dispatch.py`

**Interfaces:**
- Produces: `copy_for("order_confirmed", lang)` and the reworded `copy_for("cancel_requested", lang)`, both `str.format(order_name=...)`-ready templates containing a literal `{order_name}` placeholder. Consumed only within this task (leaf change, no other task).

- [ ] **Step 1: Write the failing tests**

First, read `backend/tests/core/test_button_dispatch.py` in full to find the exact current assertions this task must change — specifically `test_confirm_tags_records_and_status` (~line 279), `test_confirm_idempotent_on_already_confirmed` (~line 293), `test_cancel_confirm_cancels_tags_and_status` (~line 340), `test_cancel_confirm_idempotent_when_provisional_tag_present` (~line 362), `test_cancel_confirm_race_two_taps_cancel_at_most_once` (~line 405), and `test_confirm_template_send_is_pinned_to_en_regardless_of_order_language` (~line 489). Note the exact `_order(...)` helper's fields (does it set `.name`? what value?) and the `Sends`/`sends.last_text`/`sends.last_template` fixture shape — match your new assertions to what's actually there, not a guess.

Update each of the first 5 tests' assertions from `sends.last_text == copy_for("cancel_requested", "en")` (or the template-send equivalent for the confirm tests) to expect the interpolated text — e.g.:

```python
assert sends.last_text == copy_for("cancel_requested", "en").format(order_name=GID_ORDER_NAME)
```

(substitute `GID_ORDER_NAME` with whatever the test's `_order(...)` helper actually sets as the order's `name` field — read it first).

For the two confirm tests, change from asserting `sends.last_template` to asserting `sends.last_text`:

```python
assert sends.last_text == copy_for("order_confirmed", "en").format(order_name=GID_ORDER_NAME)
```

Replace `test_confirm_template_send_is_pinned_to_en_regardless_of_order_language` entirely with a new test proving the OPPOSITE (a real behavior improvement — plain text isn't template-locked to English):

```python
async def test_confirm_text_send_uses_the_orders_own_language(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order(locale="hi-IN"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    # Plain text isn't template-locked to English the way cod_confirmmsg was -- it goes through
    # copy_for's normal language detection like every other reply in this file.
    assert sends.last_text == copy_for("order_confirmed", "hi").format(order_name=GID_ORDER_NAME)
```

(Again, substitute the real order-name value/variable from the file's existing `_order()` helper.)

- [ ] **Step 2: Run tests to verify they fail**

Run (from `backend/`): `python -m pytest tests/core/test_button_dispatch.py -k "confirm or cancel_confirm" -v`
Expected: FAIL — old copy text / template-send assertions no longer match nothing has changed yet in the app code.

- [ ] **Step 3: Update the copy entries**

In `backend/app/channels/copy.py`, change the `cancel_requested` entry:

```python
    "cancel_requested": {
        "en": (
            "We have requested cancellation of your order. "
            "We will confirm once it is done."
        ),
        "hi": (
            "हमने आपके ऑर्डर को कैंसल करने का अनुरोध कर दिया है। "
            "पूरा होने पर हम आपको बता देंगे।"
        ),
        "hinglish": (
            "Humne aapke order ko cancel karne ka request kar diya hai. "
            "Ho jaane par bata denge."
        ),
        "gu": (
            "અમે તમારા ઓર્ડરને કેન્સલ કરવાની વિનંતી કરી છે. "
            "પૂરું થતાં અમે જણાવીશું."
        ),
    },
```

to:

```python
    "cancel_requested": {
        "en": "Your order {order_name} has been cancelled.",
        "hi": "आपका ऑर्डर {order_name} कैंसल कर दिया गया है।",
        "hinglish": "Aapka order {order_name} cancel kar diya gaya hai.",
        "gu": "તમારો ઓર્ડર {order_name} કેન્સલ કરવામાં આવ્યો છે.",
    },
```

Then add a new `order_confirmed` entry immediately before it (or in a sensible nearby spot next to the other order-status keys — match the file's existing key ordering/grouping):

```python
    "order_confirmed": {
        "en": "Your order {order_name} has been confirmed. Thank you for shopping with us!",
        "hi": "आपका ऑर्डर {order_name} कन्फर्म हो गया है। हमारे साथ शॉपिंग करने के लिए धन्यवाद!",
        "hinglish": (
            "Aapka order {order_name} confirm ho gaya hai. "
            "Humare saath shopping karne ke liye dhanyawad!"
        ),
        "gu": "તમારો ઓર્ડર {order_name} કન્ફર્મ થઈ ગયો છે. અમારી સાથે શોપિંગ કરવા બદલ આભાર!",
    },
```

- [ ] **Step 4: Update `_handle_confirm` and `_handle_cancel_confirm`**

In `backend/app/core/order_actions.py`, in `_handle_confirm` (~line 188-206), change both:

```python
        await _safe_send_template(c, cfg, event.wa_id, _COD_CONFIRMMSG_TEMPLATE, confirm_params)
```

occurrences to:

```python
        await _safe_send_text(
            c, cfg, event.wa_id, copy_for("order_confirmed", lang).format(order_name=order.name)
        )
```

`confirm_params = [customer_display_name(order), order.name]` (~line 196) becomes dead — remove that line only if nothing else in the function still reads `confirm_params` (check the full function body first). Check whether `_COD_CONFIRMMSG_TEMPLATE` (module-level constant, ~line 47) is referenced anywhere else in this file or imported elsewhere before removing it — per Global Constraints, leave it in place if anything else still uses it (a quick `grep -rn _COD_CONFIRMMSG_TEMPLATE backend/app` before deleting is the safe check).

In `_handle_cancel_confirm` (~line 230-266), change both:

```python
        await _safe_send_text(c, cfg, event.wa_id, copy_for("cancel_requested", lang))
```

occurrences to:

```python
        await _safe_send_text(
            c, cfg, event.wa_id, copy_for("cancel_requested", lang).format(order_name=order.name)
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/core/test_button_dispatch.py -v`
Expected: all pass, including the updated/new tests.

- [ ] **Step 6: Run the full backend suite + mypy + ruff**

Run: `python -m pytest`
Expected: all pass — check `backend/tests/test_copy.py` in particular, since it may assert something about the `cancel_requested`/copy dict shape that this task's wording change could affect (read it first if any failure appears there).
Run (from `backend/`): `python -m mypy app/channels/copy.py app/core/order_actions.py`
Run: `python -m ruff check app/channels/copy.py app/core/order_actions.py backend/tests/core/test_button_dispatch.py`
Expected: both clean.

- [ ] **Step 7: Secrets-compliance grep + `order_actions.py`-is-the-file-being-changed note**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/channels/copy.py backend/app/core/order_actions.py`
Expected: empty. (Note: unlike every other task this session, `order_actions.py` IS the file being changed here — the usual "confirm `order_actions.py` diff is empty" check does not apply to this task; that check exists to catch UNINTENDED touches to mutation code in unrelated tasks, not to forbid this one, which is deliberately about that file.)

- [ ] **Step 8: Commit**

```bash
git add backend/app/channels/copy.py backend/app/core/order_actions.py backend/tests/core/test_button_dispatch.py
git commit -m "feat(whatsapp): send plain-text order confirm/cancel replies with order name, drop template/cron dependency"
```

---

## Self-review notes (plan author)

- **Spec coverage:** both copy keys reworded/added ✓ Step 3; both `_handle_confirm`/`_handle_cancel_confirm` call sites updated ✓ Step 4; template-pinning test replaced with a language-respecting test (a real behavior improvement, explicitly called for by the design) ✓ Step 1; out-of-scope items (other copy keys, admin template catalog, cron scheduling) explicitly left untouched per Global Constraints.
- **Placeholder scan:** no TBD/TODO; every step has literal, complete code (the two "check before deleting" notes are verification instructions, not placeholders — the code change itself is fully specified).
- **Type consistency:** `copy_for(key, lang).format(order_name=order.name)` is the identical call shape at all 4 call sites (2 in `_handle_confirm`, 2 in `_handle_cancel_confirm`).
- **Scope:** single task — this is one cohesive, atomic change (copy + 2 call sites + their tests) with no natural split point.

## Next steps after Task 1 is done

1. Route to `code-reviewer` (scoped to the 3 touched files).
2. Route to `security-reviewer` — this file is the deterministic mutation-dispatch path for order cancel/confirm (a sensitive surface per `.claude/rules/common/agents.md`), even though this specific change doesn't touch the mutation calls themselves; run it for consistency with how this file has been treated elsewhere in this project.
3. `doc-updater`: update `docs/memory/component_registry.md`'s `order_actions`/copy entries, `docs/FR/_pipeline_status.md`.
4. No schema migration — nothing for the owner to run in Supabase.
5. Owner reviews → push after approval.
