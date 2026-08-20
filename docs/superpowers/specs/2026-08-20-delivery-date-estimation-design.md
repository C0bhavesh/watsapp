# Delivery-Date Estimation — Design

> Status: approved by owner (2026-08-20). Ready for planning.

## Problem

When a customer asks the WhatsApp bot "when will my order arrive," the order-tracking
agent (`app/agents/order_tracking.py`) currently only shares tracking details if the
order has already shipped, and explicitly refuses to estimate an arrival date. For an
order that hasn't shipped yet, the customer gets no delivery-date information at all.

Owner-specified rule: never leave the customer without an estimate. Compute one from a
fixed formula (prep buffer + regional zone transit) when no better source exists.

## Scope decision (owner-confirmed 2026-08-20)

Q10 in `docs/FR/client-decisions-all.md` (answered 2026-08-06) already closed "no live
courier integration" for this project. Scraping courier tracking pages for a real
carrier-supplied ETA would reopen that decision. **This feature does NOT do that.** It is
formula-only. A new client question about a possible later "real courier ETA" phase is
logged separately in `client-decisions-all.md` (ON HOLD, not blocking this build).

## Data model change

`Order` (in `app/shopify/models.py`) gains a new field:

```python
created_at: str | None = None  # Shopify Order.createdAt, raw ISO-8601
```

Additive, matches the existing `updated_at`/`Fulfillment.created_at` pattern.

Threaded through:
- `app/shopify/client.py` — GraphQL order query gains `createdAt` in the selection set;
  the order-parsing helper reads it into the new field (mirrors how `updatedAt` is
  already read).
- `app/store/schema.sql` — new nullable column on the order-mirror table, additive
  migration (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, same idiom as prior additive
  migrations in this project).
- `app/store/postgres.py` / `app/store/memory.py` — read/write the new column/field
  wherever `Order` rows are constructed from mirror data (mirrors existing
  `updated_at` handling in both).
- `app/channels/shopify_webhook.py` — if the `orders/create` webhook payload carries a
  `created_at`, capture it into the mirror at ingest time (coerced via the same
  `_s`/defensive-parsing helpers already used for other webhook fields — see
  `error_learnings.md` 2026-07-28 "Webhook payload fields must be type-coerced").

No existing field, method, or caller changes shape — this is a pure addition.

## Zone mapping

New module-level constant, `app/core/delivery_estimate.py`:

```python
_ZONE_DAYS: dict[str, int] = {"west": 2, "north": 3, "east": 5, "south": 5}

_STATE_ZONE: dict[str, str] = {
    # North
    "jammu and kashmir": "north", "ladakh": "north", "himachal pradesh": "north",
    "punjab": "north", "chandigarh": "north", "uttarakhand": "north",
    "haryana": "north", "delhi": "north", "uttar pradesh": "north",
    "rajasthan": "north",
    # West (includes MP/Chhattisgarh -- no official zone in a 4-bucket scheme,
    # grouped here as the nearest geographic fit)
    "gujarat": "west", "maharashtra": "west", "goa": "west",
    "dadra and nagar haveli and daman and diu": "west",
    "madhya pradesh": "west", "chhattisgarh": "west",
    # East (includes the Northeast states -- also ungrouped in a 4-bucket scheme)
    "west bengal": "east", "odisha": "east", "bihar": "east", "jharkhand": "east",
    "assam": "east", "sikkim": "east", "arunachal pradesh": "east",
    "nagaland": "east", "manipur": "east", "mizoram": "east", "tripura": "east",
    "meghalaya": "east",
    # South
    "karnataka": "south", "andhra pradesh": "south", "telangana": "south",
    "tamil nadu": "south", "kerala": "south", "puducherry": "south",
    "andaman and nicobar islands": "south", "lakshadweep": "south",
}
_DEFAULT_ZONE = "south"  # unknown/missing state -> longest transit (safer to over- than
                          # under-promise)
```

Matched case-insensitively (`.strip().lower()`) against `Order.customer.state`
(Shopify `shippingAddress.province`, per Q16's existing precedent of using the shipping
contact as the source of truth for this kind of per-order geography).

## Computation

```python
@dataclass(frozen=True)
class DeliveryEstimate:
    expected_date: date

def estimate_delivery(order: Order, today: date) -> DeliveryEstimate | None:
    ...
```

Rules, in order:

1. **Already delivered** — any fulfillment has `delivered_at` set, or
   `fulfillment_status` indicates delivered → return `None`. The agent's existing
   delivered-order handling is untouched; this function is not consulted.
2. **No `created_at`** (legacy order, pre-migration) → return `None`. The agent falls
   back to its current behavior (no estimate, offer to have the team check) rather than
   guessing from an unknown start point.
3. **Otherwise:**
   - `zone = _STATE_ZONE.get(normalized_state, _DEFAULT_ZONE)`
   - `base = order_created_date + timedelta(days=2 + _ZONE_DAYS[zone])`
   - **Late-ship exception:** if `(today - order_created_date).days > 3` and the order
     is still not dispatched (`fulfillment_status in (None, "UNFULFILLED")`, same
     predicate `_is_cancel_eligible` already uses) → `base += timedelta(days=2)`.
   - Return `DeliveryEstimate(expected_date=base)`.

This function does not consider `tracking_url`/courier at all (owner-confirmed: zone
comes purely from the shipping state, not the courier that ends up used).

## Wiring into the agent

`order_tracking.py`'s `_order_line`/`_tracking_line` rendering gains one more line when
`estimate_delivery(...)` returns a value and `"status"` is in `reveal_fields` (delivery
timing is part of the status picture, same gate as fulfillment/cancellation state):

```
  - Estimated delivery: 2026-08-27 (this is an estimate and may vary by 1-2 days)
```

The system prompt's existing "never estimate an arrival time beyond what the tracking
data states" instruction is updated to the new behavior: relay the estimated-delivery
line **exactly as given, including the caveat**, when present; never compute or invent
a different date. This keeps the "LLM never invents order data" invariant — the date is
precomputed in Python, the model only echoes it in natural language.

## Testing

Pure unit tests on `estimate_delivery` in `backend/tests/core/test_delivery_estimate.py`
(new file, mirrors `test_button_dispatch.py`'s placement convention):
- each zone's day count (west/north/east/south) via a representative state
- unknown/missing state falls back to the 5-day default
- late-ship exception fires only when both conditions hold (>3 days AND undispatched)
- late-ship exception does NOT fire once dispatched, even past 3 days
- already-delivered order (via `delivered_at` and via `fulfillment_status`) returns
  `None`
- missing `created_at` returns `None`

No LLM/integration test needed — the model only relays a precomputed string, matching
how tracking-link relaying is tested today (rendering-level, not LLM-behavior-level).

## Out of scope (this pass)

- Real courier-supplied ETA via tracking-page scraping (would reopen closed Q10 — see
  the new client question logged in `client-decisions-all.md`).
- Any change to already-delivered or already-shipped-with-tracking messaging beyond
  what's described above.
