import asyncio
import json

from fastapi.testclient import TestClient

from app.deps import get_container


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_get_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/knowledge/faq").status_code == 401


def test_get_returns_seed_when_no_override(client: TestClient) -> None:
    login(client)
    r = client.get("/admin/knowledge/faq")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "faq" and isinstance(json.loads(body["content"]), list)


def test_unknown_kind_400(client: TestClient) -> None:
    login(client)
    assert client.get("/admin/knowledge/menu").status_code == 400
    assert client.put("/admin/knowledge/menu", json={"content": "x"}).status_code == 400


def test_put_faq_saves_and_bumps_version(client: TestClient) -> None:
    login(client)
    items = {"items": [{"q": "Q1", "a": "A1"}]}
    assert client.put("/admin/knowledge/faq", json=items).status_code == 200
    stored = client.get("/admin/knowledge/faq").json()["content"]
    assert json.loads(stored) == [{"q": "Q1", "a": "A1"}]

    async def _version() -> str | None:
        return await get_container().config_repo.get("knowledge_version")

    assert asyncio.run(_version()) == "1"


def test_put_faq_rejects_malformed(client: TestClient) -> None:
    login(client)
    assert client.put("/admin/knowledge/faq", json={"items": [{"q": "only-q"}]}).status_code == 422
    assert client.put("/admin/knowledge/faq", json={"items": []}).status_code == 422


def test_put_brand_voice_roundtrip(client: TestClient) -> None:
    login(client)
    assert (
        client.put("/admin/knowledge/brand_voice", json={"content": "New voice"}).status_code
        == 200
    )
    assert client.get("/admin/knowledge/brand_voice").json()["content"] == "New voice"


def test_put_patterns_and_business_validate(client: TestClient) -> None:
    login(client)
    ok_p = {"items": [{"pattern": "greeting", "examples": ["hi"], "reply": "Hello"}]}
    assert client.put("/admin/knowledge/patterns", json=ok_p).status_code == 200
    bad_p = {"items": [{"pattern": "x", "examples": "not-a-list", "reply": "r"}]}
    assert client.put("/admin/knowledge/patterns", json=bad_p).status_code == 422
    ok_b = {"store_name": "Thetavas", "website": "https://thetavas.myshopify.com"}
    assert client.put("/admin/knowledge/business", json=ok_b).status_code == 200
