import asyncio

from app.core.order_resolver import resolve_by_order_name, resolve_by_phone
from app.shopify.models import Order
from app.store.base import MappingView


def _order(gid: str, name: str, phone: str | None) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeShopify:
    def __init__(
        self,
        orders_by_gid: dict[str, Order] | None = None,
        orders_by_name: dict[str, Order] | None = None,
        orders_by_phone: dict[str, list[Order]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.orders_by_gid = orders_by_gid or {}
        self.orders_by_name = orders_by_name or {}
        self.orders_by_phone = orders_by_phone or {}
        self.raises = raises

    async def get_order(self, gid: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.orders_by_gid.get(gid)

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.orders_by_name.get(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        if self.raises:
            raise self.raises
        return self.orders_by_phone.get(phone_e164, [])


class _FakeIngest:
    def __init__(self, mappings: list[MappingView] | None = None) -> None:
        self.mappings = mappings or []

    async def ingest_order_created(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    async def recent_mappings(self, limit: int) -> list[MappingView]:
        raise NotImplementedError

    async def recent_outbound(self, limit: int) -> list[object]:
        raise NotImplementedError

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        return [m for m in self.mappings if m.phone_e164 == phone_e164][:limit]

    async def count_orders_by_phone(self, phone_e164: str) -> int:
        return len([m for m in self.mappings if m.phone_e164 == phone_e164])

    async def delete_by_phone(self, phone_e164: str) -> object:
        raise NotImplementedError

    async def purge_older_than(self, cutoff: object) -> object:
        raise NotImplementedError


def _mapping(gid: str, name: str, phone: str) -> MappingView:
    return MappingView(
        order_gid=gid, order_name=name, phone_e164=phone,
        status="pending", is_cod=False, created_at=None,
    )


async def test_resolve_by_phone_finds_via_mapping() -> None:
    order = _order("gid://1", "tavas1", "+919999999999")
    shopify = _FakeShopify(orders_by_gid={"gid://1": order})
    ingest = _FakeIngest(mappings=[_mapping("gid://1", "tavas1", "+919999999999")])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert len(result) == 1
    assert result[0].order.gid == "gid://1"
    assert result[0].verified_phone == "+919999999999"


async def test_resolve_by_phone_falls_back_to_customer_lookup() -> None:
    order = _order("gid://2", "tavas2", "+918888888888")
    shopify = _FakeShopify(orders_by_phone={"+918888888888": [order]})
    ingest = _FakeIngest(mappings=[])

    result = await resolve_by_phone(shopify, ingest, "918888888888")

    assert len(result) == 1
    assert result[0].order.gid == "gid://2"


async def test_resolve_by_phone_drops_stale_mapping_with_wrong_live_phone() -> None:
    order = _order("gid://3", "tavas3", "+917777777777")
    shopify = _FakeShopify(orders_by_gid={"gid://3": order})
    ingest = _FakeIngest(mappings=[_mapping("gid://3", "tavas3", "+919999999999")])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert result == []


async def test_resolve_by_phone_invalid_number_returns_empty() -> None:
    result = await resolve_by_phone(_FakeShopify(), _FakeIngest(), "abc")
    assert result == []


async def test_resolve_by_phone_degrades_to_empty_on_shopify_outage() -> None:
    from app.shopify.errors import ShopifyUnavailable

    shopify = _FakeShopify(raises=ShopifyUnavailable("down"))
    ingest = _FakeIngest(mappings=[_mapping("gid://4", "tavas4", "+919999999999")])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert result == []


class _ConcurrencyTrackingShopify(_FakeShopify):
    """Records how many get_order calls are in flight at once."""

    def __init__(self, orders_by_gid: dict[str, Order]) -> None:
        super().__init__(orders_by_gid=orders_by_gid)
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_order(self, gid: str) -> Order | None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        return self.orders_by_gid.get(gid)


async def test_resolve_by_phone_fetches_mapped_orders_concurrently() -> None:
    """Sequential re-fetches put every extra order straight onto the customer's wait time."""
    phone = "+919999999999"
    gids = ["gid://1", "gid://2", "gid://3"]
    shopify = _ConcurrencyTrackingShopify(
        {gid: _order(gid, f"tavas{i}", phone) for i, gid in enumerate(gids)}
    )
    ingest = _FakeIngest(mappings=[_mapping(gid, f"tavas{i}", phone) for i, gid in enumerate(gids)])

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert [o.order.gid for o in result] == gids  # order preserved
    assert shopify.max_in_flight == 3


class _FlakyShopify(_FakeShopify):
    """Fails get_order for one specific gid, succeeds for the rest."""

    def __init__(self, orders_by_gid: dict[str, Order], failing_gid: str) -> None:
        super().__init__(orders_by_gid=orders_by_gid)
        self.failing_gid = failing_gid

    async def get_order(self, gid: str) -> Order | None:
        from app.shopify.errors import ShopifyUnavailable

        if gid == self.failing_gid:
            raise ShopifyUnavailable("down")
        return self.orders_by_gid.get(gid)


async def test_resolve_by_phone_keeps_orders_that_did_resolve_when_one_fetch_fails() -> None:
    phone = "+919999999999"
    shopify = _FlakyShopify(
        {
            "gid://a": _order("gid://a", "tavasa", phone),
            "gid://b": _order("gid://b", "tavasb", phone),
        },
        failing_gid="gid://a",
    )
    ingest = _FakeIngest(
        mappings=[_mapping("gid://a", "tavasa", phone), _mapping("gid://b", "tavasb", phone)]
    )

    result = await resolve_by_phone(shopify, ingest, "919999999999")

    assert [o.order.gid for o in result] == ["gid://b"]


async def test_resolve_by_phone_drops_mirror_sourced_order_with_wrong_phone() -> None:
    # A mirror-sourced order (surfaced via MirrorOrderSource) whose stored phone does NOT match the
    # verified WhatsApp number must be dropped by AuthorizedOrder's ownership invariant, exactly
    # like a live-Shopify order. Closes the "mirror-sourced" ownership gap explicitly.
    from app.core.mirror_order_source import MirrorOrderSource

    wrong = _order("gid://m", "tavas-mirror", "+911111111111")

    class _MirrorIngest:
        async def get_mirrored_order(self, gid: str) -> Order | None:
            return None

        async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
            return None

        async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
            return [wrong]

    class _EmptyShopify:
        async def get_order(self, gid: str) -> Order | None:
            return None

        async def find_order_by_name(self, raw_name: str) -> Order | None:
            return None

        async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
            return []

    source = MirrorOrderSource(_MirrorIngest(), _EmptyShopify())

    result = await resolve_by_phone(source, _FakeIngest(mappings=[]), "919999999999")

    assert result == []


async def test_resolve_by_order_name_ownership_match() -> None:
    order = _order("gid://5", "tavas5", "+919999999999")
    shopify = _FakeShopify(orders_by_name={"tavas5": order})

    result = await resolve_by_order_name(shopify, "919999999999", "tavas5")

    assert result is not None
    assert result.order.gid == "gid://5"


async def test_resolve_by_order_name_ownership_mismatch_returns_none() -> None:
    order = _order("gid://6", "tavas6", "+911111111111")
    shopify = _FakeShopify(orders_by_name={"tavas6": order})

    result = await resolve_by_order_name(shopify, "919999999999", "tavas6")

    assert result is None


async def test_resolve_by_order_name_not_found_returns_none() -> None:
    result = await resolve_by_order_name(_FakeShopify(), "919999999999", "tavas999")
    assert result is None
