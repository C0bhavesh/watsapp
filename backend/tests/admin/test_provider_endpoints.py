import asyncio

import pytest
from fastapi.testclient import TestClient

from app.deps import get_container
from app.providers.base import ProviderErrorKind
from app.providers.verify import VerifyResult


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_providers_list(client: TestClient) -> None:
    login(client)
    r = client.get("/admin/providers")
    assert r.status_code == 200
    assert any(p["key"] == "gemini" for p in r.json())


def test_unknown_provider_400(client: TestClient) -> None:
    login(client)
    r = client.post("/admin/provider", json={"provider": "nope", "api_key": "k"})
    assert r.status_code == 400


def test_verify_failure_does_not_save(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    login(client)

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=False, error="rejected", kind=ProviderErrorKind.AUTH)

    monkeypatch.setattr("app.admin.router.verify_key", fake_verify)
    r = client.post("/admin/provider", json={"provider": "gemini", "api_key": "bad"})
    assert r.status_code == 400
    assert client.get("/admin/config").json() == {"configured": False, "provider": None}


def test_verify_success_saves_encrypted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=True, error=None, kind=None)

    monkeypatch.setattr("app.admin.router.verify_key", fake_verify)
    r = client.post("/admin/provider", json={"provider": "gemini", "api_key": "good-key"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/admin/config").json() == {"configured": True, "provider": "gemini"}

    async def _read() -> tuple[str | None, str | None]:
        c = get_container()
        raw = await c.config_repo.get("llm:api_key:gemini")
        dec = await c.config.get_secret("llm:api_key:gemini")
        return raw, dec

    raw, dec = asyncio.run(_read())
    assert dec == "good-key" and raw is not None and "good-key" not in raw  # encrypted at rest


def test_missing_api_key_400(client: TestClient) -> None:
    login(client)
    r = client.post("/admin/provider", json={"provider": "gemini", "api_key": ""})
    assert r.status_code == 400
