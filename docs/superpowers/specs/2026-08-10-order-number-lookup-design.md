# Order-Number Lookup for Order Tracking — Design

**Status:** Approved by owner (2026-08-10), via conversational brainstorming (not the visual companion).

## Problem

`order_tracking` (Phase 4) only ever answers from orders already auto-linked to the customer's
WhatsApp number (`order_mappings`, populated by the Shopify `orders/create` webhook). When a
customer's phone has no linked order yet and they type an order number in free text, the agent
has no live lookup path — it correctly refuses to guess, but (per its existing "offer to connect
the customer with the team" instruction) it hands off to a human instead of checking Shopify,
even though a live-lookup helper already exists and already does everything needed.

Confirmed via a live test: asking about `tavas3898` with no linked order produced an immediate
handoff instead of an answer.

## What already exists (verified by reading the code, not assumed)

- `app/core/order_resolver.py::resolve_by_order_name(shopify, wa_id, raw_name)` — looks up an
  order by name and enforces the phone-ownership check (Critical Rule 3) via `AuthorizedOrder`.
  Returns `None` for both "doesn't exist" and "exists but wrong owner" — already
  non-enumerable by design.
- `app/shopify/models.py::normalize_order_name(raw, prefix="tavas")` — strips a leading `#`,
  lowercases, and prepends `tavas` to a bare digit string (`"9652"` → `"tavas9652"`). Handles
  both the full form and the bare-number form a customer might type.
- `app/shopify/client.py::find_order_by_name` — queries Shopify safely (validates the normalized
  name is `[a-z0-9]+` before it ever reaches the GraphQL query).

None of this is wired into the conversation pipeline (`app/core/conversation.py`) — that's the
entire gap this feature closes.

## Design

### Flow

1. Phone-based resolution runs first, unchanged (`resolve_by_phone`, only for `order_tracking`
   intent, as today).
2. If the customer's message contains a 4-digit order-number candidate (see Extraction below),
   attempt `resolve_by_order_name(shopify, wa_id, candidate)` — regardless of whether phone
   resolution already found orders (a customer can own more than one order, or one placed under
   different contact details).
3. If that lookup returns an order not already present in the resolved list (dedupe by order
   name), append it.
4. If the candidate's digit count is anything other than 4, skip the Shopify call entirely (the
   format is already known to be wrong) and instead flag it so `order_tracking`'s prompt can ask
   the customer to double-check their order ID.
5. Everything downstream (`AgentContext`, `order_tracking.run`) already renders whatever ends up
   in `context.orders` — unchanged.

### Extraction

New helper, `app/core/order_resolver.py::extract_order_number_candidate(text: str) -> str | None`.
Looks for, in priority order: an explicit `tavas<digits>` token (case-insensitive), a `#<digits>`
token, or a bare run of digits. Returns the digit portion only (the caller decides what to do
with its length — see Format validation). Returns `None` if no digit run is found at all.

Deliberately permissive on what counts as "found a digit run" — a false-positive extraction
either triggers a safe, ownership-checked Shopify lookup that returns nothing, or triggers the
format-recheck prompt. Neither leaks information or causes harm; worst case is one unneeded
question to the customer. This mechanism only runs when the router has already classified the
message as `order_tracking`, which keeps unrelated numbers (pincodes, quantities) from reaching
this path in the first place.

### Format validation

Tavas order numbers are exactly 4 digits after the `tavas` prefix today (both live-tested
examples, `tavas3898` and `tavas9652`, confirm this). Represented as a named constant,
`ORDER_NUMBER_DIGIT_LENGTH = 4`, in `app/core/order_resolver.py`, with a comment noting Shopify
order numbers are sequential and will eventually grow past 9999 as the store scales — this
constant will need bumping when that happens; it is a "true today" fact, not a permanent
assumption baked in silently.

- Candidate digit length == `ORDER_NUMBER_DIGIT_LENGTH` → real candidate, attempt the live lookup
  (step 2 above).
- Candidate digit length != `ORDER_NUMBER_DIGIT_LENGTH` → no Shopify call; instead, a new
  optional field on `AgentContext` (`order_number_format_hint: str | None = None`, default
  `None` so no other agent is affected) is set to a short factual note (e.g. that the customer
  mentioned a number that doesn't match the store's 4-digit order ID format). `order_tracking`'s
  system prompt template gets one new conditional line that includes this note when present,
  instructing the model to ask the customer to double-check their order ID and gives a real
  example (`tavas9652`) — phrased by the model itself, in whichever language the customer is
  using (rides on the existing PERSONALITY multilingual behavior, no new fixed-copy strings).

### Miss handling (no format issue, just not found / wrong owner)

No new copy. `resolve_by_order_name` returning `None` leaves the orders list unchanged, and
`order_tracking`'s existing fallback text ("No order is linked to this WhatsApp number yet. Ask
for their order number.") already covers this — identical wording whether the customer typed
nothing, a wrong 4-digit number, or someone else's order number. This preserves the existing
non-enumerability property; no new distinguishable signal is introduced.

### Multiple orders

The lookup always runs when a 4-digit candidate is found, regardless of whether `context.orders`
already has entries from phone resolution — appended only if not already present (dedupe by
`order.order.name`).

### Error handling

`resolve_by_order_name` already catches `ShopifyError` internally and returns `None` on failure.
No new error handling needed — a Shopify hiccup during this extra lookup just means no extra
order surfaces, the same degrade-gracefully posture `resolve_by_phone` already has.

### Out of scope (YAGNI)

- Multiple order numbers in one message — only the first candidate found is used.
- Fuzzy/typo-tolerant matching.
- Asking the customer to disambiguate between multiple candidates.
- Any change to `resolve_by_order_name`, `normalize_order_name`, or `find_order_by_name` — all
  already correct and unchanged.
- Any mutation capability — this feature is entirely read-only (Critical Rule 2 unaffected).

## Testing

- Unit tests for `extract_order_number_candidate`: `"tavas9652"`, `"TAVAS9652"`, `"#9652"`,
  bare `"9652"`, a number embedded in a sentence, no digits present at all, a 3-digit run, a
  5-digit run, an empty string.
- Unit test confirming the `ORDER_NUMBER_DIGIT_LENGTH` constant is what the length check compares
  against (not a hardcoded literal elsewhere).
- Integration tests (webhook-level, mocked Shopify): customer with no linked orders types a
  message containing a valid 4-digit order number → reply reflects that order's details; same
  scenario with an order number belonging to a different phone → generic fallback, no
  distinguishable wording from "no number given at all"; customer already has a phone-linked
  order and mentions a different, valid 4-digit number → both orders appear, no duplicate if the
  same number is mentioned again; a 3-digit or 5-digit number → reply asks the customer to
  recheck their order ID, no Shopify call made (assert on the fake Shopify client's call count).

## Global constraints (already binding, restated for this feature)

- Critical Rule 2 (LLM never mutates) — untouched; this feature adds no new Shopify write paths.
- Critical Rule 3 (ownership check before revealing anything, always re-fetch live) — enforced
  entirely by the existing `resolve_by_order_name`/`AuthorizedOrder` chain; this feature does not
  bypass or duplicate that check.
- No new secrets, no new admin-panel config, no schema/migration changes.
