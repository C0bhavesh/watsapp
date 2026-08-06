from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    amount: str
    currency: str


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
        phones = (self.order.phone, self.order.shipping_phone, self.order.billing_phone)
        if not self.verified_phone or self.verified_phone not in phones:
            raise ValueError("AuthorizedOrder: verified_phone does not match the order")


@dataclass(frozen=True)
class CancelRequested:
    job_id: str | None


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
