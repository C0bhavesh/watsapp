from typing import Any

import httpx

from app.config.settings import Settings
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.shopify.token_manager import TokenManager


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
