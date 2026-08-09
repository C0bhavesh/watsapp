from app.core.order_resolver import resolve_by_gid
from app.shopify.errors import ShopifyUnavailable
from app.shopify.models import AuthorizedOrder, Order


def _order(gid: str, phone: str | None) -> Order:
    return Order(
        gid=gid, name="tavas1", email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeShopify:
    def __init__(
        self, orders_by_gid: dict[str, Order] | None = None, raises: Exception | None = None
    ) -> None:
        self.orders_by_gid = orders_by_gid or {}
        self.raises = raises

    async def get_order(self, gid: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.orders_by_gid.get(gid)

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        raise NotImplementedError

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        raise NotImplementedError


GID = "gid://shopify/Order/1"


async def test_owner_gid_returns_authorized_order() -> None:
    shopify = _FakeShopify({GID: _order(GID, "+919664290413")})
    auth = await resolve_by_gid(shopify, "919664290413", GID)
    assert isinstance(auth, AuthorizedOrder)
    assert auth.order.gid == GID
    assert auth.verified_phone == "+919664290413"


async def test_non_owner_gid_returns_none() -> None:
    # Order exists but its phone does not match the tapper -> None (no enumeration).
    shopify = _FakeShopify({GID: _order(GID, "+911111111111")})
    assert await resolve_by_gid(shopify, "919664290413", GID) is None


async def test_missing_order_returns_none() -> None:
    shopify = _FakeShopify({})
    assert await resolve_by_gid(shopify, "919664290413", GID) is None


async def test_shopify_error_returns_none() -> None:
    shopify = _FakeShopify(raises=ShopifyUnavailable("down"))
    assert await resolve_by_gid(shopify, "919664290413", GID) is None


async def test_unparseable_wa_id_returns_none() -> None:
    shopify = _FakeShopify({GID: _order(GID, "+919664290413")})
    assert await resolve_by_gid(shopify, "not-a-number", GID) is None
