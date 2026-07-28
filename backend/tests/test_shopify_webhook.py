import base64
import hashlib
import hmac as hmac_lib
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.deps import get_container, reset_container

SECRET = "csec-webhook"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    await c.config.set_secret("shopify:client_secret", SECRET)
    yield
    reset_container()


def payload(gid: str = "gid://shopify/Order/1") -> dict:
    return {
        "admin_graphql_api_id": gid,
        "name": "tavas3733",
        "order_number": 3733,
        "email": "c@example.com",
        "customer": {"first_name": "Suman", "last_name": "B"},
        "shipping_address": {"phone": "+919664290413"},
        "tags": "COD",
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "949.00",
        "created_at": datetime.now(UTC).isoformat(),
        "customer_locale": "hi-IN",
    }


def sign(body: bytes) -> str:
    return base64.b64encode(hmac_lib.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


async def post(body: bytes, headers: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/shopify", content=body, headers=headers)


def headers(body: bytes, topic: str = "orders/create", webhook_id: str = "wh-1") -> dict:
    return {
        "X-Shopify-Hmac-Sha256": sign(body),
        "X-Shopify-Topic": topic,
        "X-Shopify-Webhook-Id": webhook_id,
        "Content-Type": "application/json",
    }


async def test_bad_hmac_403_and_nothing_stored() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, {**headers(body), "X-Shopify-Hmac-Sha256": "AAAA"})
    assert resp.status_code == 403
    assert not get_container().ingest.webhooks  # type: ignore[attr-defined]


async def test_orders_create_ingests_and_queues() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    store = get_container().ingest
    draft = store.outbound["order_created:gid://shopify/Order/1"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "order_confirmation_cod"
    assert params["language"] == "hi"
    assert draft.phone_e164 == "+919664290413"


async def test_duplicate_webhook_id_reports_duplicate() -> None:
    body = json.dumps(payload()).encode()
    await post(body, headers(body))
    resp = await post(body, headers(body))
    assert resp.json() == {"ok": True, "duplicate": True, "queued": False}


async def test_prepaid_order_maps_but_does_not_queue_under_cod_only() -> None:
    p = payload("gid://shopify/Order/2")
    p["payment_gateway_names"] = ["Razorpay"]
    p["tags"] = "online"
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, webhook_id="wh-2"))
    assert resp.json() == {"ok": True, "duplicate": False, "queued": False}
    store = get_container().ingest
    assert "gid://shopify/Order/2" in store.mappings  # type: ignore[attr-defined]


async def test_other_topic_ignored() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body, topic="orders/updated"))
    assert resp.json() == {"ok": True, "ignored": True}


async def test_garbage_body_with_valid_hmac_ignored() -> None:
    body = b"not-json"
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}
