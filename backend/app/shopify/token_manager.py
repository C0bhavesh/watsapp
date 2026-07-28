import asyncio
import json
import time
from collections.abc import Callable

import httpx

from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.errors import TokenGrantError

TOKEN_KEY = "shopify:access_token"
EXPIRES_KEY = "shopify:token_expires_at"
CLIENT_ID_KEY = "shopify:client_id"
CLIENT_SECRET_KEY = "shopify:client_secret"
REFRESH_MARGIN_SECONDS = 3600.0


class TokenManager:
    def __init__(
        self,
        http: httpx.AsyncClient,
        config: ConfigService,
        settings: Settings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._config = config
        self._settings = settings
        self._now = now
        self._lock = asyncio.Lock()
        self._cached_token: str | None = None
        self._cached_expires_at = 0.0

    def _fresh(self, expires_at: float) -> bool:
        return expires_at - self._now() > REFRESH_MARGIN_SECONDS

    async def get_token(self) -> str:
        if self._cached_token is not None and self._fresh(self._cached_expires_at):
            return self._cached_token
        async with self._lock:
            if self._cached_token is not None and self._fresh(self._cached_expires_at):
                return self._cached_token
            stored = await self._config.get_secret(TOKEN_KEY)
            expires_raw = await self._config.get_plain(EXPIRES_KEY)
            if stored is not None and expires_raw is not None:
                try:
                    expires_at = float(expires_raw)
                except (ValueError, TypeError):
                    expires_at = None
                if expires_at is not None and self._fresh(expires_at):
                    self._cached_token = stored
                    self._cached_expires_at = expires_at
                    return stored
            return await self._grant()

    async def force_refresh(self) -> str:
        async with self._lock:
            return await self._grant()

    async def _grant(self) -> str:
        client_id = await self._config.get_secret(CLIENT_ID_KEY)
        client_secret = await self._config.get_secret(CLIENT_SECRET_KEY)
        if not client_id or not client_secret:
            raise TokenGrantError("Shopify client credentials are not configured")
        url = f"https://{self._settings.shop_domain}/admin/oauth/access_token"
        try:
            resp = await self._http.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TokenGrantError("token endpoint unreachable") from exc
        if resp.status_code != 200:
            raise TokenGrantError(f"token grant rejected (HTTP {resp.status_code})")
        try:
            payload = resp.json()
            token = str(payload["access_token"])
            expires_at = self._now() + float(payload.get("expires_in", 86399))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TokenGrantError("malformed token response") from exc
        await self._config.set_secret(TOKEN_KEY, token)
        await self._config.set_plain(EXPIRES_KEY, str(expires_at))
        self._cached_token = token
        self._cached_expires_at = expires_at
        return token
