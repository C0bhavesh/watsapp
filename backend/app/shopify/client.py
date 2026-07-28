from collections.abc import Sequence
from typing import Any

import httpx

from app.config.settings import Settings
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.shopify.models import (
    AuthorizedOrder,
    CancelRequested,
    Money,
    Order,
    normalize_order_name,
)
from app.shopify.token_manager import TokenManager

ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    "displayFulfillmentStatus cancelledAt customerLocale "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone } billingAddress { phone }"
)


def _order_from_node(node: dict[str, Any]) -> Order:
    total_node = (node.get("totalPriceSet") or {}).get("shopMoney")
    return Order(
        gid=str(node["id"]),
        name=str(node["name"]),
        email=node.get("email"),
        phone=node.get("phone"),
        shipping_phone=(node.get("shippingAddress") or {}).get("phone"),
        billing_phone=(node.get("billingAddress") or {}).get("phone"),
        financial_status=node.get("displayFinancialStatus"),
        fulfillment_status=node.get("displayFulfillmentStatus"),
        cancelled_at=node.get("cancelledAt"),
        tags=tuple(node.get("tags") or ()),
        payment_gateway_names=tuple(node.get("paymentGatewayNames") or ()),
        total=Money(str(total_node["amount"]), str(total_node["currencyCode"]))
        if total_node
        else None,
        customer_locale=node.get("customerLocale"),
    )


class ShopifyClient:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenManager, settings: Settings) -> None:
        self._http = http
        self._tokens = tokens
        self._settings = settings

    @property
    def _url(self) -> str:
        s = self._settings
        return f"https://{s.shop_domain}/admin/api/{s.shopify_api_version}/graphql.json"

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self._tokens.get_token()
            try:
                resp = await self._http.post(
                    self._url,
                    json={"query": query, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": token},
                    timeout=self._settings.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise ShopifyUnavailable("network failure talking to Shopify") from exc
            if resp.status_code == 401:
                if attempt == 1:
                    await self._tokens.force_refresh()
                    continue
                raise ShopifyAuthError("Shopify rejected the token after refresh")
            payload = resp.json()
            errors = payload.get("errors")
            data = payload.get("data")
            if errors:
                messages = [str(e.get("message", "")) for e in errors]
                codes = {str(e.get("extensions", {}).get("code", "")) for e in errors}
                if "THROTTLED" in codes:
                    raise ShopifyThrottled("; ".join(messages))
                if data is None:
                    raise ShopifyGraphQLError(messages)
            if data is None:
                raise ShopifyGraphQLError(["empty response data"])
            return dict(data)
        raise ShopifyAuthError("unreachable")  # pragma: no cover

    async def get_order(self, gid: str) -> Order | None:
        query = f"query($id: ID!) {{ node(id: $id) {{ ... on Order {{ {ORDER_FIELDS} }} }} }}"
        data = await self._graphql(query, {"id": gid})
        node = data.get("node")
        return _order_from_node(node) if node else None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        query = (
            f"query($q: String!) {{ orders(first: 1, query: $q) "
            f"{{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }}"
        )
        data = await self._graphql(query, {"q": f"name:{name}"})
        edges = (data.get("orders") or {}).get("edges") or []
        return _order_from_node(edges[0]["node"]) if edges else None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        try:
            cust = await self._graphql(
                'query($q: String!) { customers(first: 1, query: $q) '
                '{ edges { node { id } } } }',
                {"q": f"phone:{phone_e164}"},
            )
        except ShopifyGraphQLError as exc:
            if any("access denied" in m.lower() for m in exc.messages):
                return []
            raise
        edges = (cust.get("customers") or {}).get("edges") or []
        if not edges:
            return []
        customer_id = str(edges[0]["node"]["id"]).rsplit("/", 1)[-1]
        data = await self._graphql(
            f"query($q: String!) {{ orders(first: 10, query: $q, sortKey: CREATED_AT, "
            f"reverse: true) {{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }}",
            {"q": f"customer_id:{customer_id}"},
        )
        return [_order_from_node(e["node"]) for e in (data.get("orders") or {}).get("edges") or []]

    async def add_tags(self, auth: AuthorizedOrder, tags: Sequence[str]) -> None:
        data = await self._graphql(
            "mutation($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) "
            "{ userErrors { message } } }",
            {"id": auth.order.gid, "tags": list(tags)},
        )
        errors = (data.get("tagsAdd") or {}).get("userErrors") or []
        if errors:
            raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])

    async def cancel_order(
        self, auth: AuthorizedOrder, *, reason: str = "CUSTOMER", restock: bool = True
    ) -> CancelRequested:
        data = await self._graphql(
            "mutation($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!) "
            "{ orderCancel(orderId: $orderId, reason: $reason, restock: $restock) "
            "{ job { id } orderCancelUserErrors { message } } }",
            {"orderId": auth.order.gid, "reason": reason, "restock": restock},
        )
        node = data.get("orderCancel") or {}
        errors = node.get("orderCancelUserErrors") or []
        if errors:
            raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])
        job = node.get("job") or {}
        return CancelRequested(job_id=job.get("id"))
