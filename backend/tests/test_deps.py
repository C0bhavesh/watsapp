"""Unit tests for deps.build_provider — pure, no network, no litellm import triggered."""

import pytest

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.deps import build_provider
from app.providers.litellm_provider import LiteLLMProvider, VertexConfig
from app.store.memory import InMemoryConfigRepo


def _config(master_key: str) -> ConfigService:
    return ConfigService(InMemoryConfigRepo(), SecretVault(master_key))


def test_build_provider_wires_vertex_config_from_settings(master_key: str) -> None:
    settings = Settings(
        app_master_key=master_key,
        vertex_credentials_json='{"sa":"json"}',
        vertex_project="proj-x",
        vertex_location="asia-south1",
        _env_file=None,
    )  # type: ignore[call-arg]

    provider = build_provider(settings)

    assert isinstance(provider, LiteLLMProvider)
    vertex = provider._vertex  # the attribute build_provider populates + complete() reads
    assert isinstance(vertex, VertexConfig)
    assert vertex.credentials_json == '{"sa":"json"}'
    assert vertex.project == "proj-x"
    assert vertex.location == "asia-south1"


def test_build_provider_coerces_empty_vertex_fields_to_none(
    master_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERTEX_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("VERTEX_PROJECT", raising=False)
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]

    vertex = build_provider(settings)._vertex

    assert vertex is not None
    assert vertex.credentials_json is None  # empty-string env default coerced to None
    assert vertex.project is None  # empty-string env default coerced to None
    assert vertex.location == "us-central1"  # non-empty default is preserved, not coerced


async def test_active_llm_returns_none_when_unconfigured(master_key: str) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    result = await active_llm(settings, _config(master_key))
    assert result is None


async def test_active_llm_returns_provider_for_api_key_provider(master_key: str) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    config = _config(master_key)
    await config.set_secret("llm:api_key:gemini", "test-key")
    await config.set_plain("llm:active_provider", "gemini")

    result = await active_llm(settings, config)

    assert result is not None
    provider, model, api_key, extra_params = result
    assert api_key == "test-key"
    assert model == "gemini/gemini-flash-latest"


async def test_active_llm_env_provider_needs_no_stored_key(master_key: str) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    config = _config(master_key)
    await config.set_plain("llm:active_provider", "vertex")

    result = await active_llm(settings, config)

    assert result is not None
    _, model, api_key, _ = result
    assert api_key == ""
    assert model == "vertex_ai/gemini-3.5-flash"


async def test_active_llm_returns_none_if_api_key_provider_has_no_stored_key(
    master_key: str,
) -> None:
    from app.deps import active_llm

    settings = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    config = _config(master_key)
    await config.set_plain("llm:active_provider", "gemini")

    result = await active_llm(settings, config)

    assert result is None


def test_container_has_conversations_store(
    master_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deps import get_container, reset_container

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    assert c.conversations is not None
    reset_container()
