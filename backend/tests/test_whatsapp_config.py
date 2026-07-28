import pytest

from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.store.memory import InMemoryConfigRepo


async def _seeded_service(master_key: str) -> ConfigService:
    service = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await service.set_secret("whatsapp:access_token", "tok")
    await service.set_secret("whatsapp:app_secret", "sec")
    await service.set_secret("whatsapp:verify_token", "vtok")
    await service.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await service.set_plain("whatsapp:waba_id", "2454816495000045")
    await service.set_plain("whatsapp:api_version", "v23.0")
    return service


async def test_fully_configured_loads(master_key: str) -> None:
    service = await _seeded_service(master_key)
    cfg = await load_whatsapp_config(service)
    assert cfg == WhatsAppConfig(
        access_token="tok",
        app_secret="sec",
        verify_token="vtok",
        phone_number_id="1298805403309058",
        waba_id="2454816495000045",
        api_version="v23.0",
    )


def test_repr_does_not_leak_secrets() -> None:
    cfg = WhatsAppConfig(
        access_token="EAAsecrettoken",
        app_secret="app-secret-value",
        verify_token="verify-secret-value",
        phone_number_id="1298805403309058",
        waba_id="2454816495000045",
        api_version="v23.0",
    )
    text = repr(cfg)
    assert "EAAsecrettoken" not in text
    assert "app-secret-value" not in text
    assert "verify-secret-value" not in text
    # Non-secret fields remain useful for debugging.
    assert "1298805403309058" in text
    assert "v23.0" in text


def test_frozen_equality_still_works() -> None:
    a = WhatsAppConfig(
        access_token="tok", app_secret="sec", verify_token="vtok",
        phone_number_id="123", waba_id="456", api_version="v23.0",
    )
    b = WhatsAppConfig(
        access_token="tok", app_secret="sec", verify_token="vtok",
        phone_number_id="123", waba_id="456", api_version="v23.0",
    )
    assert a == b


@pytest.mark.parametrize(
    "missing_key",
    [
        "whatsapp:access_token",
        "whatsapp:app_secret",
        "whatsapp:verify_token",
        "whatsapp:phone_number_id",
        "whatsapp:waba_id",
        "whatsapp:api_version",
    ],
)
async def test_missing_any_field_returns_none(master_key: str, missing_key: str) -> None:
    service = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    seeded = await _seeded_service(master_key)
    for key in (
        "whatsapp:access_token", "whatsapp:app_secret", "whatsapp:verify_token",
        "whatsapp:phone_number_id", "whatsapp:waba_id", "whatsapp:api_version",
    ):
        if key == missing_key:
            continue
        is_secret = "token" in key or "secret" in key
        value = await (seeded.get_secret(key) if is_secret else seeded.get_plain(key))
        assert value is not None
        if "token" in key or key == "whatsapp:app_secret":
            await service.set_secret(key, value)
        else:
            await service.set_plain(key, value)
    assert await load_whatsapp_config(service) is None
