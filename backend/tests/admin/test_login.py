from collections.abc import Iterator

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


def test_session_non_ascii_cookie_401_not_500(client: TestClient) -> None:
    # A latin-1 cookie byte reaches verify_token as a non-ASCII str (Starlette decodes
    # headers permissively); the verifier must fail closed with 401, not raise → pre-auth 500.
    raw_cookie = "admin_session=9999999999.ééé".encode("latin-1")
    r = client.get("/admin/session", headers={"cookie": raw_cookie})
    assert r.status_code == 401


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


def test_login_cookie_not_secure_in_dev(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "admin_session=" in set_cookie
    # app_env defaults to "dev" → cookie is NOT Secure so local http dev/tests keep working.
    assert "Secure" not in set_cookie


def test_login_cookie_secure_in_prod_ignores_forwarded_proto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.deps import reset_container
    from app.ratelimit import limiter

    reset_container()
    limiter.reset()
    from app.main import app

    with TestClient(app) as c:
        # A client-supplied x-forwarded-proto: http must NOT downgrade the cookie — the
        # Secure flag is derived from server config (app_env), never a client header.
        r = c.post(
            "/admin/login",
            json={"password": "test-admin-pass"},
            headers={"x-forwarded-proto": "http"},
        )
        assert r.status_code == 200
        assert "Secure" in r.headers.get("set-cookie", "")
    reset_container()


def test_login_over_long_password_not_echoed(client: TestClient) -> None:
    secret = "P" * 300  # over the 256-char max_length
    r = client.post("/admin/login", json={"password": secret})
    assert r.status_code == 422
    # The global RequestValidationError handler drops `input`, so the submitted password
    # never lands in the 422 body (browser devtools/HAR, proxies, error monitors).
    assert secret not in r.text


def test_body_cap_chunked_no_length_411(client: TestClient) -> None:
    # A generator body makes httpx use chunked transfer-encoding with NO Content-Length,
    # which would bypass a header-only size cap pre-auth. Middleware must 411 (length required).
    def gen() -> Iterator[bytes]:
        yield b'{"password": "test-admin-pass"}'

    r = client.post(
        "/admin/login", content=gen(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 411
