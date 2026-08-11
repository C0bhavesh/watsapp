from app.shopify.client import ShopifyClient
from app.shopify.errors import ShopifyGraphQLError

REQUIRED_TOPICS: tuple[str, ...] = ("ORDERS_CREATE", "ORDERS_UPDATED", "CUSTOMERS_UPDATE")

_LIST_QUERY = (
    "query($topics: [WebhookSubscriptionTopic!]) { webhookSubscriptions(first: 20, "
    "topics: $topics) { edges { node { id topic apiVersion { handle } "
    "endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } } }"
)

_CREATE_MUTATION = (
    "mutation($topic: WebhookSubscriptionTopic!, $callbackUrl: URL!, $apiVersion: String!) "
    "{ webhookSubscriptionCreate(topic: $topic, webhookSubscription: {callbackUrl: "
    "$callbackUrl, apiVersion: $apiVersion, format: JSON}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)

_UPDATE_MUTATION = (
    "mutation($id: ID!, $callbackUrl: URL!, $apiVersion: String!) { webhookSubscriptionUpdate("
    "id: $id, webhookSubscription: {callbackUrl: $callbackUrl, apiVersion: $apiVersion}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)


def _raise_on_user_errors(node: dict) -> None:  # type: ignore[type-arg]
    errors = node.get("userErrors") or []
    if errors:
        raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])


async def _ensure_one_topic(
    client: ShopifyClient, topic: str, callback_url: str
) -> str:
    """Result for ONE topic: "ok" | "updated" | "created" | "error".

    A mutation's userErrors are converted to the flat "error" string rather than raised, so one
    topic's failure cannot abort the topics after it (CUSTOMERS_UPDATE is both the likeliest to
    fail -- protected-customer-data approval -- and the last in REQUIRED_TOPICS). The Shopify
    message is deliberately NOT interpolated into the result: this dict is returned by the
    /internal/jobs endpoint, and a fixed token cannot leak whatever an error string carries.
    """
    try:
        return await _ensure_one_topic_or_raise(client, topic, callback_url)
    except ShopifyGraphQLError:
        return "error"


async def _ensure_one_topic_or_raise(
    client: ShopifyClient, topic: str, callback_url: str
) -> str:
    # F20: a subscription is correct ONLY when the callbackUrl AND the bound API version both
    # match — otherwise a version bump silently strands the sub on the old version.
    version = client.api_version
    data = await client._graphql(_LIST_QUERY, {"topics": [topic]})
    edges = (data.get("webhookSubscriptions") or {}).get("edges") or []
    for edge in edges:
        node = edge["node"]
        endpoint = node.get("endpoint") or {}
        current_version = (node.get("apiVersion") or {}).get("handle")
        if endpoint.get("callbackUrl") == callback_url and current_version == version:
            return "ok"
        result = await client._graphql(
            _UPDATE_MUTATION,
            {"id": node["id"], "callbackUrl": callback_url, "apiVersion": version},
        )
        _raise_on_user_errors(result.get("webhookSubscriptionUpdate") or {})
        return "updated"
    result = await client._graphql(
        _CREATE_MUTATION, {"topic": topic, "callbackUrl": callback_url, "apiVersion": version}
    )
    _raise_on_user_errors(result.get("webhookSubscriptionCreate") or {})
    return "created"


async def ensure_subscription(client: ShopifyClient, callback_url: str) -> dict[str, str]:
    """Ensure every topic in REQUIRED_TOPICS has a correctly-configured subscription.

    Shopify webhook subscriptions are one-per-topic, so each topic is checked/created/updated
    independently -- a stale ORDERS_CREATE subscription doesn't block CUSTOMERS_UPDATE from
    being created, and vice versa. That independence covers FAILURES too: a topic whose
    mutation returns userErrors is reported as "error" in the returned dict, and the remaining
    topics are still attempted, so the result always has one entry per REQUIRED_TOPICS.
    """
    return {
        topic: await _ensure_one_topic(client, topic, callback_url)
        for topic in REQUIRED_TOPICS
    }
