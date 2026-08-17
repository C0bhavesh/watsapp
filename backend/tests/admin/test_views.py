import asyncio
import json

from fastapi.testclient import TestClient

from app.channels.shopify_orders import order_from_webhook_payload
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


def _record_button_tap(order_gid: str, actor_wa_id: str) -> None:
    # Production writes actor_wa_id RAW (event.wa_id, no leading +) -- callers pass the raw form.
    asyncio.run(
        get_container().ingest.record_order_action(
            order_gid, "confirm", actor_wa_id, "wamid.1", "ok", None
        )
    )


def _send_ai_message(phone_e164: str, user_text: str, ai_text: str) -> None:
    # Production keys the conversation on the NORMALIZED phone (core/conversation.py stores via
    # get_or_create(normalize_phone(event.wa_id))) -- callers pass the +-prefixed E.164 form.
    async def _do() -> None:
        conv_id = await get_container().conversations.get_or_create(phone_e164)
        await get_container().conversations.append_message(conv_id, "user", user_text)
        await get_container().conversations.append_message(conv_id, "assistant", ai_text)

    asyncio.run(_do())


def _seed_outbound_at(phone_e164: str, order_gid: str, payload_json: str) -> None:
    mapping = MappingUpsert(
        order_gid=order_gid, order_name="tavaschat", order_number_int=2,
        phone_e164=phone_e164, customer_name="Suman", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{order_gid}", kind="order_confirmation",
        phone_e164=phone_e164, payload_json=payload_json,
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            f"wh-{order_gid}", "orders/create", mapping, draft
        )
    )


def _thread_id_for(client: TestClient, phone_e164: str) -> int:
    rows = client.get("/admin/conversations").json()
    match = next(r for r in rows if r["phone"] == phone_e164)
    return int(match["thread_id"])


def test_conversations_list_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/conversations").status_code == 401


def test_conversations_thread_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/conversations/1").status_code == 401


