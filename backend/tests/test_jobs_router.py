import httpx
import pytest

from app.deps import reset_container

SECRET = "topsecret-1234567"  # >= 16 chars (LOW-3 entropy floor)


@pytest.fixture(autouse=True)
def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", SECRET)
    reset_container()
    yield
    reset_container()


async def call(name: str, secret: str | None) -> httpx.Response:
    from app.main import app as fastapi_app

    headers = {} if secret is None else {"X-Cron-Secret": secret}
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/internal/jobs/{name}", headers=headers)


async def call_get(name: str, headers: dict[str, str]) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/internal/jobs/{name}", headers=headers)


async def test_wrong_secret_403() -> None:
    assert (await call("ensure_subscription", "nope")).status_code == 403
    assert (await call("ensure_subscription", None)).status_code == 403


async def test_unset_secret_503(monkeypatch: pytest.MonkeyPatch, master_key: str) -> None:
    monkeypatch.setenv("CRON_SECRET", "")
    reset_container()
    assert (await call("ensure_subscription", "")).status_code == 503


async def test_short_secret_treated_as_misconfigured_503(
    monkeypatch: pytest.MonkeyPatch, master_key: str
) -> None:
    monkeypatch.setenv("CRON_SECRET", "12345678")  # len 8 < 16 -> disabled
    reset_container()
    assert (await call("ensure_subscription", "12345678")).status_code == 503


async def test_non_ascii_secret_header_403_not_500() -> None:
    # Starlette decodes the header latin-1; hmac.compare_digest raises TypeError on
    # non-ASCII str. Must fail closed (403), never 500.
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/internal/jobs/ensure_subscription",
            headers=[(b"x-cron-secret", b"\xe9")],
        )
    assert resp.status_code == 403


async def test_get_with_valid_bearer_runs_the_job() -> None:
    # Vercel Cron fires GET with `Authorization: Bearer <CRON_SECRET>`. outbox_drain needs no
    # external config to run (default send_mode "off" returns cleanly), so a 200 proves the job ran.
    resp = await call_get("outbox_drain", {"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200
    assert resp.json()["job"] == "outbox_drain"


async def test_get_with_wrong_bearer_403() -> None:
    resp = await call_get("outbox_drain", {"Authorization": "Bearer nope"})
    assert resp.status_code == 403


async def test_get_with_missing_authorization_403() -> None:
    resp = await call_get("outbox_drain", {})
    assert resp.status_code == 403


async def test_get_with_old_cron_secret_header_but_no_bearer_403() -> None:
    # A GET carrying the POST-style X-Cron-Secret header (no Authorization) must NOT be accepted:
    # each method reads only its own header, so the wrong header on the wrong method fails closed.
    resp = await call_get("outbox_drain", {"X-Cron-Secret": SECRET})
    assert resp.status_code == 403


async def test_post_with_valid_cron_secret_still_works() -> None:
    # Regression: the manual/admin POST + X-Cron-Secret path is unchanged.
    resp = await call("outbox_drain", SECRET)
    assert resp.status_code == 200
    assert resp.json()["job"] == "outbox_drain"


async def test_unknown_job_404() -> None:
    assert (await call("nope", SECRET)).status_code == 404


async def test_ensure_subscription_without_base_url_reports_error() -> None:
    resp = await call("ensure_subscription", SECRET)
    assert resp.status_code == 200
    assert resp.json() == {
        "job": "ensure_subscription",
        "result": {"error": "public_base_url not configured"},
    }


async def test_shopify_failure_returns_502_without_leaking_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import router as jobs_router
    from app.shopify.errors import ShopifyUnavailable

    async def boom(_c: object) -> dict:
        raise ShopifyUnavailable("internal vendor detail must not leak")

    monkeypatch.setitem(jobs_router.JOBS, "boom", boom)
    resp = await call("boom", SECRET)
    assert resp.status_code == 502
    assert resp.json() == {"job": "boom", "error": "job failed"}
    assert "vendor detail" not in resp.text
