from dataclasses import dataclass

from app.core.phone import normalize_phone


@dataclass(frozen=True)
class Money:
    amount: str
    currency: str


@dataclass(frozen=True)
class LineItem:
    title: str
    quantity: int
    variant_title: str | None
    price: Money | None
    sku: str | None = None


@dataclass(frozen=True)
class Customer:
    gid: str
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    # Shopify's own last-modified stamp (raw ISO-8601). Defaulted because only the webhook
    # parsers carry it; the mirror uses it to reject an out-of-order (late retry) write.
    updated_at: str | None = None


@dataclass(frozen=True)
class FulfillmentEvent:
    """One entry in a fulfillment's Shopify tracking timeline (Admin GraphQL `FulfillmentEvent`).

    Live-read path only (`Fulfillment.events`). The RTO-aware "genuinely delivered?" rule scans
    these to tell a real delivery from an attempted/returned one. Both fields are stored raw --
    `status` is Shopify's own `FulfillmentEventStatus` enum, `happened_at` the raw ISO-8601 stamp.
    """

    status: str
    happened_at: str


@dataclass(frozen=True)
class Fulfillment:
    """One Shopify fulfillment (shipment) of an order, carrying its courier/tracking details.

    A single Order can have MULTIPLE fulfillments (split shipments), so orders hold these as a
    tuple. A single fulfillment can itself carry more than one tracking number (rare) -- we keep
    the first, matching the one-tracking-per-row mirror schema. Read-only Q&A enrichment for the
    order-tracking agent (Q10: share the Shopify tracking link when asked); never on a mutation
    path.
    """

    gid: str
    status: str | None
    tracking_company: str | None
    tracking_number: str | None
    tracking_url: str | None
    created_at: str | None = None
    # Shopify's own last-modified stamp (raw ISO-8601). The mirror's out-of-order-delivery guard:
    # a replayed fulfillments/create (label made, tracking empty) arriving AFTER a
    # fulfillments/update (tracking populated) must not revert good tracking. NULL = unknown,
    # which is always writable. Distinct from the mirror's own sync timestamp.
    updated_at: str | None = None
    # Shopify's own delivery timestamp (raw ISO-8601): the date the shipment was actually
    # delivered (Admin GraphQL `Fulfillment.deliveredAt`). None until delivered -- the common
    # case (most fulfillments are pending/in_transit). Populated ONLY on the live GraphQL read
    # path; the REST webhook payload carries no delivery-date field, so a webhook-parsed
    # Fulfillment always leaves this None. Capture-and-store only (no customer-facing use yet).
    delivered_at: str | None = None
    # Shopify's `FulfillmentDisplayStatus` (e.g. DELIVERED, ATTEMPTED_DELIVERY, OUT_FOR_DELIVERY).
    # Populated ONLY on the live GraphQL read path; the RTO-aware delivery rule reads it to decide
    # if a shipment is genuinely delivered vs attempted/returned. None on any webhook/mirror parse.
    display_status: str | None = None
    # The fulfillment's Shopify tracking timeline, newest-relevant first (see FulfillmentEvent).
    # GraphQL-read-path only; empty on webhook/mirror parses. The delivery rule scans this for an
    # ATTEMPTED/failure event AFTER a DELIVERED to catch a marked-delivered-then-returned shipment.
    events: tuple[FulfillmentEvent, ...] = ()
    # Our OWN normalized delivery-state mirror column (Task 4 populates it from ad2ship/the events
    # rule). Not a Shopify field -- distinct from display_status. Left defaulted on every read path
    # here; only the mirror write path sets it.
    shipment_status: str | None = None

    def has_tracking(self) -> bool:
        return bool(self.tracking_number or self.tracking_url)