def test_conversations_list_shows_recent_threads(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")

    resp = client.get("/admin/conversations")

    assert resp.status_code == 200
    rows = resp.json()
    row = next(r for r in rows if r["phone"] == "+919664290413")
    assert isinstance(row["thread_id"], int)
    assert row["preview"]


def test_conversations_list_includes_outbound_only_customer(client: TestClient) -> None:
    # A customer who ONLY ever received an order confirmation (no conversation row, no button tap)
    # must still surface as a thread -- the list unions all three sources, not just conversations.
    login(client)
    _seed_outbound_at("+918888888888", "gid://shopify/Order/outonly", "{}")

    rows = client.get("/admin/conversations").json()

    outbound_thread = next(r for r in rows if r["phone"] == "+918888888888")
    assert isinstance(outbound_thread["thread_id"], int)


def test_conversation_thread_merges_all_three_sources(client: TestClient) -> None:
    login(client)
    normalized = "+919664290413"
    raw_wa_id = "919664290413"  # how core/order_actions.py writes actor_wa_id (no +)
    _ingest_one("gid://shopify/Order/chat1")  # unrelated order at +919999999999
    _seed_outbound_at(
        normalized, "gid://shopify/Order/chat2",
        json.dumps({
            "template": "cod_confirmation", "language": "en",
            "body_params": {"customer_name": "Suman", "order_id": "tavaschat"},
        }),
    )
    _send_ai_message(normalized, "where is my order", "let me check for you")
    _record_button_tap("gid://shopify/Order/chat2", raw_wa_id)

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    types = [e["type"] for e in entries]
    # All three sources surface in ONE thread even though order_actions is keyed RAW while the
    # AI chat + outbound are keyed NORMALIZED -- the dual-format lookup is what proves finding #1.
    assert "template_sent" in types
    assert "customer_message" in types
    assert "ai_reply" in types
    assert "button_tap" in types
    template_entry = next(e for e in entries if e["type"] == "template_sent")
    assert "cod_confirmation" in template_entry["text"]
    button_entry = next(e for e in entries if e["type"] == "button_tap")
    assert "confirm" in button_entry["text"]
    assert "ok" in button_entry["text"]


def test_conversation_thread_unknown_thread_id_returns_404(client: TestClient) -> None:
    login(client)

    resp = client.get("/admin/conversations/900000000000")

    assert resp.status_code == 404


def test_conversation_thread_non_dict_payload_degrades_not_500(client: TestClient) -> None:
    # A valid-but-non-dict payload_json ("null"/"42"/"[]") parses successfully but has no .get --
    # the thread must still return 200 with a degraded text for that one entry, not a 500.
    login(client)
    normalized = "+917777777777"
    _seed_outbound_at(normalized, "gid://shopify/Order/nondict", "null")

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    template_entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert "unreadable template payload" in template_entry["text"]


def test_conversation_thread_includes_order_summary(client: TestClient) -> None:
    login(client)
    normalized = "+919876543210"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/orders1",
        "name": "tavas500",
        "phone": normalized,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "tags": "vip, repeat",
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "1299.00",
        "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    # A mirrored-order-only customer is NOT surfaced by /admin/conversations (that list unions
    # conversations + outbound + order_actions, never mirrored orders), so materialize the thread
    # id directly rather than resolving it via _thread_id_for.
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    summary = orders[0]
    assert summary["order_name"] == "tavas500"
    assert summary["financial_status"] == "paid"
    assert summary["fulfillment_status"] == "fulfilled"
    assert summary["is_cod"] is True
    assert summary["total_amount"] == "1299.00"
    assert summary["total_currency"] == "INR"
    assert "vip" in summary["tags"]
    assert summary["tracking_company"] is None


def test_conversation_thread_order_summary_includes_line_items(client: TestClient) -> None:
    login(client)
    normalized = "+919876500001"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/lineitems1",
        "name": "tavas600",
        "phone": normalized,
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "899.00",
        "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
        "line_items": [
            {"title": "Classic Kurta", "quantity": 2, "variant_title": "Blue / L",
             "price": "349.50", "sku": "KUR-BLU-L"},
            {"title": "Cotton Scarf", "quantity": 1, "variant_title": None,
             "price": None, "sku": None},
        ],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    line_items = resp.json()["orders"][0]["line_items"]
    assert len(line_items) == 2
    assert line_items[0] == {
        "title": "Classic Kurta", "quantity": 2, "variant_title": "Blue / L",
        "price_amount": "349.50", "price_currency": "INR",
    }
    assert line_items[1] == {
        "title": "Cotton Scarf", "quantity": 1, "variant_title": None,
        "price_amount": None, "price_currency": None,
    }


def test_conversation_thread_order_summary_empty_line_items(client: TestClient) -> None:
    login(client)
    normalized = "+919876500002"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/nolineitems",
        "name": "tavas601", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "100.00", "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.json()["orders"][0]["line_items"] == []


def test_conversation_thread_no_orders_returns_empty_list(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919111222333", "hi", "hello there")

    thread_id = _thread_id_for(client, "+919111222333")
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    assert resp.json()["orders"] == []


def test_conversation_thread_multiple_orders_sorted_most_recent_first(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919555666777"

    older = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/older",
        "name": "tavas-older", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "500.00", "currency": "INR", "updated_at": "2026-08-01T00:00:00+05:30",
    })
    newer = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/newer",
        "name": "tavas-newer", "phone": normalized, "financial_status": "pending",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "700.00", "currency": "INR", "updated_at": "2026-08-15T00:00:00+05:30",
    })
    assert older is not None and newer is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(older))
    asyncio.run(get_container().ingest.upsert_order_mirror(newer))

    # See test_conversation_thread_includes_order_summary: mirrored-order-only customers are not in
    # the conversations list, so materialize the thread id directly.
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    order_names = [o["order_name"] for o in resp.json()["orders"]]
    assert order_names == ["tavas-newer", "tavas-older"]
