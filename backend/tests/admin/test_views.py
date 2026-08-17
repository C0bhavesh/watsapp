import asyncio
import json

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


def _record_button_tap(order_gid: str, wa_id: str) -> None:
    asyncio.run(
        get_container().ingest.record_order_action(
            order_gid, "confirm", wa_id, "wamid.1", "ok", None
        )
    )


def _send_ai_message(wa_id: str, user_text: str, ai_text: str) -> None:
    async def _do() -> None:
        conv_id = await get_container().conversations.get_or_create(wa_id)
        await get_container().conversations.append_message(conv_id, "user", user_text)
        await get_container().conversations.append_message(conv_id, "assistant", ai_text)

    asyncio.run(_do())


def test_conversations_list_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/conversations").status_code == 401


def test_conversations_thread_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/conversations/919664290413").status_code == 401


def test_conversations_list_shows_recent_threads(client: TestClient) -> None:
    login(client)
    _send_ai_message("919664290413", "hi", "hello there")

    resp = client.get("/admin/conversations")

    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["user_id"] == "919664290413" for r in rows)


def test_conversation_thread_merges_all_three_sources(client: TestClient) -> None:
    login(client)
    wa_id = "919664290413"
    order_gid = "gid://shopify/Order/chat1"
    _ingest_one(order_gid)  # existing helper -- seeds an order_created outbound at +919999999999
    # Re-seed at the SAME phone this test's wa_id normalizes to, so the outbound row matches.
    # MappingUpsert/OutboundDraft are already imported at the top of this file.
    mapping = MappingUpsert(
        order_gid="gid://shopify/Order/chat2", order_name="tavaschat", order_number_int=2,
        phone_e164="+919664290413", customer_name="Suman", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key="order_created:gid://shopify/Order/chat2", kind="order_confirmation",
        phone_e164="+919664290413",
        payload_json=json.dumps({
            "template": "cod_confirmation", "language": "en",
            "body_params": {"customer_name": "Suman", "order_id": "tavaschat"},
        }),
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            "wh-chat2", "orders/create", mapping, draft
        )
    )
    _send_ai_message(wa_id, "where is my order", "let me check for you")
    _record_button_tap("gid://shopify/Order/chat2", wa_id)

    resp = client.get(f"/admin/conversations/{wa_id}")

    assert resp.status_code == 200
    entries = resp.json()
    types = [e["type"] for e in entries]
    assert "template_sent" in types
    assert "customer_message" in types
    assert "ai_reply" in types
    assert "button_tap" in types
    template_entry = next(e for e in entries if e["type"] == "template_sent")
    assert "cod_confirmation" in template_entry["text"]
    button_entry = next(e for e in entries if e["type"] == "button_tap")
    assert "confirm" in button_entry["text"]
    assert "ok" in button_entry["text"]


def test_conversation_thread_unknown_wa_id_returns_empty_list(client: TestClient) -> None:
    login(client)

    resp = client.get("/admin/conversations/900000000000")

    assert resp.status_code == 200
    assert resp.json() == []
