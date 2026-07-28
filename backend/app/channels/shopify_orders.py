from dataclasses import dataclass
from datetime import datetime

from app.core.phone import normalize_phone

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
