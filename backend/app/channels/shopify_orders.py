from dataclasses import dataclass
from datetime import datetime

from app.core.phone import normalize_phone
from app.shopify.models import Customer, Fulfillment, LineItem, Money, Order

SUPPORTED_LANGUAGES = frozenset({"en", "hi", "gu"})

# Every string on a signed-but-attacker-typed webhook payload is untrusted, so nothing
# unbounded reaches a text column. Declared here (next to the parsers that produce those
# strings) and imported by shopify_webhook.py so the cap has one definition.
MAX_FIELD_LEN = 256


def clip(v: str | None) -> str | None:
    return v if v is None else v[:MAX_FIELD_LEN]


def _s(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _c(v: object) -> str | None:
    """A payload string, coerced and length-capped — the mirror parsers' default accessor."""
    return clip(_s(v))


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
        title = _c(item.get("title"))
        if title is None:
            continue
        quantity = item.get("quantity")
        price_raw = _c(item.get("price"))
        items.append(
            LineItem(
                title=title,
                quantity=int(quantity) if isinstance(quantity, int) else 0,
                variant_title=_c(item.get("variant_title")),
                price=Money(amount=price_raw, currency="INR") if price_raw else None,
                sku=_c(item.get("sku")),
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
        first_name=_c(customer.get("first_name")),
        last_name=_c(customer.get("last_name")),
        email=_c(customer.get("email")),
        phone=normalize_phone(_s(customer.get("phone"))) or _c(customer.get("phone")),
        address_line1=_c(shipping.get("address1")),
        address_line2=_c(shipping.get("address2")),
        city=_c(shipping.get("city")),
        state=_c(shipping.get("province")),
        postal_code=_c(shipping.get("zip")),
        country=_c(shipping.get("country")),
        updated_at=_c(customer.get("updated_at")),
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
        tags = tuple(t.strip()[:MAX_FIELD_LEN] for t in raw_tags.split(",") if t.strip())
    gateways = tuple(str(g)[:MAX_FIELD_LEN] for g in _seq(payload.get("payment_gateway_names")))
    total_price = _c(payload.get("total_price"))
    currency = _c(payload.get("currency")) or "INR"
    # gid is the mirror's primary key and is deliberately NOT clipped: truncating it could
    # collapse two distinct orders onto one row. The 1 MiB body cap bounds its length.
    return Order(
        gid=gid,
        name=name[:MAX_FIELD_LEN],
        email=_c(payload.get("email")),
        phone=normalize_phone(_s(payload.get("phone"))),
        shipping_phone=normalize_phone(_s(shipping.get("phone"))),
        billing_phone=normalize_phone(_s(billing.get("phone"))),
        financial_status=_c(payload.get("financial_status")),
        fulfillment_status=_c(payload.get("fulfillment_status")),
        cancelled_at=_c(payload.get("cancelled_at")),
        tags=tags,
        payment_gateway_names=gateways,
        total=Money(amount=total_price, currency=currency) if total_price else None,
        customer_locale=_c(payload.get("customer_locale")),
        line_items=_line_items_from_webhook(payload.get("line_items")),
        customer=_customer_from_order_payload(payload),
        updated_at=_c(payload.get("updated_at")),
    )


def _order_gid_from_id(raw: object) -> str | None:
    """Build the order gid from a fulfillment payload's numeric ``order_id``.

    Unlike every other parser here, the order gid CANNOT be taken from an ``admin_graphql_api_id``
    field -- a fulfillment payload's own ``admin_graphql_api_id`` is the FULFILLMENT's gid, and the
    only reference to its order is the numeric ``order_id``. So the order gid is reconstructed here
    (the one place F20's "never reconstruct a gid" rule cannot apply, because Shopify gives us no
    order gid to copy). ``bool`` is an ``int`` subclass, so it is rejected explicitly; a string
    id must be ASCII digits only, so nothing path-like is interpolated into the gid.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return f"gid://shopify/Order/{raw}"
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return f"gid://shopify/Order/{raw}"
    return None


def _first_tracking(singular: object, plural: object) -> str | None:
    """The tracking value: the singular field if present, else the first array entry.

    Shopify sends ``tracking_number``/``tracking_url`` (singular) plus ``tracking_numbers``/
    ``tracking_urls`` (arrays). A single fulfillment can carry more than one tracking number
    (rare) -- we keep the first usable one, matching the one-tracking-per-row mirror schema.
    """
    value = _c(singular)
    if value is not None:
        return value
    for entry in _seq(plural):
        clipped = _c(entry)
        if clipped is not None:
            return clipped
    return None


def fulfillment_from_webhook_payload(
    payload: dict,  # type: ignore[type-arg]
) -> tuple[str, Fulfillment] | None:
    """Parse a Shopify ``fulfillments/create``/``fulfillments/update`` REST payload into
    ``(order_gid, Fulfillment)``.

    Returns ``None`` when the fulfillment's own gid or its order reference is missing/unusable --
    a fulfillment we cannot link to an order is not storable. Every field is treated as
    attacker-typed (the delivery is signed but the body is untrusted), so tracking fields degrade
    to ``None`` rather than raising.
    """
    gid = payload.get("admin_graphql_api_id")
    if not isinstance(gid, str) or not gid:
        return None
    order_gid = _order_gid_from_id(payload.get("order_id"))
    if order_gid is None:
        return None
    fulfillment = Fulfillment(
        # The fulfillment gid is the mirror's PRIMARY KEY and is deliberately NOT clipped:
        # truncating it could collapse two distinct fulfillments onto one row. The 1 MiB body cap
        # bounds its length (same reasoning as the order gid in order_from_webhook_payload).
        gid=gid,
        status=_c(payload.get("status")),
        tracking_company=_c(payload.get("tracking_company")),
        tracking_number=_first_tracking(
            payload.get("tracking_number"), payload.get("tracking_numbers")
        ),
        tracking_url=_first_tracking(payload.get("tracking_url"), payload.get("tracking_urls")),
        created_at=_c(payload.get("created_at")),
        updated_at=_c(payload.get("updated_at")),
        # delivered_at is intentionally left at its None default: the Shopify REST fulfillment
        # webhook payload carries NO delivery-date field (only `shipment_status`, a string enum
        # like "in_transit"/"delivered" -- not a timestamp). The actual delivery date is available
        # only via the Admin GraphQL `Fulfillment.deliveredAt`, so it is populated on the live
        # GraphQL read path (_fulfillments_from_node), never here. Do NOT invent a REST field.
    )
    return order_gid, fulfillment


def customer_from_webhook_payload(payload: dict) -> Customer | None:  # type: ignore[type-arg]
    """Parse a Shopify ``customers/update`` webhook payload -- a plain Customer resource, not
    nested in an order."""
    gid = payload.get("admin_graphql_api_id")
    if not isinstance(gid, str) or not gid:
        return None
    address = _d(payload.get("default_address"))
    return Customer(
        gid=gid,
        first_name=_c(payload.get("first_name")),
        last_name=_c(payload.get("last_name")),
        email=_c(payload.get("email")),
        phone=normalize_phone(_s(payload.get("phone"))) or _c(payload.get("phone")),
        address_line1=_c(address.get("address1")),
        address_line2=_c(address.get("address2")),
        city=_c(address.get("city")),
        state=_c(address.get("province")),
        postal_code=_c(address.get("zip")),
        country=_c(address.get("country")),
        updated_at=_c(payload.get("updated_at")),
    )
