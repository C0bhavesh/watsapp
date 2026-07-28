import pytest

from app.config.crypto import SecretVault, VaultError


def test_roundtrip(master_key: str) -> None:
    vault = SecretVault(master_key)
    token = vault.encrypt("shh-secret")
    assert token != "shh-secret"
    assert vault.decrypt(token) == "shh-secret"


def test_invalid_master_key_fails_fast() -> None:
    with pytest.raises(VaultError):
        SecretVault("not-a-fernet-key")


def test_decrypt_garbage_raises(master_key: str) -> None:
    vault = SecretVault(master_key)
    with pytest.raises(VaultError):
        vault.decrypt("gAAAAAgarbage")
