"""Unit tests for deps.build_provider — pure, no network, no litellm import triggered."""

import pytest

from app.config.settings import Settings
from app.deps import build_provider
from app.providers.litellm_provider import LiteLLMProvider, VertexConfig


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
