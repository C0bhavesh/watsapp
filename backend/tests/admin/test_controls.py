from fastapi.testclient import TestClient


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_defaults(client: TestClient) -> None:
    login(client)
    r = client.get("/admin/controls")
    assert r.status_code == 200
    body = r.json()
    assert body["send_mode"] == "off"
    assert body["push_policy"] == "cod_only"
    assert body["reveal_fields"] == ["order_number", "email", "status"]
    assert body["default_language"] == "en"
    assert body["push_staleness_hours"] == 6


def test_put_roundtrip(client: TestClient) -> None:
    login(client)
    doc = {
        "send_mode": "allowlist",
        "allowlist_phones": ["+919664290413"],
        "push_policy": "all",
        "reveal_fields": ["order_number", "status"],
        "tags": {
            "pending": ["COD pending"],
            "confirmed": ["confirmed", "Confirmed by wati"],
            "cancelled": ["cancelled", "Cancel by wati"],
        },
        "default_language": "hi",
        "push_staleness_hours": 12,
        "public_base_url": "https://bot.example.com",
        "owner_alert_number": "",
    }
    assert client.put("/admin/controls", json=doc).status_code == 200
    assert client.get("/admin/controls").json() == doc


def test_put_rejects_bad_values(client: TestClient) -> None:
    login(client)
    base = client.get("/admin/controls").json()
    assert client.put("/admin/controls", json={**base, "send_mode": "on"}).status_code == 422
    assert (
        client.put("/admin/controls", json={**base, "allowlist_phones": ["12345"]}).status_code
        == 422
    )
    assert (
        client.put("/admin/controls", json={**base, "reveal_fields": ["items"]}).status_code
        == 422
    )
    assert (
        client.put(
            "/admin/controls", json={**base, "public_base_url": "http://insecure.example"}
        ).status_code
        == 422
    )


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/controls").status_code == 401
