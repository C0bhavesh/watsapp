from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ADMIN_PW = "test-admin-pass"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.deps import reset_container
    from app.ratelimit import limiter

    reset_container()
    limiter.reset()
    from app.main import app

    with TestClient(app) as c:
        yield c
    reset_container()
