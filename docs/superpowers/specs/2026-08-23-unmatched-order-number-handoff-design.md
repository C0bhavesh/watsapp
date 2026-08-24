# Unmatched Order Number Handoff — Design

> Owner-directed (via systematic-debugger finding). Approved 2026-08-23.

## Problem

`systematic-debugger` traced a real bug: when a customer supplies a well-formed order number that doesn't resolve to an order on their WhatsApp phone (either the order belongs to a different phone, or doesn't exist at all — these must stay indistinguishable per Critical Rule 3's non-enumerable refusal design), the bot has no state for it. `app/core/conversation.py::_recover_order_by_name` collapses this case into the exact same `([], None)` return as "no order number was mentioned at all," so `app/agents/order_tracking.py`'s empty-orders prompt line ("No order is linked to this WhatsApp number yet. Ask for their order number.") fires even though the customer just gave one. The LLM, given contradictory reality (it has an order number in hand, but is told to ask for one), improvises a confusing reply and escalates — burning an unnecessary 24-hour AI-pause handoff for a common, harmless scenario (customer ordered from a family member's phone, or changed SIM).

Confirmed via code trace (`order_resolver.py::resolve_by_order_name` docstring, `conversation.py:294-298`, `order_tracking.py:170`) — not a hypothesis.

## Decision (owner-confirmed)

1. Distinguish "candidate resolved to nothing" from "no candidate given" in `_recover_order_by_name`'s return, using the same `format_hint` channel already carrying the existing wrong-digit-shape hint into the prompt — no new field on `AgentContext` needed.
2. New hint text gives the LLM the *substance* to convey in its own words (matching this codebase's existing "describe in your own words, not verbatim" convention for multi-language warmth — see `exchange.py`), built from: *"I couldn't find that order linked to this WhatsApp number. If you used a different phone number when placing the order, could you message us from that number, or share the order email so I can look into it another way?"* Plus explicit instructions: don't ask them to resend the order number (they already gave a valid-shaped one), and don't offer to connect them with the team for this reason alone unless they explicitly ask.
3. No new deterministic auto-handoff gate — the LLM decides `handoff` itself from prompt guidance, same as every other agent in this codebase. A correct instruction removes the reason to improvise-and-escalate.
4. Delete the two dead copy keys (`app/channels/copy.py`: `order_not_found`, `refusal_other_order`) as unused dead code — they're `copy_for()` fixed-string keys, a mechanism this LLM-composed reply path doesn't use anywhere today (`order_tracking.py` never calls `copy_for()` for dynamic order content). Their only current reference is a test asserting they exist, which is removed alongside them.

## Changes

**`app/core/conversation.py::_recover_order_by_name`** (currently lines 280-322): the final line,
```python
order = await resolve_by_order_name(shopify, wa_id, candidate)
return ([order] if order is not None else []), None
```
changes so the `order is None` branch returns a new hint instead of `None`, carrying the candidate order number and the substance above. The digit-shape validation branch (lines 304-320) is untouched — it already returns its own distinct hint for the wrong-shape case.

**`app/agents/order_tracking.py`**: no code change needed — `context.order_number_format_hint` (line 181-183) already threads any non-empty hint into the prompt right after `{order_context}`. The new hint text itself is written to override/supersede the "No order is linked... ask for their order number" line it will appear alongside (that line is unconditional on empty `orders`, so it still renders — the new hint's wording must make clear this response takes priority for this turn, not layer a second contradictory instruction on top of it).

**`app/channels/copy.py`**: delete the `order_not_found` and `refusal_other_order` keys.

**`tests/test_copy.py`**: remove the test coverage referencing those two keys (the file's `KEYS`/`PHASE5_KEYS` tuples and the parametrized tests that iterate them).

## Scope

No change to `order_resolver.py::resolve_by_order_name` (its non-enumerable `None`-for-either-case behavior is correct and unchanged) or to any digit-shape-validation logic. No change to how `handoff` is decided elsewhere in the codebase. No change to `AgentContext`'s shape (the existing `order_number_format_hint: str | None` field is reused, not extended).

## Testing

- `conversation.py`: a test asserting `_recover_order_by_name` returns a non-`None` hint (containing the candidate order number, not the generic wrong-shape text) when a shape-valid candidate resolves to no owned order — and still returns `(orders, None)` when it DOES resolve, and `([], None)` when no candidate is present at all (three-way branch coverage, not just the new case).
- `order_tracking.py`: a test asserting the new hint text reaches the system prompt sent to the provider (mirrors the existing test pattern for the wrong-digit-shape hint).
- `copy.py`/`test_copy.py`: confirm the deleted keys' test references are cleanly removed and `copy_for("order_not_found", ...)` / `copy_for("refusal_other_order", ...)` now raise `KeyError` (proving they're genuinely gone, not just untested).
