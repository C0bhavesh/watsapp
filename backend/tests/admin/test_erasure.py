"""DPDP right-to-erasure admin endpoint: POST /admin/erasure (delete by phone)."""

import asyncio

from fastapi.testclient import TestClient

from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft

PHONE = "+919111111111"
OTHER = "+919222222222"


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def _ingest(order_gid: str, phone: str) -> None:
    mapping = MappingUpsert(
        order_gid=order_gid,
        order_name="tavas1001",
        order_number_int=1001,
        phone_e164=phone,
        customer_name="Test",
        email="t@example.com",
        language="en",
        financial_status_at_create="pending",
        is_cod=True,
    )
    outbound = OutboundDraft(
        dedupe_key=f"order_created:{order_gid}",
        kind="order_confirmation",
        phone_e164=phone,
        payload_json="{}",
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            "wh-" + order_gid, "orders/create", mapping, outbound
        )
    )


def test_erasure_requires_auth(client: TestClient) -> None:
    assert client.post("/admin/erasure", json={"phone": PHONE}).status_code == 401


def test_erasure_deletes_and_returns_counts(client: TestClient) -> None:
    login(client)
    _ingest("gid://shopify/Order/1", PHONE)
    _ingest("gid://shopify/Order/2", PHONE)
    _ingest("gid://shopify/Order/3", OTHER)

    r = client.post("/admin/erasure", json={"phone": PHONE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"]["order_mappings"] == 2
    assert body["deleted"]["outbound_messages"] == 2

    # The other number's mapping survives.
    rows = client.get("/admin/mappings").json()
    assert {row["order_gid"] for row in rows} == {"gid://shopify/Order/3"}


def test_erasure_no_match_returns_zeros(client: TestClient) -> None:
    login(client)
    _ingest("gid://shopify/Order/1", PHONE)
    r = client.post("/admin/erasure", json={"phone": "+910000000000"})
    assert r.status_code == 200
    assert r.json()["deleted"]["order_mappings"] == 0


def test_erasure_rejects_bad_phone_without_echoing_it(client: TestClient) -> None:
    login(client)
    r = client.post("/admin/erasure", json={"phone": "+9199"})  # too short for E.164
    assert r.status_code == 422
    # The submitted phone (PII) must not be echoed back in the validation error body.
    assert "+9199" not in r.text
