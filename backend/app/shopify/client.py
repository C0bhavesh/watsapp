import asyncio
import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config.settings import Settings
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.shopify.models import (
    AuthorizedOrder,
    CancelRequested,
    Customer,
    Fulfillment,
    FulfillmentEvent,
    LineItem,
    Money,
    Order,
    Product,
    normalize_order_name,
)
from app.shopify.token_manager import TokenManager

BACKFILL_THROTTLE_SLEEP_SECONDS = 2

# The product-image URL is the ONE field pulled from a Shopify response that becomes a WhatsApp
# template header, so Meta's servers fetch whatever URL lands there -- an SSRF-adjacent sink -- and
# the value is persisted to the outbox payload_json and can be replayed up to 24h later (reminder
# job). Hold it to the same "cap/validate everything untrusted" posture as every other Shopify
# field (the MAX_FIELD_LEN clip pattern): require https, a Shopify CDN host, and a bounded length.
_MAX_IMAGE_URL_LEN = 2048
_SHOPIFY_IMAGE_HOST_SUFFIX = ".shopify.com"


def _is_shopify_image_url(url: str) -> bool:
    """True only for a bounded https URL on a ``*.shopify.com`` host (Shopify's product-image CDN).

    Dot-anchored suffix so a lookalike host (``cdn.shopify.com.evil.com``, ``evil-shopify.com``)
    is rejected, not just a substring match. Any failure returns False so the caller degrades to no
    header (Q19a graceful degrade) -- never an exception.
    """
    if len(url) > _MAX_IMAGE_URL_LEN:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = parts.hostname
    return host is not None and host.endswith(_SHOPIFY_IMAGE_HOST_SUFFIX)

ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    # updatedAt (order AND customer) is not display data -- it is the mirror's out-of-order
    # write guard. Without selecting it, every row this path writes lands with
    # `updated_at = NULL`, which the guard treats as "always overwritable", so a later stale
    # webhook replay could revert it.
    "displayFulfillmentStatus cancelledAt customerLocale createdAt updatedAt "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone address1 address2 city province zip country } "
    "billingAddress { phone } "
    "customer { id firstName lastName email updatedAt } "
    # first: 50 is a query-time ceiling far above any realistic order size -- the display side
    # (order_tracking) shows every item with no further cap, by design (owner's explicit
    # choice: show all, don't summarize/truncate a customer's own order).
    "lineItems(first: 50) { edges { node { title quantity variant { title } sku "
    "originalUnitPriceSet { shopMoney { amount currencyCode } } } } }"
)

# Courier/tracking for the order-tracking Q&A (Q10), fetched by a SEPARATE, isolated query
# (get_order_fulfillments) rather than inline in ORDER_FIELDS. This decoupling is a hard safety
# requirement: reading fulfillments needs the read_fulfillments scope, and if that scope is
# missing/revoked Shopify returns ACCESS_DENIED for the WHOLE query it appears in -- inlining it
# would take down get_order/find_order_by_name (and every Confirm/Cancel tap + reconcile).
# Order.fulfillments is a plain LIST (not an edges/node connection); an order can have several
# (split shipments); trackingInfo is itself a list (rarely >1 number) -- keep the first, matching
# the one-tracking-per-row mirror schema. `id` is selected so the live-path Fulfillment carries
# its real gid; `updatedAt` drives the mirror's out-of-order-delivery guard.
# `deliveredAt` is the actual delivery timestamp (capture-and-store only -- the REST webhook
# payload carries no equivalent field, so this GraphQL read is the ONLY path that can populate
# Fulfillment.delivered_at).
# `displayStatus` + the `events` timeline (both GraphQL-only, no webhook equivalent) drive the
# RTO-aware "genuinely delivered?" rule: displayStatus distinguishes DELIVERED from
# ATTEMPTED_DELIVERY, and events (oldest-first by HAPPENED_AT) reveals a delivered-then-returned
# shipment. `first: 250` covers a courier's full scan history in one page (no timeline paginates
# that far in practice).
FULFILLMENT_FIELDS = (
    "fulfillments(first: 5) { id status displayStatus "
    "trackingInfo(first: 3) { company number url } createdAt updatedAt deliveredAt "
    "events(first: 250, sortKey: HAPPENED_AT) { nodes { status happenedAt } } }"
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
                sku=item_node.get("sku"),
            )
        )
    return tuple(items)


def _customer_from_node(node: dict[str, Any]) -> Customer | None:
    customer = node.get("customer")
    if not isinstance(customer, dict):
        return None
    gid = customer.get("id")
    if not isinstance(gid, str) or not gid:
        return None
    shipping = node.get("shippingAddress") or {}
    return Customer(
        gid=gid,
        first_name=customer.get("firstName"),
        last_name=customer.get("lastName"),
        email=customer.get("email"),
        phone=None,
        address_line1=shipping.get("address1"),
        address_line2=shipping.get("address2"),
        city=shipping.get("city"),
        state=shipping.get("province"),
        postal_code=shipping.get("zip"),
        country=shipping.get("country"),
        updated_at=customer.get("updatedAt"),
    )


