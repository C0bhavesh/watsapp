from dataclasses import dataclass
from datetime import datetime

from app.core.phone import normalize_phone
from app.shopify.models import Customer, LineItem, Money, Order

SUPPORTED_LANGUAGES = frozenset({"en", "hi", "gu"})


def _s(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _d(v: object) -> dict[str, object]:
    return v if isinstance(v, dict) else {}


def _seq(v: object) -> tuple[object, ...]:
    return tuple(v) if isinstance(v, (list, tuple)) else ()


@dataclass(frozen=True)
class IncomingOrder:
    gid: str
    name: str
    order_number: int | None
    email: str | None
    phone_e164: str | None
    customer_name: str | None
    tags: tuple[str, ...]
    gateways: tuple[str, ...]
    created_at: datetime | None
    locale: str | None
    financial_status: str | None

    def is_cod(self) -> bool:
        if any("cash on delivery" in g.lower() for g in self.gateways):
            return True
        return any(t.strip().lower() == "cod" for t in self.tags)


def _parse_created_at(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_order_created(payload: dict) -> IncomingOrder | None:  # type: ignore[type-arg]
    gid = payload.get("admin_graphql_api_id")
    name = payload.get("name")
    if not isinstance(gid, str) or not isinstance(name, str) or not gid or not name:
        return None
    customer = _d(payload.get("customer"))
    shipping = _d(payload.get("shipping_address"))
    billing = _d(payload.get("billing_address"))
    phone = (
        normalize_phone(_s(payload.get("phone")))
        or normalize_phone(_s(customer.get("phone")))
        or normalize_phone(_s(shipping.get("phone")))
        or normalize_phone(_s(billing.get("phone")))
    )
    first = (_s(customer.get("first_name")) or _s(shipping.get("first_name")) or "").strip()
    last = (_s(customer.get("last_name")) or _s(shipping.get("last_name")) or "").strip()
    customer_name = f"{first} {last}".strip() or None
    raw_tags = payload.get("tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, str):
        tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
    gateways = tuple(str(g) for g in _seq(payload.get("payment_gateway_names")))
    number = payload.get("order_number")
    return IncomingOrder(
        gid=gid,
        name=name,
        order_number=int(number) if isinstance(number, int) else None,
        email=_s(payload.get("email")),
        phone_e164=phone,
        customer_name=customer_name,
        tags=tags,
        gateways=gateways,
        created_at=_parse_created_at(payload.get("created_at")),
        locale=_s(payload.get("customer_locale")),
        financial_status=_s(payload.get("financial_status")),
    )


def choose_language(locale: str | None, default: str = "en") -> str:
    if isinstance(locale, str) and locale:
        code = locale[:2].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
    return default


def is_eligible_for_push(
    order: IncomingOrder, now: datetime, push_policy: str, staleness_hours: float
) -> bool:
    if order.created_at is None:
        return False
    if (now - order.created_at).total_seconds() > staleness_hours * 3600:
        return False
    if push_policy == "cod_only":
        return order.is_cod()
    return push_policy in ("all", "all_prepaid_no_buttons")


def _line_items_from_webhook(raw: object) -> tuple[LineItem, ...]:
    items: list[LineItem] = []
    for node in _seq(raw):
        item = _d(node)
        title = _s(item.get("title"))
        if title is None:
            continue
        quantity = item.get("quantity")
        price_raw = _s(item.get("price"))
        items.append(
            LineItem(
                title=title,
                quantity=int(quantity) if isinstance(quantity, int) else 0,
                variant_title=_s(item.get("variant_title")),
                price=Money(amount=price_raw, currency="INR") if price_raw else None,
                sku=_s(item.get("sku")),
            )
        )
    return tuple(items)


def _customer_from_order_payload(payload: dict) -> Customer | None:  # type: ignore[type-arg]
    customer = _d(payload.get("customer"))
    gid = customer.get("admin_graphql_api_id")
    if not isinstance(gid, str) or not gid:
        return None
    shipping = _d(payload.get("shipping_address"))
    return Customer(
        gid=gid,
        first_name=_s(customer.get("first_name")),
        last_name=_s(customer.get("last_name")),
        email=_s(customer.get("email")),
        phone=normalize_phone(_s(customer.get("phone"))) or _s(customer.get("phone")),
        address_line1=_s(shipping.get("address1")),
        address_line2=_s(shipping.get("address2")),
        city=_s(shipping.get("city")),
        state=_s(shipping.get("province")),
        postal_code=_s(shipping.get("zip")),
        country=_s(shipping.get("country")),
    )


def order_from_webhook_payload(payload: dict) -> Order | None:  # type: ignore[type-arg]
    """Parse a full Shopify order webhook payload (orders/create or orders/updated) into an
    ``Order`` for the mirror -- the payload already carries everything needed, no extra Shopify
    call. Shares the same missing-gid/name guard as ``parse_order_created``."""
    gid = payload.get("admin_graphql_api_id")
    name = payload.get("name")
    if not isinstance(gid, str) or not isinstance(name, str) or not gid or not name:
        return None
    shipping = _d(payload.get("shipping_address"))
    billing = _d(payload.get("billing_address"))
    raw_tags = payload.get("tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, str):
        tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
    gateways = tuple(str(g) for g in _seq(payload.get("payment_gateway_names")))
    total_price = _s(payload.get("total_price"))
    currency = _s(payload.get("currency")) or "INR"
    return Order(
        gid=gid,
        name=name,
        email=_s(payload.get("email")),
        phone=normalize_phone(_s(payload.get("phone"))),
        shipping_phone=normalize_phone(_s(shipping.get("phone"))),
        billing_phone=normalize_phone(_s(billing.get("phone"))),
        financial_status=_s(payload.get("financial_status")),
        fulfillment_status=_s(payload.get("fulfillment_status")),
        cancelled_at=_s(payload.get("cancelled_at")),
        tags=tags,
        payment_gateway_names=gateways,
        total=Money(amount=total_price, currency=currency) if total_price else None,
        customer_locale=_s(payload.get("customer_locale")),
        line_items=_line_items_from_webhook(payload.get("line_items")),
        customer=_customer_from_order_payload(payload),
    )


def customer_from_webhook_payload(payload: dict) -> Customer | None:  # type: ignore[type-arg]
    """Parse a Shopify ``customers/update`` webhook payload -- a plain Customer resource, not
    nested in an order."""
    gid = payload.get("admin_graphql_api_id")
    if not isinstance(gid, str) or not gid:
        return None
    address = _d(payload.get("default_address"))
    return Customer(
        gid=gid,
        first_name=_s(payload.get("first_name")),
        last_name=_s(payload.get("last_name")),
        email=_s(payload.get("email")),
        phone=normalize_phone(_s(payload.get("phone"))) or _s(payload.get("phone")),
        address_line1=_s(address.get("address1")),
        address_line2=_s(address.get("address2")),
        city=_s(address.get("city")),
        state=_s(address.get("province")),
        postal_code=_s(address.get("zip")),
        country=_s(address.get("country")),
    )
