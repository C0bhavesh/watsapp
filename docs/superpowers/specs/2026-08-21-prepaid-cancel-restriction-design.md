# Prepaid Order Cancel Restriction — Design

> Owner-directed. Approved 2026-08-21.

## Problem

The owner wants prepaid orders to never be cancellable — not by the bot, and not by the customer at all, regardless of dispatch status. Cancel eligibility today is gated ONLY on dispatch/fulfillment status and whether the order is already cancelled; no payment-method check exists anywhere in the cancel chain.

## Current state (traced, not assumed)

Two of the four places a cancel button could reach a customer already exclude prepaid orders structurally, for an unrelated reason (WABA template approval), and need no change:
- `app/channels/shopify_webhook.py:472-495` — only the `cod_confirmation` template has approved Confirm/Cancel buttons; `prepaid_order` has none, so the initial order-push message never carries a cancel button for a prepaid order.
- `app/admin/router.py:1268-1271` — the admin's manual resend gates buttons on `tmpl.has_confirm_cancel_buttons`, same template-level restriction.

Two places have the actual gap and need the fix:
- `app/agents/order_tracking.py::_is_cancel_eligible()` (line 86-99) — checks only `is_cancelled()` and fulfillment status. This value is injected into the LLM's per-order context ("cancel eligible: True/False"); the system prompt (line 41) tells the LLM to offer bringing up a Confirm/Cancel button when eligible. A prepaid, undispatched order currently reads as eligible.
- `app/core/order_actions.py::_handle_cancel_request` (the deterministic mutation gate — explicitly documented in this file as "the mutation-safety core (Flow B)") — checks only `is_cancelled()` and `_CANCELLABLE_FULFILLMENT`. No payment-method check exists at the actual point of mutation.

`Order.is_cod()` already exists (`app/shopify/models.py:102`), checking `payment_gateway_names`/`tags` for "cash on delivery"/"cod". A prepaid order is simply `not order.is_cod()`.

## Decision (owner-confirmed)

Fix both places, not just the conversational one:
1. **`order_tracking.py`** — stops the bot from ever telling a prepaid customer that cancellation is possible.
2. **`order_actions.py`** — the actual mutation gate independently refuses a prepaid order too, even if a cancel button somehow reached the customer through some future or unforeseen path. This matches the codebase's existing safety convention: re-verify at the point of mutation, never trust what triggered it (the same reasoning already applied to `_CANCELLABLE_FULFILLMENT`, and to CRITICAL RULE 3's "always re-fetch order state from Shopify before acting").

## Changes

**`app/agents/order_tracking.py`:**
- `_is_cancel_eligible()` gains `and order.order.is_cod()` — a prepaid order is never eligible, regardless of dispatch status.
- The system prompt's cancellation-policy paragraph (currently lines 26-28, dispatch-only) gains an explicit prepaid clause, so the LLM can state the reason plainly rather than just going quiet on the topic: prepaid orders cannot be cancelled once placed; only Cash on Delivery orders can be cancelled, and only before dispatch.

**`app/core/order_actions.py::_handle_cancel_request`:**
- Before the existing `is_cancelled()` / fulfillment checks proceed to show the "are you sure?" Confirm/Cancel buttons, add an `is_cod()` check. If the order is prepaid, send the new refusal copy and stop — same early-return pattern already used for `already_cancelled` and `cancel_too_late`.

**`app/channels/copy.py`:** new key `cancel_not_available_prepaid`, matching the existing `cancel_too_late`/`already_cancelled` four-language pattern (en/hi/hinglish/gu):

> en: "This order was prepaid, so it can't be cancelled once placed. Please contact our support team for help."
> hi: "यह ऑर्डर प्रीपेड था, इसलिए इसे ऑर्डर देने के बाद कैंसल नहीं किया जा सकता। कृपया सहायता के लिए हमारी टीम से संपर्क करें।"
> hinglish: "Yeh order prepaid tha, isliye order place hone ke baad ise cancel nahi kar sakte. Please help ke liye hamari team se baat karein."
> gu: "આ ઓર્ડર પ્રીપેડ હતો, તેથી ઓર્ડર આપ્યા પછી તેને કેન્સલ કરી શકાતો નથી. કૃપા કરીને મદદ માટે અમારી ટીમનો સંપર્ક કરો."

## Scope

No change to `shopify_webhook.py` or `admin/router.py` — their existing template-level gating already covers this correctly for an unrelated reason and needs no touching. No change to `is_cod()` itself — it's reused as-is. No change to COD cancellation behavior (still cancellable before dispatch, exactly as today).

## Testing

- `order_tracking.py`: a test asserting `_is_cancel_eligible` returns `False` for a prepaid, undispatched order (currently would return `True` — this is the regression this whole change closes) and still returns `True` for a COD, undispatched order (unchanged behavior, must not regress).
- `order_actions.py`: a test simulating a `order:cancel:{gid}` tap against a prepaid order, asserting the customer receives the new refusal copy, no Confirm/Cancel buttons are sent, and no `orderCancel` mutation is attempted (mirrors this file's existing `already_cancelled`/`cancel_too_late` test pattern).
- `copy.py`: the new key follows the same test convention already covering the other cancel-related copy keys (e.g. all four languages present, non-empty).

## Correction (append-only, 2026-08-22 — security review)

Two claims in the "Current state" and "Scope" sections above turned out to be inaccurate; the security review of the implemented feature caught them. Recorded here rather than rewritten, matching this project's append-only correction convention (as with `docs/memory/error_learnings.md`).

1. **The admin manual-resend claim was WRONG.** "Current state" bullet 2 and "Scope" both asserted that `app/admin/router.py`'s manual resend "already excludes prepaid orders from ever seeing a cancel button" via template-level gating (`tmpl.has_confirm_cancel_buttons`). It does not. The resend list is built through `_template_applies_to_order` (`router.py:1157`), which filtered only on `is_cancelled()` / fulfillment presence — NOT payment method. So an admin resending `cod_confirmation` (the one template with `has_confirm_cancel_buttons`) for a PREPAID order would have emitted `order:cancel:{gid}` buttons to that customer. The `has_confirm_cancel_buttons` flag gates whether a template *class* can ever carry buttons, not whether *this order* should be offered that template. Fix #2 below closes the gap: `_template_applies_to_order` now excludes `cod_confirmation` when `not order.is_cod_by_gateway()`.

2. **`is_cod()` was not a safe gate for the cancel mutation.** The design used `order.is_cod()` for the eligibility/mutation gate. `is_cod()` returns True if EITHER Shopify's `payment_gateway_names` contains "cash on delivery" OR any order *tag* equals "cod". The tag arm is app-writable (this app's own `add_tags` calls, plus any third-party app / Shopify Flow), so a stray "cod" tag on a genuinely prepaid order could flip it "cancellable". Fix #1 below adds `Order.is_cod_by_gateway()` (gateway-only) and swaps the THREE cancel-gating call sites (`order_tracking.py::_is_cancel_eligible`, `order_actions.py::_handle_cancel_request`, `order_actions.py::_handle_cancel_confirm`) to it. `is_cod()` itself is unchanged and still used for the lower-stakes "(Cash on Delivery)" display note.
