import httpx
import pytest

from app.deps import get_container, reset_container


@pytest.fixture(autouse=True)
def _fresh_container(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    yield
    reset_container()


async def test_health_returns_ok() -> None:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "thetavas-order-bot"}


def test_container_is_singleton() -> None:
    assert get_container() is get_container()


def test_container_wires_shopify_layer() -> None:
    c = get_container()
    assert c.shopify is not None and c.tokens is not None
