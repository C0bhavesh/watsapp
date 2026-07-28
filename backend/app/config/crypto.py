from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    """Raised when the vault cannot be constructed or a value cannot be decrypted."""


class SecretVault:
    def __init__(self, master_key: str) -> None:
        try:
            self._fernet = Fernet(master_key.encode())
        except (ValueError, TypeError) as exc:
            raise VaultError("APP_MASTER_KEY is not a valid Fernet key") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise VaultError("decryption failed") from exc