@dataclass(frozen=True)
class Order:
    gid: str
    name: str
    email: str | None
    phone: str | None
    shipping_phone: str | None
    billing_phone: str | None
    financial_status: str | None
    fulfillment_status: str | None
    cancelled_at: str | None
    tags: tuple[str, ...]
    payment_gateway_names: tuple[str, ...]
    total: Money | None
    customer_locale: str | None
    line_items: tuple[LineItem, ...] = ()
    customer: Customer | None = None
    # See Customer.updated_at — the mirror's out-of-order-delivery guard.
    updated_at: str | None = None
    # Shopify Order.createdAt, raw ISO-8601 -- the starting point for the delivery-date
    # estimate formula (app/core/delivery_estimate.py). None for orders synced before this
    # field existed; callers that need it must handle that (they do -- see delivery_estimate).
    created_at: str | None = None
    # Split shipments: an order can have several fulfillments, each with its own tracking. Empty
    # until the order is fulfilled. Populated on both read paths (live Shopify query + DB mirror).
    fulfillments: tuple[Fulfillment, ...] = ()

    def best_phone(self) -> str | None:
        return self.phone or self.shipping_phone or self.billing_phone

    def is_cod(self) -> bool:
        if any("cash on delivery" in g.lower() for g in self.payment_gateway_names):
            return True
        return any(t.strip().lower() == "cod" for t in self.tags)

    def is_cod_by_gateway(self) -> bool:
        """COD by Shopify's OWN payment gateway only -- deliberately ignores the "cod" tag.

        Security-relevant: this gates the IRREVERSIBLE cancel mutation (and the admin resend of the
        cancel-button template). Unlike ``is_cod()``, it trusts ONLY ``payment_gateway_names``,
        which is Shopify-owned. The tag arm of ``is_cod()`` is written by this app's own
        ``add_tags`` calls (validated only for length/count, not content) and by any third-party
        app / Shopify Flow, so a stray "cod" tag on a genuinely prepaid order could otherwise flip
        it "cancellable". Only Shopify's gateway data may unlock a cancel; ``is_cod()`` stays as-is
        for lower-stakes display use (the "(Cash on Delivery)" note).
        """
        return any("cash on delivery" in g.lower() for g in self.payment_gateway_names)

    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


@dataclass(frozen=True)
class AuthorizedOrder:
    """Only core.order_resolver may construct this in production code (ADR-004).

    The invariant is enforced at runtime, not just by convention: a value of this
    type guarantees ``verified_phone`` matches one of the order's phones.
    """

    order: Order
    verified_phone: str

    def __post_init__(self) -> None:
        # Normalize BOTH sides before comparing so the check is source-independent: live Shopify
        # supplies phones raw (customer free-text like "98765 43210"), the Postgres mirror stores
        # them E.164-normalized, and the in-memory store now matches. Two representations of the
        # same number are the same number for an ownership check. An unparseable verified_phone
        # (normalize -> None) is rejected, and a None never matches a None order phone, so this
        # never authorizes against an unverifiable number.
        verified = normalize_phone(self.verified_phone)
        order_phones = {
            normalize_phone(p)
            for p in (self.order.phone, self.order.shipping_phone, self.order.billing_phone)
        }
        if not self.verified_phone or verified is None or verified not in order_phones:
            raise ValueError("AuthorizedOrder: verified_phone does not match the order")


@dataclass(frozen=True)
class CancelRequested:
    job_id: str | None


# Tavas order numbers are exactly this many digits today (confirmed live: tavas3898,
# tavas9652). Shopify order numbers are sequential, so this WILL need bumping once the store's
# order count crosses 9999 -- a "true today" fact, not a permanent assumption.
ORDER_NUMBER_DIGIT_LENGTH = 4


def normalize_order_name(raw: str, prefix: str = "tavas") -> str:
    name = raw.strip().lstrip("#").lower()
    if name.isdigit():
        return f"{prefix}{name}"
    return name


@dataclass(frozen=True)
class Product:
    gid: str
    title: str
    handle: str
    price: Money | None
    available: bool
    product_type: str | None
    tags: tuple[str, ...]
