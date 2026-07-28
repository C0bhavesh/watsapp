from dataclasses import dataclass

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.client import ShopifyClient
from app.shopify.token_manager import TokenManager
from app.store.base import ConfigRepo
from app.store.memory import InMemoryConfigRepo


@dataclass
class Container:
    settings: Settings
    vault: SecretVault
    config_repo: ConfigRepo
    config: ConfigService
    http: httpx.AsyncClient
    tokens: TokenManager
    shopify: ShopifyClient


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        settings = Settings()  # type: ignore[call-arg]  # app_master_key comes from env/.env
        vault = SecretVault(settings.app_master_key)
        config_repo: ConfigRepo = InMemoryConfigRepo()  # Phase 2: Postgres when database_url set
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient(follow_redirects=False)  # never replay the token to a redirect
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(settings, vault, config_repo, config, http, tokens, shopify)
    return _container


def reset_container() -> None:
    global _container
    _container = None
