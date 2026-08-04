import pytest
from pydantic import ValidationError


def test_defaults(settings) -> None:
    assert settings.shop_domain == "thetavas.myshopify.com"
    assert settings.shopify_api_version == "2026-07"
    assert settings.database_url == ""
    assert settings.request_timeout_seconds == 20.0
    assert settings.app_env == "dev"


def test_vertex_defaults(settings) -> None:
    assert settings.vertex_credentials_json == ""
    assert settings.vertex_project == ""
    assert settings.vertex_location == "us-central1"


def test_vertex_from_env(master_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import Settings

    monkeypatch.setenv("VERTEX_CREDENTIALS_JSON", '{"sa":"json"}')
    monkeypatch.setenv("VERTEX_PROJECT", "my-proj")
    monkeypatch.setenv("VERTEX_LOCATION", "asia-south1")
    s = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    assert s.vertex_credentials_json == '{"sa":"json"}'
    assert s.vertex_project == "my-proj"
    assert s.vertex_location == "asia-south1"


def test_missing_master_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import Settings

    monkeypatch.delenv("APP_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
