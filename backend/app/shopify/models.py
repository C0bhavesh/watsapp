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

    def best_phone(self) -> str | None:
        return self.phone or self.shipping_phone or self.billing_phone

    def is_cod(self) -> bool:
        if any("cash on delivery" in g.lower() for g in self.payment_gateway_names):
            return True
        return any(t.strip().lower() == "cod" for t in self.tags)

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
