import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_login_ok_sets_cookie_and_session_works(client: TestClient) -> None:
    login(client)
    assert client.cookies.get("admin_session")
    assert client.get("/admin/session").status_code == 200


def test_login_wrong_password_401(client: TestClient) -> None:
    assert client.post("/admin/login", json={"password": "nope"}).status_code == 401


def test_session_without_cookie_401(client: TestClient) -> None:
    assert client.get("/admin/session").status_code == 401


def test_login_unset_password_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.deps import reset_container
    from app.ratelimit import limiter

    reset_container()
    limiter.reset()
    from app.main import app

    with TestClient(app) as c:
        assert c.post("/admin/login", json={"password": "x"}).status_code == 503
    reset_container()


def test_login_rate_limited_after_5(client: TestClient) -> None:
    for _ in range(5):
        assert client.post("/admin/login", json={"password": "bad"}).status_code == 401
    assert client.post("/admin/login", json={"password": "bad"}).status_code == 429


def test_body_cap_413(client: TestClient) -> None:
    big = b"x" * 1_048_577  # one byte over the 1 MiB cap; httpx sets Content-Length for us
    r = client.post("/admin/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
