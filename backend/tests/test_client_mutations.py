import json

import httpx
import pytest

from app.shopify.errors import ShopifyGraphQLError
from app.shopify.models import AuthorizedOrder, CancelRequested
from tests.test_client_graphql import grant_or, make_client, seed
from tests.test_models import make_order

AUTH = AuthorizedOrder(order=make_order(), verified_phone="+919999999999")


async def test_add_tags_sends_gid_and_tags(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"tagsAdd": {"userErrors": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.add_tags(AUTH, ["confirmed"])
    assert captured["variables"] == {"id": AUTH.order.gid, "tags": ["confirmed"]}


async def test_add_tags_user_errors_raise(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"tagsAdd": {"userErrors": [
            {"message": "Order does not exist"}
        ]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyGraphQLError):
        await client.add_tags(AUTH, ["confirmed"])


async def test_cancel_order_returns_job_and_reads_typed_errors(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"orderCancel": {
            "job": {"id": "gid://shopify/Job/9"}, "orderCancelUserErrors": [],
        }}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await client.cancel_order(AUTH)
    assert result == CancelRequested(job_id="gid://shopify/Job/9")
    assert captured["variables"]["reason"] == "CUSTOMER"
    assert captured["variables"]["restock"] is True


async def test_cancel_order_user_errors_raise(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orderCancel": {
            "job": None,
            "orderCancelUserErrors": [{"message": "Order already cancelled"}],
        }}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyGraphQLError):
        await client.cancel_order(AUTH)
