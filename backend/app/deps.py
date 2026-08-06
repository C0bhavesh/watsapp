from dataclasses import dataclass

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.providers.base import LLMProvider
from app.providers.litellm_provider import LiteLLMProvider, VertexConfig
from app.providers.registry import get_provider
from app.shopify.client import ShopifyClient
from app.shopify.token_manager import TokenManager
from app.store.base import ConfigRepo, ConversationStore, IngestStore, MessageStore
from app.store.memory import (
    InMemoryConfigRepo,
    InMemoryConversationStore,
    InMemoryIngestStore,
    InMemoryMessageStore,
)
from app.store.pg_factory import LazyPool
from app.store.postgres import (
    PostgresConfigRepo,
    PostgresConversationStore,
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
    conversations: ConversationStore


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
            conversations: ConversationStore = PostgresConversationStore(pool)
        else:
            config_repo = InMemoryConfigRepo()
            ingest = InMemoryIngestStore()
            messages = InMemoryMessageStore()
            conversations = InMemoryConversationStore()
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient(follow_redirects=False)  # never replay the token to a redirect
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(
            settings, vault, config_repo, config, http, tokens, shopify, ingest, messages,
            conversations,
        )
    return _container


def reset_container() -> None:
    global _container
    _container = None


def build_provider(settings: Settings) -> LiteLLMProvider:
    """Construct the LLM verifier with Vertex env-credentials wired in.

    Built at call time (never at import), so the webhook cold path never pays for it and the
    Vertex service-account JSON is read from env-sourced settings only when a verify is requested.
    ``LiteLLMProvider`` still imports litellm lazily inside ``complete`` — nothing here triggers it.
    """
    vertex = VertexConfig(
        credentials_json=settings.vertex_credentials_json or None,
        project=settings.vertex_project or None,
        location=settings.vertex_location,
    )
    return LiteLLMProvider(vertex=vertex)


async def active_llm(
    settings: Settings, config: ConfigService
) -> tuple[LLMProvider, str, str, dict[str, object] | None] | None:
    """Resolve the LLM provider the owner activated in the admin panel, ready to call.

    Mirrors the admin panel's own resolution (`llm:active_provider` / `llm:api_key:{provider}`)
    so the conversation engine always uses whatever is currently configured there. Returns
    None if nothing is active yet, or an api_key provider is active but has no stored key.
    """
    active = await config.get_plain("llm:active_provider")
    # active is never "" in practice -- only real provider keys are written (see
    # admin/router.py::set_provider, which validates via get_provider() before any write); using
    # is None here to match the brief's spec.
    if active is None:
        return None
    info = get_provider(active)
    if info is None:
        return None
    provider = build_provider(settings)
    if info.auth_kind == "env":
        return provider, info.default_model, "", info.request_params
    api_key = await config.get_secret(f"llm:api_key:{active}")
    if api_key is None:
        return None
    return provider, info.default_model, api_key, info.request_params
