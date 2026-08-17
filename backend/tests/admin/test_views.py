import asyncio
import json
from datetime import UTC, datetime, timedelta

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


def test_conversations_list_does_not_corrupt_recency_on_reload(client: TestClient) -> None:
    # Regression: GET /admin/conversations used to bump every listed conversation's last_active_at
    # (via get_or_create) on EVERY page load, silently rewriting recency with no real activity.
    # Two back-to-back loads (no writes in between) must return byte-identical order AND leave every
    # conversation's last_active_at untouched.
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    _seed_outbound_at("+918888888888", "gid://shopify/Order/idem", "{}")
    c = get_container()

    # Warm-up load: the first-ever view legitimately CREATES the outbound-only thread's
    # conversation row (the intended one-time materialization, stamped DEFAULT now()). The bug
    # under test is recency being rewritten on EVERY subsequent load, so compare two steady-state
    # loads after the rows already exist.
    client.get("/admin/conversations")

    first = client.get("/admin/conversations").json()
    before = {
        s.user_id: s.last_active_at
        for s in asyncio.run(c.conversations.recent_conversations(1000))
    }
    second = client.get("/admin/conversations").json()
    after = {
        s.user_id: s.last_active_at
        for s in asyncio.run(c.conversations.recent_conversations(1000))
    }

    assert first == second
    assert before == after


def test_conversations_list_uses_max_last_active_not_first_wins(client: TestClient) -> None:
    # _add() must take the MAX last_active across sources, not first-non-None-wins: a stale
    # conversations.last_active_at must not mask a fresher outbound_messages stamp for the SAME
    # phone. P has a stale conversation row (2020) + a fresh outbound (05:00); Q is outbound-only at
    # 03:00. With MAX capture P outranks Q; first-wins would rank P by 2020 -> below Q.
    login(client)
    c = get_container()
    p = "+919000000001"
    q = "+919000000002"

    async def _stale_conv() -> None:
        conv_id = await c.conversations.get_or_create(p)
        c.conversations._last_active_at[conv_id] = datetime(2020, 1, 1, tzinfo=UTC)  # type: ignore[attr-defined]

    asyncio.run(_stale_conv())
    _seed_outbound_at(p, "gid://shopify/Order/pfresh", "{}")
    _seed_outbound_at(q, "gid://shopify/Order/qmid", "{}")
    meta = c.ingest._outbound_meta  # type: ignore[attr-defined]
    fresh = datetime(2026, 8, 17, 5, tzinfo=UTC)
    middle = datetime(2026, 8, 17, 3, tzinfo=UTC)
    meta["order_created:gid://shopify/Order/pfresh"].created_at = fresh
    meta["order_created:gid://shopify/Order/qmid"].created_at = middle

    rows = client.get("/admin/conversations").json()
    order = [r["phone"] for r in rows if r["phone"] in (p, q)]

    assert order == [p, q]


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


