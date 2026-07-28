import pytest
from pydantic import ValidationError


def test_defaults(settings) -> None:
    assert settings.shop_domain == "thetavas.myshopify.com"
    assert settings.shopify_api_version == "2026-07"
    assert settings.database_url == ""
    assert settings.request_timeout_seconds == 20.0
    assert settings.app_env == "dev"


def test_missing_master_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import Settings

    monkeypatch.delenv("APP_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
