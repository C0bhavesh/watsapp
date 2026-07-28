import httpx
import pytest

from app.deps import reset_container


@pytest.fixture(autouse=True)
def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", "topsecret")
    reset_container()
    yield
    reset_container()


async def call(name: str, secret: str | None) -> httpx.Response:
    from app.main import app as fastapi_app

    headers = {} if secret is None else {"X-Cron-Secret": secret}
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/internal/jobs/{name}", headers=headers)


async def test_wrong_secret_403() -> None:
    assert (await call("ensure_subscription", "nope")).status_code == 403
    assert (await call("ensure_subscription", None)).status_code == 403


async def test_unset_secret_503(monkeypatch: pytest.MonkeyPatch, master_key: str) -> None:
    monkeypatch.setenv("CRON_SECRET", "")
    reset_container()
    assert (await call("ensure_subscription", "")).status_code == 503


async def test_unknown_job_404() -> None:
    assert (await call("nope", "topsecret")).status_code == 404


async def test_ensure_subscription_without_base_url_reports_error() -> None:
    resp = await call("ensure_subscription", "topsecret")
    assert resp.status_code == 200
    assert resp.json() == {
        "job": "ensure_subscription",
        "result": {"error": "public_base_url not configured"},
    }