def test_conversation_thread_template_entry_has_clean_text_and_status(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500020"
    _seed_outbound_at(
        normalized, "gid://shopify/Order/cleanbubble",
        json.dumps({
            "template": "order_shipped", "language": "en",
            "body_params": ["Chiranjiv", "tavas4029", "Delhivery Surface",
                             "https://ad2ship.com/track-order/57143610408612"],
        }),
    )

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["status"] == "queued"
    assert "Chiranjiv" in entry["text"]
    assert "tavas4029" in entry["text"]
    assert "Delhivery Surface" in entry["text"]
    assert "https://ad2ship.com/track-order/57143610408612" in entry["text"]
    # The raw internal dump format ("template → param1, param2") must be gone.
    assert "→" not in entry["text"]
    assert "order_shipped" not in entry["text"]


def test_conversation_thread_unmapped_template_falls_back_cleanly(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500021"
    _seed_outbound_at(
        normalized, "gid://shopify/Order/unmapped",
        json.dumps({
            "template": "some_future_template", "language": "en",
            "body_params": ["a", "b"],
        }),
    )

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["status"] == "queued"
    # Falls back to the pre-existing "template → params" format, still correct/non-crashing.
    assert entry["text"] == "some_future_template → a, b"


def test_conversation_thread_template_param_count_mismatch_falls_back(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500022"
    # order_shipped's map expects 4 positional params; this payload supplies only 1.
    _seed_outbound_at(
        normalized, "gid://shopify/Order/mismatch",
        json.dumps({
            "template": "order_shipped", "language": "en",
            "body_params": ["OnlyOneParam"],
        }),
    )

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["text"] == "order_shipped → OnlyOneParam"


def test_conversation_thread_non_template_entries_have_no_status_key(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500023"
    raw_wa_id = normalized.lstrip("+")
    _send_ai_message(normalized, "where is my order", "let me check")
    _record_button_tap("gid://shopify/Order/nostatus", raw_wa_id)

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    for entry in resp.json()["entries"]:
        if entry["type"] != "template_sent":
            assert "status" not in entry


def test_conversation_thread_template_entry_includes_delivery_status(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500070"
    wamid = "wamid.dstatus1"
    # _seed_outbound_at does not set a template_wamid, so seed inline to capture the queued row's
    # id, mark it sent with a known wamid, then route a Meta "delivered" status by that wamid --
    # exactly the path a real status webhook takes to populate OutboundEntry.delivery_status.
    order_gid = "gid://shopify/Order/dstatus1"

    async def _seed_delivered() -> None:
        c = get_container()
        mapping = MappingUpsert(
            order_gid=order_gid, order_name="tavaschat", order_number_int=2,
            phone_e164=normalized, customer_name="Suman", email=None, language="en",
            financial_status_at_create="PENDING", is_cod=True,
        )
        draft = OutboundDraft(
            dedupe_key=f"order_created:{order_gid}", kind="order_confirmation",
            phone_e164=normalized,
            payload_json=json.dumps({"template": "order_shipped", "language": "en",
                                     "body_params": ["A", "B", "C", "D"]}),
        )
        result = await c.ingest.ingest_order_created(
            f"wh-{order_gid}", "orders/create", mapping, draft
        )
        assert result.outbound_id is not None
        await c.ingest.mark_outbound_sent(result.outbound_id, wamid)
        await c.ingest.apply_outbound_delivery_status(wamid, "delivered")

    asyncio.run(_seed_delivered())

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "template_sent")
    assert entry["delivery_status"] == "delivered"


def test_conversation_thread_ai_reply_entry_includes_delivery_status_field(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500071"
    _send_ai_message(normalized, "hi", "hello there")

    thread_id = _thread_id_for(client, normalized)
    resp = client.get(f"/admin/conversations/{thread_id}")

    entry = next(e for e in resp.json()["entries"] if e["type"] == "ai_reply")
    assert "delivery_status" in entry
    assert entry["delivery_status"] is None  # no status ever reported for this test message


def test_conversation_thread_unknown_thread_id_returns_404(client: TestClient) -> None:
    login(client)

    resp = client.get("/admin/conversations/900000000000")

    assert resp.status_code == 404


def test_conversation_thread_reports_paused_until_when_paused(client: TestClient) -> None:
    login(client)
    normalized = "+919876500030"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    future = datetime.now(UTC) + timedelta(hours=24)
    asyncio.run(get_container().conversations.pause_until(thread_id, future))

    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    paused_until = resp.json()["paused_until"]
    assert paused_until is not None
    assert paused_until.startswith(future.date().isoformat())


def test_conversation_thread_paused_until_null_when_not_paused(client: TestClient) -> None:
    login(client)
    normalized = "+919876500031"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    assert resp.json()["paused_until"] is None


def test_resume_conversation_clears_the_pause(client: TestClient) -> None:
    login(client)
    normalized = "+919876500032"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    future = datetime.now(UTC) + timedelta(hours=24)
    asyncio.run(get_container().conversations.pause_until(thread_id, future))

    resp = client.post(f"/admin/conversations/{thread_id}/resume")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    cleared = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert cleared is None or cleared <= datetime.now(UTC)


def test_resume_conversation_unknown_thread_id_returns_404(client: TestClient) -> None:
    login(client)

    resp = client.post("/admin/conversations/900000000001/resume")

    assert resp.status_code == 404


def test_resume_conversation_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/conversations/1/resume")

    assert resp.status_code == 401


def test_resume_conversation_on_unpaused_thread_is_a_harmless_noop(client: TestClient) -> None:
    login(client)
    normalized = "+919876500033"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    resp = client.post(f"/admin/conversations/{thread_id}/resume")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


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


def test_conversations_list_includes_customer_name_and_order_names(
    client: TestClient,
) -> None:
    login(client)
    normalized = "+919876500010"
    older = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/listname-older",
        "name": "tavas700", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "100.00", "currency": "INR",
        "updated_at": "2026-08-01T00:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/1",
            "first_name": "Priya", "last_name": "Shah",
        },
    })
    newer = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/listname-newer",
        "name": "tavas701", "phone": normalized, "financial_status": "pending",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "200.00", "currency": "INR",
        "updated_at": "2026-08-15T00:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/1",
            "first_name": "Priya", "last_name": "Shah",
        },
    })
    assert older is not None and newer is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(older))
    asyncio.run(get_container().ingest.upsert_order_mirror(newer))
    _send_ai_message(normalized, "hi", "hello")  # so this phone is listed at all

    rows = client.get("/admin/conversations").json()

    row = next(r for r in rows if r["phone"] == normalized)
    assert row["customer_name"] == "Priya Shah"
    assert set(row["order_names"]) == {"tavas700", "tavas701"}


