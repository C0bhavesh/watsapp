"""Dev-only live smoke test. Requires env: APP_MASTER_KEY, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET.

Read-only against the live store + mutation schema checks on a non-existent gid.
Run: python -m scripts.smoke_shopify
"""

import asyncio
import os

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.client import ShopifyClient
from app.shopify.errors import ShopifyGraphQLError
from app.shopify.models import AuthorizedOrder, Order
from app.shopify.token_manager import TokenManager
from app.store.memory import InMemoryConfigRepo


def _bogus_auth() -> AuthorizedOrder:
    order = Order(
        gid="gid://shopify/Order/1", name="tavas0", email=None, phone=None,
        shipping_phone=None, billing_phone=None, financial_status=None,
        fulfillment_status=None, cancelled_at=None, tags=(), payment_gateway_names=(),
        total=None, customer_locale=None,
    )
    return AuthorizedOrder(order=order, verified_phone="+910000000000")


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # app_master_key comes from env/.env
    config = ConfigService(InMemoryConfigRepo(), SecretVault(settings.app_master_key))
    await config.set_secret("shopify:client_id", os.environ["SHOPIFY_CLIENT_ID"])
    await config.set_secret("shopify:client_secret", os.environ["SHOPIFY_CLIENT_SECRET"])
    async with httpx.AsyncClient() as http:
        client = ShopifyClient(http, TokenManager(http, config, settings), settings)
        token = await client._tokens.get_token()  # noqa: SLF001
        print(f"token: {token[:10]}... OK")
        latest = await client.find_order_by_name("tavas3733")
        print(f"find_order_by_name: {'OK ' + latest.name if latest else 'NOT FOUND'}")
        if latest:
            refetched = await client.get_order(latest.gid)
            print(f"get_order: {'OK' if refetched and refetched.gid == latest.gid else 'FAIL'}")
        fallback = await client.find_customer_orders_by_phone("+910000000000")
        print(f"customer fallback (expect [] until read_customers granted): {fallback}")
        for label, call in (
            ("tagsAdd bogus-gid", client.add_tags(_bogus_auth(), ["smoke-test"])),
            ("orderCancel bogus-gid", client.cancel_order(_bogus_auth())),
        ):
            try:
                await call
                print(f"{label}: UNEXPECTED SUCCESS")
            except ShopifyGraphQLError as exc:
                print(f"{label}: OK (userError as expected: {exc.messages[0]})")


if __name__ == "__main__":
    asyncio.run(main())
