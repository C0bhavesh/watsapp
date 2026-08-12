import asyncpg
import pytest

from app.core.mirror_order_source import MirrorOrderSource
from app.shopify.models import Order


def _order(gid: str, name: str) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=None, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeMirrorIngest:
    def __init__(
        self,
        order_by_gid: Order | None = None,
        order_by_name: Order | None = None,
        orders_by_phone: list[Order] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.order_by_gid = order_by_gid
        self.order_by_name = order_by_name
        self.orders_by_phone = orders_by_phone or []
        self.raises = raises

    async def get_mirrored_order(self, gid: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.order_by_gid

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        if self.raises:
            raise self.raises
        return self.order_by_name

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        if self.raises:
            raise self.raises
        return self.orders_by_phone


class _FakeShopify:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        self.calls.append("get_order")
        return _order(gid, "tavas-from-shopify")

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        self.calls.append("find_order_by_name")
        return _order("gid://shopify-fallback", raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        self.calls.append("find_customer_orders_by_phone")
        return [_order("gid://shopify-fallback", "tavas-fallback")]


async def test_get_order_hit_never_calls_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_gid=_order("gid://1", "tavas1"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas1"
    assert shopify.calls == []


async def test_get_order_miss_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_gid=None)
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas-from-shopify"
    assert shopify.calls == ["get_order"]


async def test_get_order_db_error_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(raises=OSError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas-from-shopify"
    assert shopify.calls == ["get_order"]


async def test_get_order_asyncpg_error_falls_back_to_shopify() -> None:
    # A genuine DB error (asyncpg's base error type) degrades to a Shopify fallback, same as an
    # OSError -- an infra hiccup on this read path must not break the customer's turn.
    ingest = _FakeMirrorIngest(raises=asyncpg.PostgresError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.get_order("gid://1")

    assert result is not None
    assert result.name == "tavas-from-shopify"
    assert shopify.calls == ["get_order"]


async def test_get_order_programming_error_propagates_not_swallowed() -> None:
    # A genuine bug (e.g. a KeyError from a row-mapping regression) must NOT masquerade as a cache
    # miss forever -- it surfaces instead of silently falling through to Shopify on every read.
    ingest = _FakeMirrorIngest(raises=KeyError("row mapping bug"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    with pytest.raises(KeyError):
        await source.get_order("gid://1")
    assert shopify.calls == []


async def test_find_order_by_name_hit_never_calls_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_name=_order("gid://2", "tavas2"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_order_by_name("tavas2")

    assert result is not None
    assert result.gid == "gid://2"
    assert shopify.calls == []


async def test_find_order_by_name_miss_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(order_by_name=None)
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_order_by_name("tavas2")

    assert result is not None
    assert shopify.calls == ["find_order_by_name"]


async def test_find_order_by_name_db_error_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(raises=OSError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_order_by_name("tavas2")

    assert result is not None
    assert shopify.calls == ["find_order_by_name"]


async def test_find_customer_orders_by_phone_hit_never_calls_shopify() -> None:
    ingest = _FakeMirrorIngest(orders_by_phone=[_order("gid://3", "tavas3")])
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_customer_orders_by_phone("+919999999999")

    assert len(result) == 1
    assert result[0].gid == "gid://3"
    assert shopify.calls == []


async def test_find_customer_orders_by_phone_empty_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(orders_by_phone=[])
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_customer_orders_by_phone("+919999999999")

    assert len(result) == 1
    assert shopify.calls == ["find_customer_orders_by_phone"]


async def test_find_customer_orders_by_phone_db_error_falls_back_to_shopify() -> None:
    ingest = _FakeMirrorIngest(raises=OSError("db down"))
    shopify = _FakeShopify()
    source = MirrorOrderSource(ingest, shopify)

    result = await source.find_customer_orders_by_phone("+919999999999")

    assert len(result) == 1
    assert shopify.calls == ["find_customer_orders_by_phone"]
