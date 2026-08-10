import json
import re
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
    LineItem,
    Money,
    Order,
    Product,
    normalize_order_name,
)
from app.shopify.token_manager import TokenManager

ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    "displayFulfillmentStatus cancelledAt customerLocale "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone } billingAddress { phone } "
    # first: 50 is a query-time ceiling far above any realistic order size -- the display side
    # (order_tracking) shows every item with no further cap, by design (owner's explicit
    # choice: show all, don't summarize/truncate a customer's own order).
    "lineItems(first: 50) { edges { node { title quantity variant { title } "
    "originalUnitPriceSet { shopMoney { amount currencyCode } } } } }"
)


def _line_items_from_node(node: dict[str, Any]) -> tuple[LineItem, ...]:
    edges = (node.get("lineItems") or {}).get("edges") or []
    items: list[LineItem] = []
    for edge in edges:
        item_node = edge.get("node") or {}
        price_node = (item_node.get("originalUnitPriceSet") or {}).get("shopMoney")
        variant = item_node.get("variant") or {}
        items.append(
            LineItem(
                title=str(item_node.get("title", "")),
                quantity=int(item_node.get("quantity") or 0),
                variant_title=variant.get("title"),
                price=(
                    Money(amount=price_node["amount"], currency=price_node["currencyCode"])
                    if price_node
                    else None
                ),
            )
        )
    return tuple(items)


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
        line_items=_line_items_from_node(node),
    )


PRODUCT_FIELDS = (
    "id title handle productType tags totalInventory "
    "priceRangeV2 { minVariantPrice { amount currencyCode } }"
)


# Shopify search-syntax metacharacters, split by how they should be removed. Quotes bind
# INSIDE a word, so dropping them keeps the word whole ("women's" -> "womens"); parens, `:`
# (field prefixes like status:/title:), `*` (wildcard) and `\` (escape -- it could otherwise
# escape the closing paren of the wrapper `search_products` builds) separate terms, so they
# become a space rather than fusing two words into a nonsense token.
_QUOTE_CHARS_RE = re.compile(r"[\"']")
_UNSAFE_CHARS_RE = re.compile(r"[*:()\\]")
# Standalone boolean operators only -- "andaman" and "order" keep their letters.
_BOOLEAN_TOKEN_RE = re.compile(r"\b(?:and|or|not)\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_product_query(query: str) -> str | None:
    """Strip Shopify search-operator syntax from a customer message, keeping the rest.

    Rejecting a whole query on the first metacharacter told customers the store had nothing
    for ordinary phrasing ("black and white saree", "women's kurti", "size: M") -- a fabricated
    negative, since a rejected query and a genuinely empty catalog search were indistinguishable
    to the caller. Sanitizing searches the safe remainder instead.

    Returns None when nothing searchable survives, so a caller can tell "your message had no
    searchable product term" apart from "searched and found nothing" (an empty list).
    """
    cleaned = _QUOTE_CHARS_RE.sub("", query)
    cleaned = _UNSAFE_CHARS_RE.sub(" ", cleaned)
    cleaned = _BOOLEAN_TOKEN_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _product_from_node(node: dict[str, Any]) -> Product:
    price_node = (node.get("priceRangeV2") or {}).get("minVariantPrice")
    total_inventory = node.get("totalInventory")
    # None (untracked inventory) defaults to available; a real 0 means out of stock.
    available = total_inventory is None or total_inventory > 0
    return Product(
        gid=str(node["id"]),
        title=str(node["title"]),
        handle=str(node.get("handle") or ""),
        price=Money(str(price_node["amount"]), str(price_node["currencyCode"]))
        if price_node
        else None,
        available=available,
        product_type=node.get("productType"),
        tags=tuple(node.get("tags") or ()),
    )


class ShopifyClient:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenManager, settings: Settings) -> None:
        self._http = http
        self._tokens = tokens
        self._settings = settings

    @property
    def api_version(self) -> str:
        return self._settings.shopify_api_version

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
            if resp.status_code >= 500:
                raise ShopifyUnavailable(f"Shopify returned HTTP {resp.status_code}")
            if resp.status_code == 429:
                raise ShopifyThrottled("Shopify throttled the request (HTTP 429)")
            if resp.status_code != 200:
                raise ShopifyUnavailable(f"Shopify returned HTTP {resp.status_code}")
            try:
                payload = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ShopifyUnavailable("non-JSON response") from exc
            errors = payload.get("errors")
            data = payload.get("data")
            if errors:
                messages = [str(e.get("message", "")) for e in errors]
                codes = {str(e.get("extensions", {}).get("code", "")) for e in errors}
                if "THROTTLED" in codes:
                    raise ShopifyThrottled("; ".join(messages))
                if data is None:
                    raise ShopifyGraphQLError(messages, tuple(c for c in codes if c))
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
        if re.fullmatch(r"[a-z0-9]+", name) is None:
            return None
        query = (
            f"query($q: String!) {{ orders(first: 1, query: $q) "
            f"{{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }}"
        )
        data = await self._graphql(query, {"q": f"name:{name}"})
        edges = (data.get("orders") or {}).get("edges") or []
        return _order_from_node(edges[0]["node"]) if edges else None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        if re.fullmatch(r"\+[1-9]\d{7,14}", phone_e164) is None:
            return []
        try:
            cust = await self._graphql(
                'query($q: String!) { customers(first: 1, query: $q) '
                '{ edges { node { id } } } }',
                {"q": f"phone:{phone_e164}"},
            )
        except ShopifyGraphQLError as exc:
            if "ACCESS_DENIED" in exc.codes or any(
                "access denied" in m.lower() for m in exc.messages
            ):
                return []
            raise
        edges = (cust.get("customers") or {}).get("edges") or []
        if not edges:
            return []
        customer_id = str(edges[0]["node"]["id"]).rsplit("/", 1)[-1]
        if re.fullmatch(r"\d+", customer_id) is None:
            return []
        data = await self._graphql(
            f"query($q: String!) {{ orders(first: 10, query: $q, sortKey: CREATED_AT, "
            f"reverse: true) {{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }}",
            {"q": f"customer_id:{customer_id}"},
        )
        return [_order_from_node(e["node"]) for e in (data.get("orders") or {}).get("edges") or []]

    async def search_products(self, query: str, limit: int = 5) -> list[Product] | None:
        """Search the live catalog. ``None`` = the query had no searchable term (never
        searched); ``[]`` = searched and the catalog genuinely had no match."""
        sanitized = sanitize_product_query(query)
        if sanitized is None:
            return None
        # status:active only -- never surface a draft/archived product to a customer.
        gql_query = f"({sanitized}) AND status:active"
        data = await self._graphql(
            f"query($q: String!, $first: Int!) {{ products(first: $first, query: $q) "
            f"{{ edges {{ node {{ {PRODUCT_FIELDS} }} }} }} }}",
            {"q": gql_query, "first": limit},
        )
        edges = (data.get("products") or {}).get("edges") or []
        return [_product_from_node(e["node"]) for e in edges]

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
