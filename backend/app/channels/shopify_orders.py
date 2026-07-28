from dataclasses import dataclass
from datetime import datetime

from app.core.phone import normalize_phone

SUPPORTED_LANGUAGES = frozenset({"en", "hi", "gu"})


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
    customer = payload.get("customer") or {}
    shipping = payload.get("shipping_address") or {}
    billing = payload.get("billing_address") or {}
    phone = (
        normalize_phone(payload.get("phone"))
        or normalize_phone(customer.get("phone"))
        or normalize_phone(shipping.get("phone"))
        or normalize_phone(billing.get("phone"))
    )
    first = str(customer.get("first_name") or shipping.get("first_name") or "").strip()
    last = str(customer.get("last_name") or shipping.get("last_name") or "").strip()
    customer_name = f"{first} {last}".strip() or None
    raw_tags = payload.get("tags") or ""
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, str):
        tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
    gateways = tuple(str(g) for g in payload.get("payment_gateway_names") or ())
    number = payload.get("order_number")
    return IncomingOrder(
        gid=gid,
        name=name,
        order_number=int(number) if isinstance(number, int) else None,
        email=payload.get("email"),
        phone_e164=phone,
        customer_name=customer_name,
        tags=tags,
        gateways=gateways,
        created_at=_parse_created_at(payload.get("created_at")),
        locale=payload.get("customer_locale"),
    )


def choose_language(locale: str | None, default: str = "en") -> str:
    if locale:
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
