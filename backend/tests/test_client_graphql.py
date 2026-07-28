import httpx
import pytest

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.shopify.client import ShopifyClient
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.shopify.token_manager import TokenManager
from app.store.memory import InMemoryConfigRepo


def make_client(settings, master_key, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    tokens = TokenManager(http, config, settings)
    return ShopifyClient(http, tokens, settings), config


async def seed(config) -> None:
    await config.set_secret("shopify:client_id", "cid")
    await config.set_secret("shopify:client_secret", "csec")


def grant_or(payload_fn):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "shpat_t1", "expires_in": 86399})
        return payload_fn(request)

    return handler


async def test_graphql_sends_token_and_version(settings, master_key) -> None:
    seen: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("X-Shopify-Access-Token")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"ok": True}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    data = await client._graphql("{ shop { name } }")
    assert data == {"ok": True}
    assert seen["header"] == "shpat_t1"
    assert "/admin/api/2026-07/graphql.json" in seen["path"]


async def test_http_401_refreshes_once_then_raises(settings, master_key) -> None:
    count = {"gql": 0, "grants": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            count["grants"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"shpat_{count['grants']}", "expires_in": 86399},
            )
        count["gql"] += 1
        return httpx.Response(401, json={"errors": "unauthorized"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await seed(config)
    client = ShopifyClient(http, TokenManager(http, config, settings), settings)
    with pytest.raises(ShopifyAuthError):
        await client._graphql("{ shop { name } }")
    assert count["gql"] == 2 and count["grants"] == 2


async def test_throttled_maps_to_typed_error(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "data": None,
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyThrottled):
        await client._graphql("{ shop { name } }")


async def test_errors_with_null_data_raise_graphql_error(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": "Access denied for customers field."}], "data": None},
        )

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyGraphQLError) as e:
        await client._graphql("{ customers { id } }")
    assert "Access denied" in e.value.messages[0]


async def test_partial_data_with_errors_returns_data(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "Access denied for customer field."}],
            "data": {"orders": {"edges": []}},
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client._graphql("{ orders { edges } }") == {"orders": {"edges": []}}


async def test_network_error_maps_to_unavailable(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyUnavailable):
        await client._graphql("{ shop { name } }")


async def test_http_500_maps_to_unavailable(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>Internal Server Error</html>")

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyUnavailable):
        await client._graphql("{ shop { name } }")


async def test_http_429_maps_to_throttled(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests")

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyThrottled):
        await client._graphql("{ shop { name } }")


async def test_http_200_non_json_maps_to_unavailable(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyUnavailable):
        await client._graphql("{ shop { name } }")
