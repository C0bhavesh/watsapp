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
    "shippingAddress": {
        "phone": "+918888888888", "address1": "12 MG Road", "address2": None,
        "city": "Bengaluru", "province": "Karnataka", "zip": "560001", "country": "India",
    },
    "billingAddress": {"phone": None},
    "customer": {
        "id": "gid://shopify/Customer/987654321", "firstName": "Suman", "lastName": "Bayala",
        "email": "c@example.com",
    },
    "lineItems": {
        "edges": [
            {
                "node": {
                    "title": "Blue Chikankari Kurti",
                    "quantity": 1,
                    "variant": {"title": "Blue / M"},
                    "sku": "KUR-BLU-M",
                    "originalUnitPriceSet": {
                        "shopMoney": {"amount": "999.00", "currencyCode": "INR"}
                    },
                }
            },
            {
                "node": {
                    "title": "Cotton Dupatta",
                    "quantity": 2,
                    "variant": {"title": "Red"},
                    "sku": None,
                    "originalUnitPriceSet": {
                        "shopMoney": {"amount": "150.00", "currencyCode": "INR"}
                    },
                }
            },
        ]
    },
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


async def test_get_order_parses_line_items(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert len(order.line_items) == 2
    first, second = order.line_items
    assert first.title == "Blue Chikankari Kurti"
    assert first.quantity == 1
    assert first.variant_title == "Blue / M"
    assert first.price is not None
    assert first.price.amount == "999.00"
    assert first.price.currency == "INR"
    assert second.title == "Cotton Dupatta"
    assert second.quantity == 2
    assert second.variant_title == "Red"
    assert second.price is not None and second.price.amount == "150.00"


async def test_get_order_line_item_without_variant_parses_variant_title_none(
    settings, master_key
) -> None:
    node = {
        **ORDER_NODE,
        "lineItems": {
            "edges": [
                {
                    "node": {
                        "title": "Gift Card",
                        "quantity": 1,
                        "variant": None,
                        "originalUnitPriceSet": {
                            "shopMoney": {"amount": "500.00", "currencyCode": "INR"}
                        },
                    }
                }
            ]
        },
    }

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": node}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert len(order.line_items) == 1
    assert order.line_items[0].variant_title is None


async def test_get_order_zero_line_items_parses_to_empty_tuple(settings, master_key) -> None:
    node = {**ORDER_NODE, "lineItems": {"edges": []}}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": node}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.line_items == ()


async def test_get_order_missing_line_items_key_parses_to_empty_tuple(settings, master_key) -> None:
    node = {k: v for k, v in ORDER_NODE.items() if k != "lineItems"}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": node}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.line_items == ()


async def test_get_order_parses_customer(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.customer is not None
    assert order.customer.gid == "gid://shopify/Customer/987654321"
    assert order.customer.first_name == "Suman"
    assert order.customer.city == "Bengaluru"
    assert order.customer.postal_code == "560001"


async def test_get_order_missing_customer_parses_none(settings, master_key) -> None:
    node = {k: v for k, v in ORDER_NODE.items() if k != "customer"}

    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": node}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.customer is None


async def test_get_order_parses_line_item_sku(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    first, second = order.line_items
    assert first.sku == "KUR-BLU-M"
    assert second.sku is None


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


async def test_search_products_empty_query_returns_none_without_calling_shopify(
    settings, master_key
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, handler)
    await seed(config)
    # None (not []) so the caller can tell "nothing searchable in the message" apart from
    # "searched and found nothing" -- an empty list would fabricate a negative answer.
    assert await client.search_products("") is None
    assert calls == []


async def test_search_products_sanitizes_search_operators_instead_of_rejecting(
    settings, master_key
) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    # Crafted malicious input with OR/status injection: the operators are stripped and the
    # remaining words are searched, rather than the whole query being refused.
    results = await client.search_products("wedding lehenga) OR status:archived OR (blue")
    assert results == []
    sent = captured["variables"]["q"]
    assert sent == "(wedding lehenga status archived blue) AND status:active"


async def test_search_products_operators_only_query_returns_none_without_calling_shopify(
    settings, master_key
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"products": {"edges": []}}})

    client, config = make_client(settings, master_key, handler)
    await seed(config)
    # Nothing searchable survives sanitization -> unusable input, not "no matches".
    assert await client.search_products('*** "" ()') is None
    assert calls == []


async def test_search_products_ordinary_customer_language_is_searched(
    settings, master_key
) -> None:
    """Real customer phrasing (and/or/not, apostrophes, colons) must never be refused."""
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"data": {"products": {"edges": [{"node": PRODUCT_NODE}]}}}
        )

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    results = await client.search_products("black and white women's saree, size: M")
    assert len(results) == 1
    assert results[0].title == "Blue Chikankari Kurti"
    assert captured["variables"]["q"] == "(black white womens saree, size M) AND status:active"


async def test_search_products_legitimate_free_text_works(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"products": {"edges": [{"node": PRODUCT_NODE}]}}}
        )

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    # Legitimate product description with English words and punctuation
    results = await client.search_products("blue kurti for wedding")
    assert results is not None and len(results) == 1
    assert results[0].title == "Blue Chikankari Kurti"


async def test_list_orders_created_since_pages_through_results(settings, master_key) -> None:
    page1 = {
        "orders": {
            "edges": [{"cursor": "c1", "node": ORDER_NODE}],
            "pageInfo": {"hasNextPage": True},
        }
    }
    page2_node = {**ORDER_NODE, "id": "gid://shopify/Order/second", "name": "tavas9999"}
    page2 = {
        "orders": {
            "edges": [{"cursor": "c2", "node": page2_node}],
            "pageInfo": {"hasNextPage": False},
        }
    }
    calls: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["variables"].get("cursor") is None:
            return httpx.Response(200, json={"data": page1})
        return httpx.Response(200, json={"data": page2})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    orders = [o async for o in client.list_orders_created_since("2025-08-10")]
    assert len(orders) == 2
    assert orders[0].gid == "gid://shopify/Order/12187547894128"
    assert orders[1].gid == "gid://shopify/Order/second"
    assert len(calls) == 2
    assert calls[1]["variables"]["cursor"] == "c1"


async def test_list_orders_created_since_empty_result(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": {
            "edges": [], "pageInfo": {"hasNextPage": False}}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    orders = [o async for o in client.list_orders_created_since("2025-08-10")]
    assert orders == []
