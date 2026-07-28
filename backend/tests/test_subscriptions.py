import json

import httpx

from app.shopify.subscriptions import ensure_subscription
from tests.test_client_graphql import grant_or, make_client, seed


def sub_edge(url: str, version: str = "2026-07") -> dict:
    return {"node": {"id": "gid://shopify/WebhookSubscription/5", "topic": "ORDERS_CREATE",
                     "apiVersion": {"handle": version},
                     "endpoint": {"__typename": "WebhookHttpEndpoint", "callbackUrl": url}}}


async def test_existing_correct_subscription_is_ok(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify")]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "ok"


async def test_missing_subscription_is_created(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/9"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "created"
    create_call = captured[-1]
    assert create_call["variables"]["callbackUrl"] == "https://x.example/webhooks/shopify"


async def test_wrong_url_subscription_is_updated(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionUpdate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/5"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://old.example/hook")]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "updated"
    assert captured[-1]["variables"]["id"] == "gid://shopify/WebhookSubscription/5"


async def test_correct_url_but_stale_api_version_is_updated(settings, master_key) -> None:
    # F20: a sub still bound to an OLD Shopify API version must be re-pointed even though
    # its callbackUrl already matches.
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionUpdate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/5"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify", version="2025-10")]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "updated"
    assert captured[-1]["variables"]["id"] == "gid://shopify/WebhookSubscription/5"
    assert captured[-1]["variables"]["apiVersion"] == "2026-07"


async def test_create_sends_current_api_version(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/9"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "created"
    assert captured[-1]["variables"]["apiVersion"] == "2026-07"


async def test_correct_url_and_version_makes_no_mutation(settings, master_key) -> None:
    calls: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify")]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await ensure_subscription(client, "https://x.example/webhooks/shopify") == "ok"
    assert len(calls) == 1  # only the list query — no create/update mutation fired
