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


def test_put_business_extra_dict_capped_at_50(client: TestClient) -> None:
    login(client)
    over = {"extra": {f"k{i}": "v" for i in range(51)}}
    assert client.put("/admin/knowledge/business", json=over).status_code == 422
    ok = {"extra": {f"k{i}": "v" for i in range(50)}}
    assert client.put("/admin/knowledge/business", json=ok).status_code == 200


def test_put_business_extra_over_long_value_is_422_not_500(client: TestClient) -> None:
    login(client)
    # One over-length VALUE trips BusinessBody._cap_extra_entry_lengths — a custom
    # field_validator raising ValueError, which leaves a raw ValueError in ctx["error"].
    # errors() must drop that non-serializable ctx so the response is a clean 422, not a
    # 500 (json.dumps TypeError while rendering the HTTPException detail).
    r = client.put("/admin/knowledge/business", json={"extra": {"k": "v" * 3000}})
    assert r.status_code == 422


def test_put_business_extra_over_long_key_is_422_not_500(client: TestClient) -> None:
    login(client)
    r = client.put("/admin/knowledge/business", json={"extra": {"x" * 300: "v"}})
    assert r.status_code == 422


def test_put_patterns_rejects_over_long_example(client: TestClient) -> None:
    login(client)
    # A single 300-char example must be rejected (per-element cap), else a huge example
    # inflates the Phase-4 assembled prompt (DoS surface).
    bad = {"items": [{"pattern": "p", "examples": ["x" * 300], "reply": "r"}]}
    assert client.put("/admin/knowledge/patterns", json=bad).status_code == 422
