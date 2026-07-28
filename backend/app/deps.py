from dataclasses import dataclass

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.client import ShopifyClient
from app.shopify.token_manager import TokenManager
from app.store.base import ConfigRepo, IngestStore, MessageStore
from app.store.memory import (
    InMemoryConfigRepo,
    InMemoryIngestStore,
    InMemoryMessageStore,
)
from app.store.pg_factory import LazyPool
from app.store.postgres import (
    PostgresConfigRepo,
    PostgresIngestStore,
    PostgresMessageStore,
)


@dataclass
class Container:
    settings: Settings
    vault: SecretVault
    config_repo: ConfigRepo
    config: ConfigService
    http: httpx.AsyncClient
    tokens: TokenManager
    shopify: ShopifyClient
    ingest: IngestStore
    messages: MessageStore


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        settings = Settings()  # type: ignore[call-arg]  # app_master_key comes from env/.env
        vault = SecretVault(settings.app_master_key)
        if settings.database_url:
            pool = LazyPool(settings.database_url)
            config_repo: ConfigRepo = PostgresConfigRepo(pool)
            ingest: IngestStore = PostgresIngestStore(pool)
            messages: MessageStore = PostgresMessageStore(pool)
        else:
            config_repo = InMemoryConfigRepo()
            ingest = InMemoryIngestStore()
            messages = InMemoryMessageStore()
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient(follow_redirects=False)  # never replay the token to a redirect
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(
            settings, vault, config_repo, config, http, tokens, shopify, ingest, messages
        )
    return _container


def reset_container() -> None:
    global _container
    _container = None
