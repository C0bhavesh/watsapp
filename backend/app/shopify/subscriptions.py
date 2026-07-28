from app.shopify.client import ShopifyClient
from app.shopify.errors import ShopifyGraphQLError

_LIST_QUERY = (
    "query { webhookSubscriptions(first: 20, topics: [ORDERS_CREATE]) { edges { node "
    "{ id topic endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } } }"
)

_CREATE_MUTATION = (
    "mutation($callbackUrl: URL!) { webhookSubscriptionCreate(topic: ORDERS_CREATE, "
    "webhookSubscription: {callbackUrl: $callbackUrl, format: JSON}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)

_UPDATE_MUTATION = (
    "mutation($id: ID!, $callbackUrl: URL!) { webhookSubscriptionUpdate(id: $id, "
    "webhookSubscription: {callbackUrl: $callbackUrl}) "
    "{ webhookSubscription { id } userErrors { message } } }"
)


def _raise_on_user_errors(node: dict) -> None:  # type: ignore[type-arg]
    errors = node.get("userErrors") or []
    if errors:
        raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])


async def ensure_subscription(client: ShopifyClient, callback_url: str) -> str:
    data = await client._graphql(_LIST_QUERY)
    edges = (data.get("webhookSubscriptions") or {}).get("edges") or []
    for edge in edges:
        node = edge["node"]
        endpoint = node.get("endpoint") or {}
        if endpoint.get("callbackUrl") == callback_url:
            return "ok"
        result = await client._graphql(
            _UPDATE_MUTATION, {"id": node["id"], "callbackUrl": callback_url}
        )
        _raise_on_user_errors(result.get("webhookSubscriptionUpdate") or {})
        return "updated"
    result = await client._graphql(_CREATE_MUTATION, {"callbackUrl": callback_url})
    _raise_on_user_errors(result.get("webhookSubscriptionCreate") or {})
    return "created"
