from app.config.crypto import SecretVault
from app.store.base import ConfigRepo


class ConfigService:
    def __init__(self, repo: ConfigRepo, vault: SecretVault) -> None:
        self._repo = repo
        self._vault = vault

    async def get_plain(self, key: str) -> str | None:
        return await self._repo.get(key)

    async def set_plain(self, key: str, value: str) -> None:
        await self._repo.set(key, value)

    async def get_secret(self, key: str) -> str | None:
        raw = await self._repo.get(key)
        return None if raw is None else self._vault.decrypt(raw)

    async def set_secret(self, key: str, value: str) -> None:
        await self._repo.set(key, self._vault.encrypt(value))
