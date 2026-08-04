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


def test_providers_list_includes_auth_kind(client: TestClient) -> None:
    login(client)
    r = client.get("/admin/providers")
    assert r.status_code == 200
    by_key = {p["key"]: p for p in r.json()}
    assert by_key["gemini"]["auth_kind"] == "api_key"
    assert by_key["vertex"]["auth_kind"] == "env"


def test_vertex_env_provider_saves_without_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=True, error=None, kind=None)

    monkeypatch.setattr("app.admin.router.verify_key", fake_verify)
    # No api_key sent for the env-auth provider.
    r = client.post("/admin/provider", json={"provider": "vertex"})
    assert r.status_code == 200 and r.json() == {"ok": True}

    # Active provider recorded, and GET /config reports configured without a stored key.
    assert client.get("/admin/config").json() == {"configured": True, "provider": "vertex"}

    async def _read() -> tuple[str | None, str | None]:
        c = get_container()
        active = await c.config_repo.get("llm:active_provider")
        secret = await c.config_repo.get("llm:api_key:vertex")
        return active, secret

    active, secret = asyncio.run(_read())
    assert active == "vertex"
    assert secret is None  # env-auth provider never writes a key secret


def test_vertex_env_verify_failure_uses_safe_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    leak = '{"private_key":"-----BEGIN PRIVATE KEY-----LEAKED-----END PRIVATE KEY-----"}'

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=False, error=leak, kind=ProviderErrorKind.UNKNOWN)

    monkeypatch.setattr("app.admin.router.verify_key", fake_verify)
    r = client.post("/admin/provider", json={"provider": "vertex"})
    assert r.status_code == 400
    assert leak not in r.text  # the service-account JSON must never surface
    assert "-----BEGIN PRIVATE KEY-----" not in r.text
    assert r.json()["detail"] == (
        "Could not verify the provider credentials. Check the service-account JSON, "
        "project, and location."
    )
    # Nothing activated on failure.
    assert client.get("/admin/config").json() == {"configured": False, "provider": None}


def test_vertex_env_auth_failure_is_service_account_oriented(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    leak = '{"private_key":"-----BEGIN PRIVATE KEY-----LEAKED-----END PRIVATE KEY-----"}'

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=False, error=leak, kind=ProviderErrorKind.AUTH)

    monkeypatch.setattr("app.admin.router.verify_key", fake_verify)
    r = client.post("/admin/provider", json={"provider": "vertex"})
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    # Env/Vertex auth failure must reference the service-account credentials, not "the API key".
    assert "service-account" in detail
    assert "project" in detail
    assert "location" in detail
    assert "api key" not in detail
    # No raw provider error / service-account JSON ever surfaces.
    assert leak not in r.text
    assert "-----BEGIN PRIVATE KEY-----" not in r.text
    # Nothing activated on failure.
    assert client.get("/admin/config").json() == {"configured": False, "provider": None}


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


def test_over_long_api_key_not_echoed(client: TestClient) -> None:
    login(client)
    secret = "A" * 600  # over the 512-char max_length
    r = client.post("/admin/provider", json={"provider": "gemini", "api_key": secret})
    assert r.status_code == 422
    assert secret not in r.text  # validation handler must not echo the submitted key


def test_unknown_error_kind_returns_generic_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    leak = "litellm-internal-detail-should-not-surface"

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=False, error=leak, kind=ProviderErrorKind.UNKNOWN)

    monkeypatch.setattr("app.admin.router.verify_key", fake_verify)
    r = client.post("/admin/provider", json={"provider": "gemini", "api_key": "k"})
    assert r.status_code == 400
    assert leak not in r.text  # never forward raw provider/internal exception text
    assert r.json()["detail"] == "Could not verify the key with the provider."
