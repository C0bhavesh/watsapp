import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def master_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def settings(master_key: str):
    from app.config.settings import Settings

    return Settings(app_master_key=master_key)
