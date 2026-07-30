import asyncio

from fastapi.testclient import TestClient

from app.deps import get_container


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


FULL = {
    "phone_number_id": "123456789012345",
    "waba_id": "234567890123456",
    "api_version": "v23.0",
    "access_token": "tokenA",
    "app_secret": "secretA",
    "verify_token": "verifyA",
}


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/whatsapp").status_code == 401


def test_first_time_requires_all_six(client: TestClient) -> None:
    login(client)
    r = client.post("/admin/whatsapp", json={"phone_number_id": "123456789012345"})
    assert r.status_code == 422
    assert "waba_id" in r.text  # names the missing fields


def test_rejects_path_like_phone_number_id(client: TestClient) -> None:
    login(client)
    # A non-digit id (path-like junk) must not be stored and interpolated into the Graph URL.
    bad = {**FULL, "phone_number_id": "../../evil"}
    assert client.post("/admin/whatsapp", json=bad).status_code == 422


def test_save_status_and_partial_update(client: TestClient) -> None:
    login(client)
    assert client.post("/admin/whatsapp", json=FULL).status_code == 200
    status = client.get("/admin/whatsapp").json()
    assert status == {
        "configured": True,
        "phone_number_id": "123456789012345",
        "waba_id": "234567890123456",
        "api_version": "v23.0",
    }
    # rotate ONE secret; everything else survives
    assert client.post("/admin/whatsapp", json={"access_token": "tokenB"}).status_code == 200

    async def _read() -> tuple[str | None, str | None]:
        cfg = get_container().config
        return (
            await cfg.get_secret("whatsapp:access_token"),
            await cfg.get_secret("whatsapp:app_secret"),
        )

    tok, sec = asyncio.run(_read())
    assert (tok, sec) == ("tokenB", "secretA")


def test_secrets_never_echoed(client: TestClient) -> None:
    login(client)
    client.post("/admin/whatsapp", json=FULL)
    body = client.get("/admin/whatsapp").text
    assert "tokenA" not in body and "secretA" not in body and "verifyA" not in body
