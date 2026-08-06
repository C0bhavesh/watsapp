import json

import httpx

from tests.test_client_graphql import grant_or, make_client, seed

ORDER_NODE = {
    "id": "gid://shopify/Order/12187547894128",
    "name": "tavas3733",
    "email": "c@example.com",
    "phone": "+919999999999",
    "tags": ["COD", "COD pending"],
    "paymentGatewayNames": ["Cash on Delivery (COD)"],
    "displayFinancialStatus": "PENDING",
    "displayFulfillmentStatus": "UNFULFILLED",
    "cancelledAt": None,
    "customerLocale": "en-IN",
    "totalPriceSet": {"shopMoney": {"amount": "949.0", "currencyCode": "INR"}},
    "shippingAddress": {"phone": "+918888888888"},
    "billingAddress": {"phone": None},
}


async def test_get_order_parses_full_node(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.name == "tavas3733"
    assert order.is_cod()
    assert order.best_phone() == "+919999999999"
    assert order.customer_locale == "en-IN"
    assert order.total is not None and order.total.currency == "INR"


async def test_get_order_missing_returns_none(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": None}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.get_order("gid://shopify/Order/1") is None


async def test_find_order_by_name_normalizes_and_queries(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"orders": {"edges": [{"node": ORDER_NODE}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.find_order_by_name("#3733")
    assert order is not None and order.gid.endswith("12187547894128")
    assert captured["variables"]["q"] == "name:tavas3733"


async def test_find_order_by_name_none_found(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.find_order_by_name("9999") is None


async def test_find_order_by_name_rejects_search_operators(settings, master_key) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "shpat_t1", "expires_in": 86399})
        return httpx.Response(200, json={"data": {"orders": {"edges": [{"node": ORDER_NODE}]}}})

    client, config = make_client(settings, master_key, handler)
    await seed(config)
    assert await client.find_order_by_name("3733 OR email:*") is None
    assert calls == []


async def test_customer_search_access_denied_returns_empty(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "Access denied for customers field."}], "data": None,
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.find_customer_orders_by_phone("+919999999999") == []


async def test_customer_search_rejects_search_operators(settings, master_key) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "shpat_t1", "expires_in": 86399})
        return httpx.Response(200, json={"data": {"customers": {"edges": []}}})

    client, config = make_client(settings, master_key, handler)
    await seed(config)
    assert await client.find_customer_orders_by_phone("+910000000000 OR email:x@y.z") == []
    assert calls == []


async def test_customer_search_non_numeric_id_skips_second_query(settings, master_key) -> None:
    gql_calls = {"n": 0}

    def gql(request: httpx.Request) -> httpx.Response:
        gql_calls["n"] += 1
        return httpx.Response(200, json={"data": {"customers": {"edges": [
            {"node": {"id": "gid://shopify/Customer/not-a-number"}}
        ]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.find_customer_orders_by_phone("+919999999999") == []
    assert gql_calls["n"] == 1


async def test_customer_search_access_denied_code_non_english(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "अनुमति अस्वीकृत", "extensions": {"code": "ACCESS_DENIED"}}],
            "data": None,
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.find_customer_orders_by_phone("+919999999999") == []


async def test_customer_search_two_step(settings, master_key) -> None:
    step = {"n": 0}

    def gql(request: httpx.Request) -> httpx.Response:
        step["n"] += 1
        if step["n"] == 1:
            return httpx.Response(200, json={"data": {"customers": {"edges": [
                {"node": {"id": "gid://shopify/Customer/77"}}
            ]}}})
        return httpx.Response(200, json={"data": {"orders": {"edges": [{"node": ORDER_NODE}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    orders = await client.find_customer_orders_by_phone("+919999999999")
    assert len(orders) == 1 and orders[0].name == "tavas3733"


PRODUCT_NODE = {
    "id": "gid://shopify/Product/1",
    "title": "Blue Chikankari Kurti",
    "handle": "blue-chikankari-kurti",
    "productType": "Kurti",
    "tags": ["chikankari", "blue"],
    "totalInventory": 12,
    "priceRangeV2": {"minVariantPrice": {"amount": "1299.0", "currencyCode": "INR"}},
}


async def test_search_products_parses_full_node(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"products": {"edges": [{"node": PRODUCT_NODE}]}}}
        )

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("blue kurti")
    assert len(results) == 1
    product = results[0]
    assert product.gid == "gid://shopify/Product/1"
    assert product.title == "Blue Chikankari Kurti"
    assert product.available is True
    assert product.price is not None and product.price.currency == "INR"


async def test_search_products_includes_status_active_in_query(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.search_products("kurti")
    assert "status:active" in captured["variables"]["q"]


async def test_search_products_zero_inventory_is_unavailable(settings, master_key) -> None:
    node = {**PRODUCT_NODE, "totalInventory": 0}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"products": {"edges": [{"node": node}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("kurti")
    assert results[0].available is False


async def test_search_products_untracked_inventory_defaults_available(settings, master_key) -> None:
    node = {**PRODUCT_NODE, "totalInventory": None}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"products": {"edges": [{"node": node}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("kurti")
    assert results[0].available is True


async def test_search_products_respects_limit(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.search_products("kurti", limit=2)
    assert captured["variables"]["first"] == 2


async def test_search_products_empty_query_returns_empty_without_calling_shopify(
    settings, master_key
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, handler)
    await seed(config)
    assert await client.search_products("") == []
    assert calls == []
