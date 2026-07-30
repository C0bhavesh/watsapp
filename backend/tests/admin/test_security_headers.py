from fastapi.testclient import TestClient
from httpx import Response

CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def _assert_security_headers(r: Response) -> None:
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


def test_static_panel_carries_security_headers(client: TestClient) -> None:
    r = client.get("/admin/ui/")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_session_carries_security_headers(client: TestClient) -> None:
    _login(client)
    r = client.get("/admin/session")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_pii_view_is_no_store(client: TestClient) -> None:
    # mappings/outbox return customer phone PII — must never be cached by browser/proxy.
    _login(client)
    r = client.get("/admin/mappings")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
