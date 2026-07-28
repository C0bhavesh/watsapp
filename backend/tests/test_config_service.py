from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.store.memory import InMemoryConfigRepo


async def test_plain_roundtrip(master_key: str) -> None:
    svc = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    assert await svc.get_plain("missing") is None
    await svc.set_plain("shopify:api_version", "2026-07")
    assert await svc.get_plain("shopify:api_version") == "2026-07"


async def test_secret_is_encrypted_at_rest(master_key: str) -> None:
    repo = InMemoryConfigRepo()
    svc = ConfigService(repo, SecretVault(master_key))
    await svc.set_secret("shopify:client_secret", "shpss_dummy_value")
    raw = await repo.get("shopify:client_secret")
    assert raw is not None and "shpss_dummy_value" not in raw
    assert await svc.get_secret("shopify:client_secret") == "shpss_dummy_value"


async def test_get_secret_missing_returns_none(master_key: str) -> None:
    svc = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    assert await svc.get_secret("nope") is None