def _fulfillments_from_node(node: dict[str, Any]) -> tuple[Fulfillment, ...]:
    # Order.fulfillments is a plain list ([Fulfillment!]!), not an edges/node connection.
    raw = node.get("fulfillments") or []
    result: list[Fulfillment] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        info_list = f.get("trackingInfo") or []
        # Keep the first tracking entry (a fulfillment rarely has more than one), matching the
        # single-tracking-per-row mirror schema.
        info = info_list[0] if info_list and isinstance(info_list[0], dict) else {}
        # events is a connection ({ nodes: [...] }); guard every level so a missing/short payload
        # (or a webhook-parsed node, which never carries it) degrades to an empty tuple.
        event_nodes = (f.get("events") or {}).get("nodes") or []
        events = tuple(
            FulfillmentEvent(
                status=str(e.get("status") or ""),
                happened_at=str(e.get("happenedAt") or ""),
            )
            for e in event_nodes
            if isinstance(e, dict)
        )
        result.append(
            Fulfillment(
                gid=str(f.get("id") or ""),
                status=f.get("status"),
                tracking_company=info.get("company"),
                tracking_number=info.get("number"),
                tracking_url=info.get("url"),
                created_at=f.get("createdAt"),
                updated_at=f.get("updatedAt"),
                delivered_at=f.get("deliveredAt"),
                display_status=f.get("displayStatus"),
                events=events,
            )
        )
    return tuple(result)


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
        customer=_customer_from_node(node),
        updated_at=node.get("updatedAt"),  # selected by ORDER_FIELDS; see the note there
        created_at=node.get("createdAt"),  # selected by ORDER_FIELDS; see the note there
        # fulfillments are fetched separately (get_order_fulfillments) and attached by the caller,
        # so a missing read_fulfillments scope can never fail the core order query.
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

    async def get_order_fulfillments(self, gid: str) -> tuple[Fulfillment, ...]:
        """Fetch ONLY an order's fulfillments (courier/tracking), fully isolated from the core
        order query.

        Reading fulfillments requires the ``read_fulfillments`` scope; when it is missing/revoked
        Shopify returns ACCESS_DENIED for the whole query. This method queries the fulfillments
        sub-tree on its own and swallows any ``ShopifyError`` (ACCESS_DENIED, throttle, outage)
        into an empty tuple, so a missing scope can NEVER take down order lookups -- it only
        silently withholds tracking. Deliberately best-effort: callers attach the result to an
        already-fetched Order.
        """
        query = f"query($id: ID!) {{ order(id: $id) {{ {FULFILLMENT_FIELDS} }} }}"
        try:
            data = await self._graphql(query, {"id": gid})
        except ShopifyError:
            return ()
        node = data.get("order")
        if not isinstance(node, dict):
            return ()
        return _fulfillments_from_node(node)

    async def get_product_image_url(
        self, product_gid: str, variant_gid: str | None = None
    ) -> str | None:
        """Fetch the ordered VARIANT's own image for the cod_confirmation template header,
        falling back to the product's shared featured image when the variant has no dedicated
        photo of its own -- Shopify returns a null variant image for a colour that shares the
        product's one photo (normal, not an error). Bug fix (2026-08-20): the header previously
        always came from the product's featuredImage, so every colour of a multi-colour product
        showed the same (default) photo regardless of which colour was actually ordered.

        When ``variant_gid`` is available both gids are resolved in ONE GraphQL query, so the
        existing <5s webhook-ack timeout budget is not doubled by a second round trip. When it is
        unavailable (e.g. a legacy/malformed payload with no variant_id), this falls back to the
        exact product-only query used before this fix -- a genuine regression guard, not just a
        subset of the combined query.

        Best-effort and fully isolated (same posture as ``get_order_fulfillments``): any
        ``ShopifyError`` (product/variant missing, ACCESS_DENIED, throttle, outage) or an
        image-less product/variant degrades to ``None`` so the confirmation still sends without a
        header — a Shopify hiccup must never block the customer's message (Q19a). The URL is
        validated at THIS source (``_is_shopify_image_url``: https + ``*.shopify.com`` host +
        length cap) regardless of which field it came from — the real check, since the value is
        then persisted and fetched by Meta; the downstream https-prefix checks are only
        defense-in-depth.
        """
        if variant_gid:
            query = (
                "query($vid: ID!, $pid: ID!) { "
                "productVariant(id: $vid) { image { url } } "
                "product(id: $pid) { featuredImage { url } } }"
            )
            variables: dict[str, Any] = {"vid": variant_gid, "pid": product_gid}
        else:
            query = "query($id: ID!) { product(id: $id) { featuredImage { url } } }"
            variables = {"id": product_gid}
        try:
            data = await self._graphql(query, variables)
        except ShopifyError:
            return None
        if variant_gid:
            variant_node = data.get("productVariant")
            if isinstance(variant_node, dict):
                variant_image = variant_node.get("image")
                if isinstance(variant_image, dict):
                    variant_url = variant_image.get("url")
                    if isinstance(variant_url, str) and _is_shopify_image_url(variant_url):
                        return variant_url
        node = data.get("product")
        if not isinstance(node, dict):
            return None
        image = node.get("featuredImage")
        if not isinstance(image, dict):
            return None
        url = image.get("url")
        if isinstance(url, str) and _is_shopify_image_url(url):
            return url
        return None

    async def _with_fulfillments(self, order: Order) -> Order:
        """Best-effort attach live tracking AFTER the core order fetch already succeeded.

        Skip the sub-fetch entirely for an order that cannot have fulfillments yet
        (displayFulfillmentStatus None/UNFULFILLED) -- there is nothing to fetch, so issuing
        get_order_fulfillments would be a pure-latency round trip (and, while read_fulfillments
        is not granted, a guaranteed ACCESS_DENIED). Most COD order-status questions are about
        pending/unfulfilled orders, so this removes nearly all the extra calls; only a
        (partially) fulfilled order actually queries for tracking.
        """
        if order.fulfillment_status in (None, "UNFULFILLED"):
            return order
        return replace(order, fulfillments=await self.get_order_fulfillments(order.gid))

    async def get_order(self, gid: str) -> Order | None:
        query = f"query($id: ID!) {{ node(id: $id) {{ ... on Order {{ {ORDER_FIELDS} }} }} }}"
        data = await self._graphql(query, {"id": gid})
        node = data.get("node")
        if not node:
            return None
        return await self._with_fulfillments(_order_from_node(node))

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
        if not edges:
            return None
        return await self._with_fulfillments(_order_from_node(edges[0]["node"]))

    async def _page_with_backoff(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        """One backfill page, retried once after a throttle.

        Paging a year of orders spends a lot of the leaky bucket; the backfill is a one-time
        manual script, so a fixed short sleep and a single retry is enough (no jitter/backoff
        curve needed). A second throttle propagates -- the operator re-runs the script.
        """
        try:
            return await self._graphql(query, variables)
        except ShopifyThrottled:
            await asyncio.sleep(BACKFILL_THROTTLE_SLEEP_SECONDS)
            return await self._graphql(query, variables)

    async def list_orders_created_since(self, since_iso: str) -> AsyncIterator[Order]:
        """Page through every order created on or after ``since_iso`` (a date string like
        "2025-08-10"), yielding each as an ``Order``. Used only by the one-time backfill
        script -- not part of any live customer-facing read path.

        ``first: 10`` (not 50) because ORDER_FIELDS selects ``lineItems(first: 50)``: the two
        multiply, and Shopify rejects any single query over 1000 cost points with
        MAX_COST_EXCEEDED (50 x 51 = ~2550 could never run; 10 x 51 = ~510 is safe). Fulfillments
        are NOT part of ORDER_FIELDS (they are fetched by a separate isolated query), so they add
        nothing to this page's cost -- the backfill script attaches them per order afterwards.
        Every other ORDER_FIELDS caller uses first: 1/10, so this pager is the only one at risk.
        """
        query = (
            "query($q: String!, $cursor: String) { orders(first: 10, after: $cursor, "
            f"query: $q) {{ edges {{ cursor node {{ {ORDER_FIELDS} }} }} "
            "pageInfo { hasNextPage } } }"
        )
        cursor: str | None = None
        search = f"created_at:>={since_iso}"
        while True:
            data = await self._page_with_backoff(query, {"q": search, "cursor": cursor})
            connection = data.get("orders") or {}
            edges = connection.get("edges") or []
            for edge in edges:
                yield _order_from_node(edge["node"])
            if not edges or not (connection.get("pageInfo") or {}).get("hasNextPage"):
                return
            cursor = edges[-1]["cursor"]

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
        orders = [
            _order_from_node(e["node"]) for e in (data.get("orders") or {}).get("edges") or []
        ]
        # Attach tracking best-effort AFTER the core orders resolved -- get_order_fulfillments
        # swallows a missing-scope ACCESS_DENIED, so this never fails the phone lookup.
        # Fan the (up to 10) per-order fulfillment fetches out CONCURRENTLY rather than awaiting
        # them one at a time -- a customer-facing Q&A path must not serialize 10 round trips.
        # _with_fulfillments itself skips the fetch for None/UNFULFILLED orders, so in the common
        # case (pending COD orders) most of these resolve without any extra call at all.
        return list(await asyncio.gather(*(self._with_fulfillments(o) for o in orders)))

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
