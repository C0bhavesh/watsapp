# Plain-Text Order Confirm/Cancel Messages — Design

> Owner-directed, 2026-08-20. Root cause context: the async `cod_cancel` template (sent by the `reconcile_cancels` cron job) has never fired in production — that cron was never scheduled after `vercel.json`'s crons block was dropped on 2026-08-14 to unblock deploys on the Vercel Hobby plan. Rather than fix the cron scheduling (a separate deploy-config decision, still open), the owner chose to remove the dependency on templates for these two automatic bot replies entirely.

## Problem

Two automatic WhatsApp replies the bot sends after a button tap currently depend on either a broken async job or a Meta-approved template:
1. **Order confirm tap** (`order:confirm:{gid}`, `_handle_confirm` in `app/core/order_actions.py`) sends the Meta template `cod_confirmmsg` synchronously — this still works today (Meta templates don't need the cron), but the owner wants to simplify it to plain text.
2. **Cancel confirm tap** (`order:cancel:confirm:{gid}`, `_handle_cancel_confirm`) already sends a plain-text reply synchronously ("We have requested cancellation of your order. We will confirm once it is done.") — but that wording implies a LATER confirmation is still coming via the `cod_cancel` template, which never arrives because the job that sends it has never run.

## Decision

Both automatic replies become plain text, sent synchronously (already the case for #2, newly the case for #1) with the order name interpolated, and worded as a completed action (not "we'll confirm later" — the underlying Shopify mutation has already succeeded synchronously by the time either message is sent).

## Scope

**In scope:** `app/core/order_actions.py`'s two automatic message sends (`_handle_confirm`, `_handle_cancel_confirm`) and their `app/channels/copy.py` text.

**Out of scope (explicitly untouched):**
- The `cod_confirmmsg`/`cod_cancel` Meta templates themselves and the admin panel's manual template-resend feature (`app/admin/template_catalog.py`, `admin/router.py`'s `/conversations/{id}/templates`) — an admin can still manually send the fuller template later if they choose.
- The broken `reconcile_cancels` cron/scheduling problem — still an open, separate decision (Vercel Pro vs. external scheduler), not resolved by this change. This change simply removes the CUSTOMER-FACING dependency on that job ever running; the reconcile job's other duties (applying the final `cancelled` tag, updating mapping status) are unaffected and still won't run until the scheduling decision is made.
- The initial order-confirmation push (sent at order-creation time, a different code path, `cod_confirmation`/`order_confirmation` template) — not part of this change.

## Copy changes (`app/channels/copy.py`)

New key `order_confirmed` (replaces the `cod_confirmmsg` template send in `_handle_confirm`), four languages matching the file's existing tone:
- en: "Your order {order_name} has been confirmed. Thank you for shopping with us!"
- hi: "आपका ऑर्डर {order_name} कन्फर्म हो गया है। हमारे साथ शॉपिंग करने के लिए धन्यवाद!"
- hinglish: "Aapka order {order_name} confirm ho gaya hai. Humare saath shopping karne ke liye dhanyawad!"
- gu: "તમારો ઓર્ડર {order_name} કન્ફર્મ થઈ ગયો છે. અમારી સાથે શોપિંગ કરવા બદલ આભાર!"

Existing key `cancel_requested` (used in `_handle_cancel_confirm`, both the fresh-cancel and idempotent-retap branches) reworded to drop "we will confirm once it is done" and interpolate the order name:
- en: "Your order {order_name} has been cancelled."
- hi: "आपका ऑर्डर {order_name} कैंसल कर दिया गया है।"
- hinglish: "Aapka order {order_name} cancel kar diya gaya hai."
- gu: "તમારો ઓર્ડર {order_name} કેન્સલ કરવામાં આવ્યો છે."

Both keys become `.format(order_name=...)`-style templates; `copy_for` itself is unchanged (still returns the raw string — the `.format()` call happens at the `order_actions.py` call site, where `order.name` is already in scope, mirroring how `_handle_confirm` already builds `confirm_params = [customer_display_name(order), order.name]` today for the template call it's replacing).

## Code changes (`app/core/order_actions.py`)

- `_handle_confirm`: both `_safe_send_template(c, cfg, event.wa_id, _COD_CONFIRMMSG_TEMPLATE, confirm_params)` calls (the idempotent re-tap branch and the fresh-confirm branch) become `_safe_send_text(c, cfg, event.wa_id, copy_for("order_confirmed", lang).format(order_name=order.name))`. `confirm_params`/`_COD_CONFIRMMSG_TEMPLATE` become unused in this function — remove `confirm_params`'s construction if nothing else in the function needs it (verify before removing `_COD_CONFIRMMSG_TEMPLATE`'s module-level constant, since it may still be referenced elsewhere, e.g. the admin template catalog — check before deleting).
- `_handle_cancel_confirm`: both `_safe_send_text(c, cfg, event.wa_id, copy_for("cancel_requested", lang))` calls (the idempotent re-tap branch and the fresh-cancel branch) become `_safe_send_text(c, cfg, event.wa_id, copy_for("cancel_requested", lang).format(order_name=order.name))`.

No change to the mutation logic itself (`c.shopify.add_tags`, `c.shopify.cancel_order`, `c.ingest.record_order_action`, `c.ingest.set_mapping_status`), ownership checks, or any other branch (`already_cancelled`, `cancel_too_late`, `cancel_failed`, `cancel_kept`) — those messages don't reference an order name today and the owner didn't ask to change them.

## Testing

Existing tests in `backend/tests/core/test_button_dispatch.py` (or wherever `_handle_confirm`/`_handle_cancel_confirm` are covered — confirm exact file before writing the plan) assert the template send; update them to assert the new text send instead, plus new assertions that the sent text contains the order's `name`. No new test *behavior* beyond confirming the interpolation is correct and the mutation/tag/record-order-action calls are unchanged.
