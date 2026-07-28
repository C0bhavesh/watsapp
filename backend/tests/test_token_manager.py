import asyncio

import httpx
import pytest

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.shopify.errors import TokenGrantError
from app.shopify.token_manager import TokenManager
from app.store.memory import InMemoryConfigRepo


def make_manager(settings, master_key, responder, now=lambda: 1_000_000.0):
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    return TokenManager(http, config, settings, now=now), config, calls


def ok_grant(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "shpat_test_token", "expires_in": 86399})


async def seed(config: ConfigService) -> None:
    await config.set_secret("shopify:client_id", "cid")
    await config.set_secret("shopify:client_secret", "csec")


async def test_grant_fetches_stores_and_caches(settings, master_key) -> None:
    mgr, config, calls = make_manager(settings, master_key, ok_grant)
    await seed(config)
    token = await mgr.get_token()
    assert token == "shpat_test_token"
    assert len(calls) == 1
    body = calls[0].content.decode()
    assert "grant_type=client_credentials" in body and "cid" in body
    assert await config.get_secret("shopify:access_token") == "shpat_test_token"
    # second call: cached, no new HTTP
    assert await mgr.get_token() == "shpat_test_token"
    assert len(calls) == 1


async def test_expired_store_token_triggers_refresh(settings, master_key) -> None:
    now = {"t": 1_000_000.0}
    mgr, config, calls = make_manager(settings, master_key, ok_grant, now=lambda: now["t"])
    await seed(config)
    await mgr.get_token()
    now["t"] += 86399 - 100  # inside the 1h refresh margin
    await mgr.get_token()
    assert len(calls) == 2


async def test_single_flight_concurrent_calls_one_grant(settings, master_key) -> None:
    mgr, config, calls = make_manager(settings, master_key, ok_grant)
    await seed(config)
    await asyncio.gather(mgr.get_token(), mgr.get_token(), mgr.get_token())
    assert len(calls) == 1


async def test_grant_failure_raises_without_leaking_secret(settings, master_key) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": "invalid client"})

    mgr, config, _ = make_manager(settings, master_key, bad)
    await seed(config)
    with pytest.raises(TokenGrantError) as exc_info:
        await mgr.get_token()
    assert "csec" not in str(exc_info.value)


async def test_missing_credentials_raise(settings, master_key) -> None:
    mgr, _config, _ = make_manager(settings, master_key, ok_grant)
    with pytest.raises(TokenGrantError):
        await mgr.get_token()


async def test_grant_non_json_response_raises(settings, master_key) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    mgr, config, _ = make_manager(settings, master_key, bad)
    await seed(config)
    with pytest.raises(TokenGrantError):
        await mgr.get_token()


async def test_corrupt_expires_at_falls_through_to_grant(settings, master_key) -> None:
    mgr, config, calls = make_manager(settings, master_key, ok_grant)
    await seed(config)
    await config.set_secret("shopify:access_token", "shpat_stale")
    await config.set_plain("shopify:token_expires_at", "not-a-float")
    token = await mgr.get_token()
    assert token == "shpat_test_token"
    assert len(calls) == 1


async def test_grant_missing_access_token_raises(settings, master_key) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 86399})

    mgr, config, _ = make_manager(settings, master_key, bad)
    await seed(config)
    with pytest.raises(TokenGrantError):
        await mgr.get_token()
