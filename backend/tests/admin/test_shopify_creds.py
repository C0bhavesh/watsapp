import asyncio

from fastapi.testclient import TestClient

from app.deps import get_container


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/shopify").status_code == 401


def test_first_time_requires_both(client: TestClient) -> None:
    login(client)
    assert client.get("/admin/shopify").json() == {"configured": False}
    assert client.post("/admin/shopify", json={"client_id": "abc"}).status_code == 422


def test_save_then_partial_update(client: TestClient) -> None:
    login(client)
    r = client.post("/admin/shopify", json={"client_id": "id1", "client_secret": "sec1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/admin/shopify").json() == {"configured": True}
    # partial update: change only the secret; id must survive
    assert client.post("/admin/shopify", json={"client_secret": "sec2"}).status_code == 200

    async def _read() -> tuple[str | None, str | None]:
        cfg = get_container().config
        return await cfg.get_secret("shopify:client_id"), await cfg.get_secret(
            "shopify:client_secret"
        )

    cid, csec = asyncio.run(_read())
    assert (cid, csec) == ("id1", "sec2")


def test_secrets_never_echoed(client: TestClient) -> None:
    login(client)
    client.post("/admin/shopify", json={"client_id": "id1", "client_secret": "sec1"})
    body = client.get("/admin/shopify").text
    assert "id1" not in body and "sec1" not in body