def test_conversations_list_no_orders_has_null_name_empty_order_names(
    client: TestClient,
) -> None:
    login(client)
    _send_ai_message("+919876500011", "hi", "hello")

    rows = client.get("/admin/conversations").json()

    row = next(r for r in rows if r["phone"] == "+919876500011")
    assert row["customer_name"] is None
    assert row["order_names"] == []


def test_conversations_list_order_with_no_customer_name_parts(client: TestClient) -> None:
    login(client)
    normalized = "+919876500012"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/noname",
        "name": "tavas702", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "50.00", "currency": "INR",
        "updated_at": "2026-08-17T00:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))
    _send_ai_message(normalized, "hi", "hello")

    rows = client.get("/admin/conversations").json()

    row = next(r for r in rows if r["phone"] == normalized)
    assert row["customer_name"] is None
    assert row["order_names"] == ["tavas702"]


def test_conversation_thread_order_summary_includes_customer_address(client: TestClient) -> None:
    login(client)
    normalized = "+919876500040"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/addr1",
        "name": "tavas800", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "500.00", "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/40",
            "first_name": "Neha", "last_name": "Verma",
        },
        "shipping_address": {
            "address1": "12 MG Road", "address2": "Flat 3B",
            "city": "Pune", "province": "Maharashtra", "zip": "411001",
        },
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    summary = resp.json()["orders"][0]
    assert summary["customer_name"] == "Neha Verma"
    assert summary["address_line1"] == "12 MG Road"
    assert summary["address_line2"] == "Flat 3B"
    assert summary["city"] == "Pune"
    assert summary["state"] == "Maharashtra"
    assert summary["postal_code"] == "411001"


def test_conversation_thread_order_summary_no_customer_has_null_address(client: TestClient) -> None:
    login(client)
    normalized = "+919876500041"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/noaddr",
        "name": "tavas801", "phone": normalized, "financial_status": "paid",
        "fulfillment_status": None, "tags": "", "payment_gateway_names": [],
        "total_price": "500.00", "currency": "INR",
        "updated_at": "2026-08-17T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.get(f"/admin/conversations/{thread_id}")

    summary = resp.json()["orders"][0]
    assert summary["customer_name"] is None
    assert summary["address_line1"] is None
    assert summary["city"] is None
