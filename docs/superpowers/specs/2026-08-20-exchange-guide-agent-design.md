# Exchange Guide Agent — Design

> Status: approved by owner (2026-08-20). Ready for planning.

## Problem

A customer asking to exchange an item currently gets a generic answer from the `policy`
agent (the store's abstract exchange-policy text) with no way to actually act on it. The
owner wants a guided flow: check eligibility, collect the size they want, explain the
process, record the request so it's visible in the admin panel, and later answer "where
is my exchange" from that record.

## Scope decision (owner-confirmed 2026-08-20)

This bot has zero WhatsApp media-message handling today (`app/channels/whatsapp_inbound.py`
parses text/button/interactive only — no image/video). The store's policy also covers a
second, different exchange path: damaged/incorrect items, which need photo/video proof and
manual verification, with no fixed time window. **This build covers SIZE exchange only**
(the automatable 48-hour-window path). A damaged/incorrect-item complaint still routes to
`customer_support` for manual handling, exactly as it does today — no regression, no new
scope. The media-handling work needed for that path is a separate future feature, to be
brainstormed on its own once the owner is ready to define how proof gets collected/stored.

Also explicitly decided:
- **No live stock check.** The bot does not look up variant inventory for the requested
  size before creating the request; "subject to stock availability" is resolved manually
  later in the process, not at request time.
- **No button-tap confirmation.** Unlike order Confirm/Cancel (CLAUDE.md's standard
  "LLM never mutates, only a button tap does"), the owner chose to let the agent create
  the exchange request directly once eligibility + size are established in conversation.
  See "Mutation-safety note" below for how this is still done without letting the LLM
  touch the database directly.
- **No separate client sign-off gate applies to this project** — confirmed directly with
  the owner (Bhavesh) 2026-08-20: there is no third-party client distinct from him: his
  answers in this design conversation are final, not provisional pending someone else's
  approval.

## Data model

New table, `backend/app/store/schema.sql` (additive, `CREATE TABLE IF NOT EXISTS`,
mirrors this file's existing table style):

```sql
CREATE TABLE IF NOT EXISTS exchange_requests (
    id                  bigserial PRIMARY KEY,
    order_gid           text NOT NULL,
    order_name          text NOT NULL,
    phone_e164          text NOT NULL,
    requested_size      text NOT NULL,
    status              text NOT NULL DEFAULT 'requested',
    requested_at        timestamptz NOT NULL DEFAULT now(),
    return_tracking_url text,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exchange_requests_order ON exchange_requests (order_gid);
CREATE INDEX IF NOT EXISTS idx_exchange_requests_phone ON exchange_requests (phone_e164);
```

`status` is a fixed set, matching the owner's 4-step process message and admin-driven
(no courier/QC integration exists to auto-advance it):

```python
ExchangeStatus = Literal[
    "requested", "return_picked_up", "qc_passed", "qc_failed",
    "replacement_dispatched", "delivered",
]
```

New frozen dataclass `ExchangeRequest` in a new `app/core/exchange_models.py` — kept out of
`app/shopify/models.py`, which holds only Shopify-sourced data; an exchange request is
app-owned state with no Shopify counterpart. Mirrors the field names above, with
`status: ExchangeStatus`.

## Store port

New protocol `ExchangeStore` in `app/store/base.py` (mirrors `ConversationStore`'s shape),
implemented by both `PostgresExchangeStore` (`app/store/postgres.py`) and
`InMemoryExchangeStore` (`app/store/memory.py`):

```python
class ExchangeStore(Protocol):
    async def create(self, order_gid: str, order_name: str, phone_e164: str,
                      requested_size: str) -> ExchangeRequest: ...
    async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]: ...
    async def set_status(self, id: int, status: ExchangeStatus) -> None: ...
    async def set_return_tracking_url(self, id: int, url: str) -> None: ...
```

## Eligibility — computed in Python, never by the LLM

New pure function in `app/core/exchange_eligibility.py` (mirrors
`_is_cancel_eligible`/`estimate_delivery`'s existing discipline: a fact is computed once in
code and handed to the agent as text, never left for the model to derive from raw dates):

```python
@dataclass(frozen=True)
class ExchangeEligibility:
    eligible: bool
    reason: str  # always set, both when eligible and not -- the agent relays it verbatim

def check_exchange_eligibility(order: Order, today: date) -> ExchangeEligibility:
    ...
```

Rules, in order:
1. Order cancelled → not eligible, reason "order is cancelled."
2. No fulfillment has `delivered_at` set → not eligible, reason "order not yet delivered."
   (Mirrors `delivery_estimate.py`'s existing `_is_delivered` check — same predicate,
   reused rather than reimplemented.)
3. Delivery date = the latest non-null `delivered_at` across fulfillments. If
   `today - delivery_date > 48 hours` → not eligible, reason "delivered on
   {date}, outside the 48-hour exchange window."
4. Otherwise → eligible, reason "delivered on {date}, within the 48-hour exchange window."

## `AgentContext` change

New field on the frozen dataclass in `app/agents/base.py`:

```python
exchange_requests: list[ExchangeRequest] = field(default_factory=list)
```

Populated in `core/conversation.py` the same way `orders` already is — a store read scoped
to the resolved phone, done once per turn regardless of which agent ends up handling it
(matches how `orders` is already unconditionally resolved).

## Router change

`app/agents/router.py`'s `_ROUTER_PROMPT` gains a 6th intent:

```
- exchange: the customer wants to actually exchange an item from THEIR OWN order for a
  different size (not just asking about the exchange policy in the abstract -- that is
  policy). Reports of a damaged, defective, or wrong item are NOT this -- route those to
  customer_support instead, since they need photo/video proof this bot cannot yet collect.
```

`policy`'s existing bullet is narrowed the same way the delivery-timing split narrowed it
earlier: "return, exchange, or refund rules... in the abstract" stays, with an explicit
exclusion for "a customer who wants to actually exchange their own order — that is
`exchange`."

## Conversation flow (`app/agents/exchange.py`)

New agent, same shape as `order_tracking.py`/`policy.py`: a system-prompt template +
`HANDOFF_JSON_CONTRACT`, fed a Python-rendered context block per eligible/ineligible order
plus any existing exchange requests (status + return-tracking link, so the agent can
answer "where is my exchange" from real data, never invented).

1. Agent is given each order's `ExchangeEligibility` fact. If ineligible, it explains why
   using the given reason and does not proceed further for that order.
2. If eligible, it asks which size the customer wants — explicitly no color/product swap
   (owner's stated rule; the prompt states this directly so the model doesn't offer one).
   If more than one eligible order, it asks which order, same disambiguation pattern
   `order_tracking` already relies on (full order list handed to the model, it asks).
3. Once an order + size are both established in the conversation, the model's structured
   JSON reply carries one more field beyond `reply`/`handoff`:
   `"create_exchange": {"order_gid": "...", "size": "..."} | null`.
4. **Mutation-safety note:** the database write is NOT performed by the LLM. Deterministic
   Python code in `exchange.py`'s `run()` reads this field from the parsed JSON and calls
   `ExchangeStore.create(...)` itself — the same mechanism `handoff: true` already uses to
   trigger a real side effect (the 24h pause) without the model touching anything directly.
   This keeps "the LLM never mutates" true in the sense this codebase has always meant it
   (no tool-calling, no direct DB/API access from the model) while honoring the owner's
   choice to skip a customer-facing button-tap step. It is still a deliberate deviation from
   the *pattern* used for order Confirm/Cancel — a raw JSON field is a lower bar than a
   dedicated interactive button, so a validation layer sits between the field and the write
   (below).
5. On a successful create, the agent's reply is the 4-step process message, paraphrased
   from the owner's text (not copied verbatim — same "read like the store's best support
   person" bar every other agent reply meets), naming the order and requested size.

**Validation before the write actually happens** (defense against a model hallucinating
the field or getting the order/size wrong): `exchange.py` re-checks `order_gid` is one of
`context.orders`' gids AND that order's `ExchangeEligibility.eligible` is `True` before
calling `ExchangeStore.create` — a `create_exchange` field naming an ineligible or
unrecognized order is silently ignored (logged, not created), same "never trust the model's
claim, re-verify against real data" discipline `order_actions.py` already applies to button
taps.

## Admin panel

`_order_summary()` (`app/admin/router.py`, already backing the chat page's order-details
3rd pane) gains an optional `exchange` key per order when a request exists:

```python
{"requested_size": ..., "status": ..., "requested_at": ..., "return_tracking_url": ...}
```

`chats.html`/`chats.js` render an "Exchange" section in the order panel: size requested,
request date, current status (readable label), and the return-tracking URL once set.
Admin can advance `status` through the fixed stages and set/edit `return_tracking_url`
via a new endpoint:

```
POST /admin/exchanges/{id}
  {"status": "return_picked_up" | ..., "return_tracking_url": "https://..."}  (both optional)
```

Same `require_admin` + rate-limit pattern as every other admin-mutation endpoint
(`send_manual_reply`, `send_admin_template`).

## Testing

- `check_exchange_eligibility`: cancelled, undelivered, within-window, outside-window,
  boundary at exactly 48h (must still be eligible — `<=`, not `<`).
- `exchange.py`: prompt-content assertions (eligible/ineligible context rendered
  correctly, size-only framing present) following this codebase's established
  prompt-testability limitation (asserts prompt structure, not live model judgment) —
  same honest caveat as `router.py`/`policy.py`'s tests today.
- `exchange.py`'s `run()`: the deterministic create-and-validate path — a `create_exchange`
  field naming an eligible known order creates a record; naming an ineligible or unknown
  order does not; store call is asserted via a fake `ExchangeStore`.
- `ExchangeStore` (both impls): create/list_for_phone/set_status/set_return_tracking_url,
  mirroring the existing `ConversationStore` test shape.
- `_order_summary()`/admin endpoint: exchange key present only when a request exists;
  `POST /admin/exchanges/{id}` updates status/tracking, rejects an unknown id and an
  invalid status value.

## Out of scope (this pass)

- Damaged/incorrect-item exchange (needs media-message handling — future feature).
- Live stock check for the requested size.
- Any automation for generating/sending the return-pickup tracking link — the field just
  holds whatever URL the admin enters.
- A button-tap confirmation step for creating the request (owner-decided; see "Mutation-
  safety note" above for how the write is still deterministic despite this).
