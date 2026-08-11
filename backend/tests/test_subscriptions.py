import json

import httpx

from app.shopify.subscriptions import REQUIRED_TOPICS, ensure_subscription
from tests.test_client_graphql import grant_or, make_client, seed


def sub_edge(url: str, topic: str = "ORDERS_CREATE", version: str = "2026-07") -> dict:
    return {"node": {"id": f"gid://shopify/WebhookSubscription/{topic}", "topic": topic,
                     "apiVersion": {"handle": version},
                     "endpoint": {"__typename": "WebhookHttpEndpoint", "callbackUrl": url}}}


async def test_all_topics_already_correct_makes_no_mutation(settings, master_key) -> None:
    calls: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify", topic=topic)]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result == {topic: "ok" for topic in REQUIRED_TOPICS}
    assert len(calls) == len(REQUIRED_TOPICS)  # one list query per topic, no mutations


async def test_missing_topic_is_created_independently(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/new"},
                "userErrors": []}}})
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        if topic == "ORDERS_CREATE":
            return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
                sub_edge("https://x.example/webhooks/shopify", topic="ORDERS_CREATE")]}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result["ORDERS_CREATE"] == "ok"
    assert result["ORDERS_UPDATED"] == "created"
    assert result["CUSTOMERS_UPDATE"] == "created"


async def test_wrong_url_subscription_is_updated_for_that_topic_only(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionUpdate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/updated"},
                "userErrors": []}}})
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        if topic == "ORDERS_CREATE":
            return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
                sub_edge("https://old.example/hook", topic="ORDERS_CREATE")]}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify", topic=topic)]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result["ORDERS_CREATE"] == "updated"
    assert result["ORDERS_UPDATED"] == "ok"
    assert result["CUSTOMERS_UPDATE"] == "ok"


async def test_stale_api_version_is_updated_even_with_correct_url(settings, master_key) -> None:
    # F20: a subscription whose callbackUrl already matches must STILL be updated if its bound
    # API version has drifted -- isolates the `current_version == version` half of the
    # correctness check from the callbackUrl half (test_wrong_url_... above only falsifies
    # the URL half).
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionUpdate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionUpdate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/updated"},
                "userErrors": []}}})
        topic = body["variables"].get("topics", ["ORDERS_CREATE"])[0]
        if topic == "ORDERS_CREATE":
            return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
                sub_edge("https://x.example/webhooks/shopify", topic="ORDERS_CREATE",
                          version="2025-10")]}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": [
            sub_edge("https://x.example/webhooks/shopify", topic=topic)]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await ensure_subscription(client, "https://x.example/webhooks/shopify")
    assert result["ORDERS_CREATE"] == "updated"
    assert result["ORDERS_UPDATED"] == "ok"
    assert result["CUSTOMERS_UPDATE"] == "ok"
    update_calls = [c for c in captured if "webhookSubscriptionUpdate" in c["query"]]
    assert len(update_calls) == 1
    assert update_calls[0]["variables"]["apiVersion"] == "2026-07"


async def test_create_sends_current_api_version(settings, master_key) -> None:
    captured: list[dict] = []

    def gql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "webhookSubscriptionCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/new"},
                "userErrors": []}}})
        return httpx.Response(200, json={"data": {"webhookSubscriptions": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await ensure_subscription(client, "https://x.example/webhooks/shopify")
    create_calls = [c for c in captured if "webhookSubscriptionCreate" in c["query"]]
    assert len(create_calls) == len(REQUIRED_TOPICS)
    assert all(c["variables"]["apiVersion"] == "2026-07" for c in create_calls)
