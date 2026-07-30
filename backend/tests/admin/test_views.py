import asyncio

from fastapi.testclient import TestClient

from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def _ingest_one(order_gid: str) -> None:
    mapping = MappingUpsert(
        order_gid=order_gid,
        order_name="tavas1001",
        order_number_int=1001,
        phone_e164="+919999999999",
        customer_name="Test",
        email="t@example.com",
        language="en",
        financial_status_at_create="pending",
        is_cod=True,
    )
    outbound = OutboundDraft(
        dedupe_key=f"order_created:{order_gid}",
        kind="order_confirmation",
        phone_e164="+919999999999",
        payload_json="{}",
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            "wh-1-" + order_gid, "orders/create", mapping, outbound
        )
    )


def test_views_require_auth(client: TestClient) -> None:
    assert client.get("/admin/mappings").status_code == 401
    assert client.get("/admin/outbox").status_code == 401


def test_mappings_and_outbox_views(client: TestClient) -> None:
    login(client)
    _ingest_one("gid://shopify/Order/1")
    _ingest_one("gid://shopify/Order/2")
    m = client.get("/admin/mappings")
    assert m.status_code == 200
    rows = m.json()
    assert len(rows) == 2
    assert {r["order_gid"] for r in rows} == {"gid://shopify/Order/1", "gid://shopify/Order/2"}
    assert rows[0]["status"] == "pending" and rows[0]["is_cod"] is True
    o = client.get("/admin/outbox")
    assert o.status_code == 200
    orows = o.json()
    assert len(orows) == 2 and orows[0]["state"] == "queued" and orows[0]["attempts"] == 0


def test_limit_validated(client: TestClient) -> None:
    login(client)
    assert client.get("/admin/mappings?limit=0").status_code == 422
    assert client.get("/admin/mappings?limit=501").status_code == 422
