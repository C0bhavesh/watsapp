import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.admin.controls import AdminControls
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
    rows = client.get("/admin/conversations").json()["threads"]
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
    rows = resp.json()["threads"]
    row = next(r for r in rows if r["phone"] == "+919664290413")
    assert isinstance(row["thread_id"], int)
    assert row["preview"]


def test_conversations_list_shows_unread_count_for_new_customer_message(
    client: TestClient,
) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")
    # Opening the thread marks it read as of now; a message that arrives AFTER that must count.
    client.get(f"/admin/conversations/{thread_id}")
    # This machine's clock resolution is coarse enough that back-to-back datetime.now(UTC) calls
    # can tie (confirmed empirically -- consecutive calls returned the identical value in >99.9%
    # of a tight-loop sample), which would make the strict created_at > last_read_at comparison
    # in count_unread_messages silently miss this message. A small sleep guarantees the new
    # message's timestamp is unambiguously later than the mark_read stamp above.
    time.sleep(0.05)

    async def _new_customer_message() -> None:
        await get_container().conversations.append_message(thread_id, "user", "still there?")

    asyncio.run(_new_customer_message())

    rows = client.get("/admin/conversations").json()["threads"]
    row = next(r for r in rows if r["thread_id"] == thread_id)
    assert row["unread_count"] == 1


def test_opening_thread_clears_unread_count(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")
    # See the sibling test above for why this sleep is needed on this machine's coarse clock.
    time.sleep(0.05)

    async def _new_customer_message() -> None:
        await get_container().conversations.append_message(thread_id, "user", "still there?")

    asyncio.run(_new_customer_message())
    before = client.get("/admin/conversations").json()["threads"]
    row_before = next(r for r in before if r["thread_id"] == thread_id)
    assert row_before["unread_count"] >= 1

    # Same clock-tie risk as above, mirrored: the "after" assertion needs the mark_read stamp
    # below to land strictly after the message just appended, or the race described in
    # error_learnings.md (2026-08-19) could tie the two and make this assertion coincidentally
    # pass for the wrong reason (or flake on a machine with even coarser clock resolution).
    time.sleep(0.05)
    client.get(f"/admin/conversations/{thread_id}")

    after = client.get("/admin/conversations").json()["threads"]
    row_after = next(r for r in after if r["thread_id"] == thread_id)
    assert row_after["unread_count"] == 0


def test_conversations_list_reports_ai_paused_while_handed_off(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")
    future = datetime.now(UTC) + timedelta(hours=1)
    asyncio.run(get_container().conversations.pause_until(thread_id, future))

    rows = client.get("/admin/conversations").json()["threads"]
    row = next(r for r in rows if r["thread_id"] == thread_id)
    assert row["ai_paused"] is True


def test_conversations_list_reports_ai_not_paused_by_default(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")

    rows = client.get("/admin/conversations").json()["threads"]
    row = next(r for r in rows if r["phone"] == "+919664290413")
    assert row["ai_paused"] is False


def test_conversations_list_reports_exchange_processing_flags(client: TestClient) -> None:
    login(client)
    phone_unprocessed = "+919000000101"
    phone_processed_status = "+919000000102"
    phone_processed_tracking = "+919000000103"
    phone_no_exchange = "+919000000104"

    for phone in (
        phone_unprocessed, phone_processed_status, phone_processed_tracking, phone_no_exchange,
    ):
        _send_ai_message(phone, "hi", "hello there")

    c = get_container()
    asyncio.run(c.exchanges.create("gid://o/unproc", "tavasU", phone_unprocessed, "M"))

    processed_status = asyncio.run(
        c.exchanges.create("gid://o/procstat", "tavasP1", phone_processed_status, "M")
    )
    asyncio.run(c.exchanges.set_status(processed_status.id, "return_picked_up"))

    processed_tracking = asyncio.run(
        c.exchanges.create("gid://o/proctrack", "tavasP2", phone_processed_tracking, "L")
    )
    asyncio.run(
        c.exchanges.set_return_tracking_url(processed_tracking.id, "https://track.example/1")
    )

    rows = client.get("/admin/conversations").json()["threads"]
    by_phone = {r["phone"]: r for r in rows}

    assert by_phone[phone_unprocessed]["exchange_unprocessed"] is True
    assert by_phone[phone_unprocessed]["exchange_processed"] is False

    assert by_phone[phone_processed_status]["exchange_unprocessed"] is False
    assert by_phone[phone_processed_status]["exchange_processed"] is True

    assert by_phone[phone_processed_tracking]["exchange_unprocessed"] is False
    assert by_phone[phone_processed_tracking]["exchange_processed"] is True

    assert by_phone[phone_no_exchange]["exchange_unprocessed"] is False
    assert by_phone[phone_no_exchange]["exchange_processed"] is False


def test_conversations_list_includes_outbound_only_customer(client: TestClient) -> None:
    # A customer who ONLY ever received an order confirmation (no conversation row, no button tap)
    # must still surface as a thread -- the list unions all three sources, not just conversations.
    login(client)
    _seed_outbound_at("+918888888888", "gid://shopify/Order/outonly", "{}")

    rows = client.get("/admin/conversations").json()["threads"]

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

    first = client.get("/admin/conversations").json()["threads"]
    # NOTE (2026-08-19): since recent_conversations() excludes message-less rows (the thread-list
    # sort-order fix), this store-level comparison only meaningfully covers the messaged thread
    # (+919664290413) -- the outbound-only thread (+918888888888) has no `messages` rows, so it is
    # correctly absent from both `before` and `after` here, degenerating to a no-op check for it.
    # `first == second` above/below is what still covers the outbound-only case, via
    # GET /admin/conversations' union of all three sources.
    before = {
        s.user_id: s.last_active_at
        for s in asyncio.run(c.conversations.recent_conversations(1000))
    }
    second = client.get("/admin/conversations").json()["threads"]
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

    rows = client.get("/admin/conversations").json()["threads"]
    order = [r["phone"] for r in rows if r["phone"] in (p, q)]

    assert order == [p, q]


def _seed_conv_at(phone_e164: str, when: datetime) -> int:
    async def _do() -> int:
        c = get_container()
        conv_id = await c.conversations.get_or_create(phone_e164)
        await c.conversations.append_message(conv_id, "user", "hi")
        c.conversations._last_active_at[conv_id] = when  # type: ignore[attr-defined]
        return conv_id

    return asyncio.run(_do())


def test_conversations_list_returns_envelope_with_pagination_fields(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")

    body = client.get("/admin/conversations").json()

    assert isinstance(body, dict)
    assert isinstance(body["threads"], list)
    assert "next_cursor" in body
    assert "has_more" in body


def test_conversations_list_paginates_older_with_cursor(client: TestClient) -> None:
    # The reported bug: only the 50 most-recent threads were reachable. Keyset paging must let a
    # second request fetch the strictly-older threads via the first page's next_cursor.
    login(client)
    _seed_conv_at("+919000000001", datetime(2026, 8, 10, tzinfo=UTC))
    _seed_conv_at("+919000000002", datetime(2026, 8, 11, tzinfo=UTC))
    _seed_conv_at("+919000000003", datetime(2026, 8, 12, tzinfo=UTC))

    page1 = client.get("/admin/conversations", params={"limit": 2}).json()
    phones1 = {t["phone"] for t in page1["threads"]}

    assert phones1 == {"+919000000003", "+919000000002"}
    assert page1["has_more"] is True
    assert page1["next_cursor"]

    page2 = client.get(
        "/admin/conversations",
        params={"limit": 2, "before_last_active_at": page1["next_cursor"]},
    ).json()
    phones2 = {t["phone"] for t in page2["threads"]}

    assert "+919000000001" in phones2
    assert "+919000000003" not in phones2
    assert page2["has_more"] is False


def test_conversations_list_pagination_surfaces_crowded_out_older_thread(
    client: TestClient,
) -> None:
    # Regression for the code-review "Major" finding: with a single union-boundary cursor, a phone
    # whose stamp straddles the cursor across sources can re-occupy a page slot and crowd out a
    # genuinely-unseen OLDER phone, producing a page that adds ZERO net-new threads while has_more
    # is still true. A client that stopped on the first zero-net-new page would PERMANENTLY hide the
    # crowded-out thread. Paging while has_more (the fixed client's auto-continue) must surface it.
    #
    # A and B: fresh outbound (page 1) + a lower conversation stamp that re-crowds page 2.
    # C: a conversation-only thread, older than A/B's outbound but NEWER than their conversation
    # stamps, so A/B's re-fetched conversation rows crowd it off page 2 under limit=2.
    login(client)
    c = get_container()
    _seed_conv_at("+919000000011", datetime(2026, 8, 20, 6, 5, tzinfo=UTC))  # A conv
    _seed_conv_at("+919000000012", datetime(2026, 8, 20, 6, 4, tzinfo=UTC))  # B conv
    _seed_conv_at("+919000000013", datetime(2026, 8, 20, 5, 0, tzinfo=UTC))  # C conv (the victim)
    _seed_outbound_at("+919000000011", "gid://shopify/Order/A", "{}")
    _seed_outbound_at("+919000000012", "gid://shopify/Order/B", "{}")
    meta = c.ingest._outbound_meta  # type: ignore[attr-defined]
    a_out = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    b_out = datetime(2026, 8, 20, 9, 59, tzinfo=UTC)
    meta["order_created:gid://shopify/Order/A"].created_at = a_out
    meta["order_created:gid://shopify/Order/B"].created_at = b_out

    # Simulate the client's "Load older" loop: keep paging while the server reports has_more.
    seen: set[str] = set()
    cursor: str | None = None
    saw_zero_new_page_with_more = False
    for _ in range(20):
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["before_last_active_at"] = cursor
        body = client.get("/admin/conversations", params=params).json()
        before = len(seen)
        seen.update(t["phone"] for t in body["threads"])
        if not body["has_more"]:
            break
        if len(seen) == before:
            saw_zero_new_page_with_more = True  # the crowding precondition actually occurred
        cursor = body["next_cursor"]
        if cursor is None:
            break

    # The crowded-out older thread is reachable, and only because we kept paging on has_more (a
    # stop-on-zero-new client would have quit at the page that surfaced nothing new).
    assert "+919000000013" in seen
    assert saw_zero_new_page_with_more


def test_conversations_list_adversarial_cursor_does_not_500(client: TestClient) -> None:
    # Security regression: a NAIVE (no-offset) cursor used to raise TypeError (naive-vs-aware
    # datetime compare) in the in-memory list-source queries -> 500 on the live path whenever
    # DATABASE_URL is unset. A malformed/extreme cursor must degrade to page 1, never crash.
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")

    for cursor in ("2026-01-01T00:00:00", "not-a-timestamp", "9999-12-31T23:59:59-23:59"):
        resp = client.get("/admin/conversations", params={"before_last_active_at": cursor})
        assert resp.status_code == 200, cursor
        assert isinstance(resp.json()["threads"], list)


def test_conversations_list_search_finds_old_thread_by_order_name(client: TestClient) -> None:
    # Search must run server-side over the FULL order mirror, not just the loaded page -- so a
    # customer whose only footprint is a mirrored order (no chat / no button tap) is still findable.
    login(client)
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/searchme",
        "name": "tavas4156",
        "phone": "+919384880222",
        "updated_at": "2026-08-15T10:00:00+05:30",
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/searchme",
            "first_name": "Amita", "last_name": "Chadha",
        },
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    by_order = client.get("/admin/conversations", params={"q": "4156"}).json()
    assert "+919384880222" in {t["phone"] for t in by_order["threads"]}

    by_name = client.get("/admin/conversations", params={"q": "amita"}).json()
    assert "+919384880222" in {t["phone"] for t in by_name["threads"]}


def test_conversations_list_search_by_phone_substring(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    _send_ai_message("+917000000000", "hi", "hello there")

    body = client.get("/admin/conversations", params={"q": "6642"}).json()
    phones = {t["phone"] for t in body["threads"]}

    assert "+919664290413" in phones
    assert "+917000000000" not in phones


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

    entries = resp.json()["entries"]
    for entry in entries:
        if entry["type"] != "template_sent":
            assert "status" not in entry

    # A customer's own inbound text is never an outbound send, so it must never carry a
    # delivery_status field -- only ai_reply/template_sent entries can. (Guards the router's
    # per-entry shaping from ever leaking a delivery_status key onto an inbound bubble.)
    customer_entry = next(e for e in entries if e["type"] == "customer_message")
    assert "delivery_status" not in customer_entry


def test_conversation_thread_includes_customer_image_entry(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    async def _save_image() -> int:
        return await get_container().ingest.save_inbound_image(
            "+919664290413", "wamid.IMG1", "image/jpeg", b"\xff\xd8\xff\xe0fakejpeg"
        )

    image_id = asyncio.run(_save_image())

    resp = client.get(f"/admin/conversations/{thread_id}")
    entries = resp.json()["entries"]
    image_entry = next(e for e in entries if e["type"] == "customer_image")
    assert image_entry["image_id"] == image_id
    assert image_entry["mime_type"] == "image/jpeg"


def test_get_conversation_image_returns_bytes(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    async def _save_image() -> int:
        return await get_container().ingest.save_inbound_image(
            "+919664290413", "wamid.IMG1", "image/png", b"\x89PNGfakepng"
        )

    image_id = asyncio.run(_save_image())

    resp = client.get(f"/admin/conversations/{thread_id}/images/{image_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNGfakepng"


def test_get_conversation_image_404s_for_wrong_thread(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    _send_ai_message("+919876500000", "hi", "hello there")
    thread_a = _thread_id_for(client, "+919664290413")

    async def _save_image() -> int:
        return await get_container().ingest.save_inbound_image(
            "+919876500000", "wamid.IMG2", "image/jpeg", b"other-customer-photo"
        )

    image_id = asyncio.run(_save_image())

    resp = client.get(f"/admin/conversations/{thread_a}/images/{image_id}")
    assert resp.status_code == 404


def test_get_conversation_image_404s_for_missing_id(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    resp = client.get(f"/admin/conversations/{thread_id}/images/999999")
    assert resp.status_code == 404


def test_get_conversation_image_404s_for_unknown_thread(client: TestClient) -> None:
    login(client)
    resp = client.get("/admin/conversations/999999/images/1")
    assert resp.status_code == 404


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


def test_conversation_thread_includes_exchange_details_when_a_request_exists(
    client: TestClient,
) -> None:
    login(client)
    phone = "+919876500099"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/exchange1",
        "name": "tavas9901",
        "phone": phone,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "999.00",
        "currency": "INR",
        "updated_at": "2026-08-20T10:00:00+05:30",
        "line_items": [],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))
    created = asyncio.run(
        get_container().exchanges.create("gid://shopify/Order/exchange1", "tavas9901", phone, "M")
    )

    thread_id = asyncio.run(get_container().conversations.get_or_create(phone))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    assert orders[0]["exchange"] == {
        "id": created.id, "requested_size": "M", "status": "requested",
        "requested_at": created.requested_at, "return_tracking_url": None,
        "replacement_tracking_url": None,
    }


def test_conversation_thread_order_has_no_exchange_key_when_none_exists(
    client: TestClient,
) -> None:
    login(client)
    phone = "+919876500098"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/no-exchange1",
        "name": "tavas9902",
        "phone": phone,
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "499.00",
        "currency": "INR",
        "updated_at": "2026-08-20T10:00:00+05:30",
        "line_items": [],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    thread_id = asyncio.run(get_container().conversations.get_or_create(phone))
    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    assert "exchange" not in orders[0]


def test_conversation_thread_survives_exchange_lookup_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    phone = "+919876500097"
    order = order_from_webhook_payload({
        "admin_graphql_api_id": "gid://shopify/Order/exch-fail1",
        "name": "tavas9903",
        "phone": phone,
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "tags": "",
        "payment_gateway_names": [],
        "total_price": "599.00",
        "currency": "INR",
        "updated_at": "2026-08-20T10:00:00+05:30",
        "line_items": [],
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))

    async def _boom(_phone: str) -> list[object]:
        raise RuntimeError("exchange_requests table does not exist")

    monkeypatch.setattr(get_container().exchanges, "list_for_phone", _boom)

    thread_id = asyncio.run(get_container().conversations.get_or_create(phone))
    resp = client.get(f"/admin/conversations/{thread_id}")

    # The exchange lookup blew up, but entries/orders must still return -- the order is present
    # and simply carries no exchange details (empty fallback), never a 500.
    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    assert "exchange" not in orders[0]


def test_update_exchange_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/exchanges/1", json={"status": "return_picked_up"})
    assert resp.status_code == 401


def test_update_exchange_unknown_id_returns_404(client: TestClient) -> None:
    login(client)
    resp = client.post("/admin/exchanges/999999", json={"status": "return_picked_up"})
    assert resp.status_code == 404


def test_update_exchange_sets_status(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/1", "tavas1", "+919999999999", "M")
    )
    resp = client.post(f"/admin/exchanges/{created.id}", json={"status": "qc_passed"})
    assert resp.status_code == 200
    updated = asyncio.run(get_container().exchanges.get(created.id))
    assert updated is not None
    assert updated.status == "qc_passed"


def test_update_exchange_sets_return_tracking_url(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/2", "tavas2", "+919999999999", "L")
    )
    resp = client.post(
        f"/admin/exchanges/{created.id}", json={"return_tracking_url": "https://track/xyz"}
    )
    assert resp.status_code == 200
    updated = asyncio.run(get_container().exchanges.get(created.id))
    assert updated is not None
    assert updated.return_tracking_url == "https://track/xyz"


def test_update_exchange_sets_replacement_tracking_url(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/4", "tavas4", "+919999999999", "XL")
    )
    resp = client.post(
        f"/admin/exchanges/{created.id}",
        json={"replacement_tracking_url": "https://track/repl-xyz"},
    )
    assert resp.status_code == 200
    updated = asyncio.run(get_container().exchanges.get(created.id))
    assert updated is not None
    assert updated.replacement_tracking_url == "https://track/repl-xyz"


def test_update_exchange_store_lookup_failure_returns_503_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A store-level failure on the pre-update lookup (DB blip, schema drift) must surface as a
    # clean 503, matching this router's "store unavailable" convention -- never a raw 500.
    login(client)

    async def _boom(_id: int) -> object:
        raise RuntimeError("exchange_requests table does not exist")

    monkeypatch.setattr(get_container().exchanges, "get", _boom)

    resp = client.post("/admin/exchanges/1", json={"status": "return_picked_up"})

    assert resp.status_code == 503


def test_update_exchange_rejects_invalid_status(client: TestClient) -> None:
    login(client)
    created = asyncio.run(
        get_container().exchanges.create("gid://o/3", "tavas3", "+919999999999", "S")
    )
    resp = client.post(f"/admin/exchanges/{created.id}", json={"status": "not_a_real_status"})
    assert resp.status_code == 422


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

    rows = client.get("/admin/conversations").json()["threads"]

    row = next(r for r in rows if r["phone"] == normalized)
    assert row["customer_name"] == "Priya Shah"
    assert set(row["order_names"]) == {"tavas700", "tavas701"}


def test_conversations_list_no_orders_has_null_name_empty_order_names(
    client: TestClient,
) -> None:
    login(client)
    _send_ai_message("+919876500011", "hi", "hello")

    rows = client.get("/admin/conversations").json()["threads"]

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

    rows = client.get("/admin/conversations").json()["threads"]

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


class _FakeTextSender:
    def __init__(self, result: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(self, http, cfg, to, body, timeout=20.0):
        self.calls.append({"to": to, "body": body, "timeout": timeout})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeTemplateSender:
    """Records send_template calls, for the template-resend endpoint's tests. Mirrors
    test_outbox_drain_job.py's FakeSender (same patch target, app.jobs.outbox_drain.send_template
    -- the admin router never calls send_template directly, only via send_inline_outbound)."""

    def __init__(self, result: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ):
        self.calls.append(
            {
                "to": to,
                "template": template_name,
                "language": language,
                "body_params": body_params,
                "button_payloads": list(button_payloads),
                "header_image_url": header_image_url,
                "timeout": timeout,
            }
        )
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _seed_whatsapp_config() -> None:
    c = get_container()
    asyncio.run(c.config.set_secret("whatsapp:access_token", "tok"))
    asyncio.run(c.config.set_secret("whatsapp:app_secret", "sec"))
    asyncio.run(c.config.set_secret("whatsapp:verify_token", "ver"))
    asyncio.run(c.config.set_plain("whatsapp:phone_number_id", "1298805403309058"))
    asyncio.run(c.config.set_plain("whatsapp:waba_id", "2454816495000045"))
    asyncio.run(c.config.set_plain("whatsapp:api_version", "v23.0"))


def test_manual_reply_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/conversations/1/messages", json={"text": "hi"})
    assert resp.status_code == 401


def test_manual_reply_unknown_thread_id_returns_404(client: TestClient, monkeypatch) -> None:
    login(client)
    _seed_whatsapp_config()
    resp = client.post("/admin/conversations/900000000002/messages", json={"text": "hi"})
    assert resp.status_code == 404


def test_manual_reply_rejects_empty_text(client: TestClient) -> None:
    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500040"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "   "})
    assert resp.status_code == 400


def test_manual_reply_sends_persists_and_pauses_ai(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500041"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    fake = _FakeTextSender(SendResult(ok=True, status_code=200, wamid="wamid.MANUAL1", error=None))
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "On its way!"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "wamid": "wamid.MANUAL1"}
    assert fake.calls == [{"to": normalized, "body": "On its way!", "timeout": 20.0}]

    messages = asyncio.run(
        get_container().conversations.find_messages_by_user_id(normalized, limit=10)
    )
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "On its way!"
    assert messages[-1].sender == "admin"

    paused_until = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert paused_until is not None
    assert paused_until > datetime.now(UTC) + timedelta(hours=23)


def test_manual_reply_send_mode_off_still_sends(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual admin reply deliberately bypasses the send_mode kill switch (design decision)."""
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500042"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    from app.admin.controls import save_controls

    asyncio.run(
        save_controls(
            get_container().config,
            AdminControls(
                send_mode="off",
                allowlist_phones=[],
                owner_alert_number="",
                default_language="en",
            ),
        )
    )

    fake = _FakeTextSender(SendResult(ok=True, status_code=200, wamid="wamid.MANUAL2", error=None))
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "hello"})

    assert resp.status_code == 200
    assert len(fake.calls) == 1


def test_manual_reply_reports_send_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.whatsapp_sender import WhatsAppSendError

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500043"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    fake = _FakeTextSender(WhatsAppSendError("timeout"))
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "hello"})

    assert resp.status_code == 502
    assert resp.json()["ok"] is False

    messages = asyncio.run(
        get_container().conversations.find_messages_by_user_id(normalized, limit=10)
    )
    assert messages[-1].content == "hello"
    assert messages[-1].sender == "admin"
    # A failed send (no wamid ever arrived) is marked "failed" so the UI shows the red "!" tick
    # instead of a misleading grey "sent" tick, and delivery_retry never treats it as sent.
    assert messages[-1].delivery_status == "failed"

    # A failed send must NOT pause/mute the AI (unlike the customer-initiated handoff path): a
    # persistently failing send would otherwise silently mute the AI for 24h with no owner alert.
    paused_until = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert paused_until is None


def test_manual_reply_marks_failed_when_result_not_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ok SendResult (send returned but Meta rejected it) also marks the row failed and
    surfaces Meta's error, without pausing the AI."""
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500045"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    fake = _FakeTextSender(
        SendResult(ok=False, status_code=400, wamid=None, error="invalid recipient")
    )
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "hello"})

    assert resp.status_code == 502
    assert resp.json() == {"ok": False, "error": "invalid recipient"}

    messages = asyncio.run(
        get_container().conversations.find_messages_by_user_id(normalized, limit=10)
    )
    assert messages[-1].delivery_status == "failed"
    paused_until = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert paused_until is None


def test_manual_reply_returns_503_and_marks_failed_when_whatsapp_unconfigured(
    client: TestClient,
) -> None:
    """With no WhatsApp config, the send can't happen: return 503 and mark the persisted row
    failed so it shows the red "!" tick and never looks like a delivered message."""
    login(client)
    # Deliberately do NOT call _seed_whatsapp_config() -> load_whatsapp_config returns None.
    normalized = "+919876500046"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    resp = client.post(f"/admin/conversations/{thread_id}/messages", json={"text": "hello"})

    assert resp.status_code == 503

    messages = asyncio.run(
        get_container().conversations.find_messages_by_user_id(normalized, limit=10)
    )
    assert messages[-1].content == "hello"
    assert messages[-1].delivery_status == "failed"
    paused_until = asyncio.run(get_container().conversations.get_paused_until(thread_id))
    assert paused_until is None


def test_manual_reply_row_visible_to_memory_and_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a row CREATED BY THE ENDPOINT (via a real POST) must be visible to the AI's
    memory window (core/memory.load_history) and findable by delivery_retry's wamid lookup."""
    from app.channels.whatsapp_sender import SendResult
    from app.core.memory import load_history

    login(client)
    _seed_whatsapp_config()
    normalized = "+919876500047"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))

    fake = _FakeTextSender(
        SendResult(ok=True, status_code=200, wamid="wamid.MANUAL_E2E", error=None)
    )
    monkeypatch.setattr("app.admin.router.send_text", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/messages", json={"text": "shipped today"}
    )
    assert resp.status_code == 200

    # (1) The manually-sent row is replayed into the AI's memory window as an assistant turn.
    _conv_id, history = asyncio.run(load_history(get_container().conversations, normalized))
    assert any(m.role == "assistant" and m.content == "shipped today" for m in history)

    # (2) The same row is findable by delivery_retry via its wamid (so a delivery-failure webhook
    # could resend it), proving the endpoint's output is on the standard retry path.
    info = asyncio.run(
        get_container().conversations.get_message_retry_info("wamid.MANUAL_E2E")
    )
    assert info is not None
    assert info.content == "shipped today"


def test_conversation_thread_reports_sender_on_manual_reply(client: TestClient) -> None:
    login(client)
    normalized = "+919876500044"
    thread_id = asyncio.run(get_container().conversations.get_or_create(normalized))
    asyncio.run(
        get_container().conversations.append_message(
            thread_id, "assistant", "manual text", sender="admin"
        )
    )

    resp = client.get(f"/admin/conversations/{thread_id}")

    assert resp.status_code == 200
    ai_entries = [e for e in resp.json()["entries"] if e["type"] == "ai_reply"]
    assert ai_entries[-1]["sender"] == "admin"


def _seed_order_for_thread(
    order_name: str = "tavas5001", phone: str = "+919876500050",
    order_gid: str = "gid://shopify/Order/50001",
    cancelled_at: str | None = None, fulfilled: bool = False,
    payment_gateway_names: list[str] | None = None,
) -> int:
    # NOTE: find_mirrored_orders_by_phone (used by both new endpoints) reads from the order
    # MIRROR (ingest.upsert_order_mirror), not from order_mappings -- ingest_order_created alone
    # (the pattern used by _ingest_one/_seed_outbound_at above) would leave the mirror empty and
    # every order lookup 404. Build a real Order via the same order_from_webhook_payload path
    # test_conversation_thread_includes_order_summary already uses, and mirror it directly.
    order = order_from_webhook_payload({
        "admin_graphql_api_id": order_gid,
        "name": order_name,
        "phone": phone,
        "financial_status": "pending",
        "fulfillment_status": "fulfilled" if fulfilled else None,
        "cancelled_at": cancelled_at,
        "tags": "",
        "payment_gateway_names": (
            payment_gateway_names
            if payment_gateway_names is not None
            else ["Cash on Delivery (COD)"]
        ),
        "total_price": "999.00",
        "currency": "INR",
        "updated_at": "2026-08-19T10:00:00+05:30",
    })
    assert order is not None
    asyncio.run(get_container().ingest.upsert_order_mirror(order))
    if fulfilled:
        from app.shopify.models import Fulfillment

        asyncio.run(
            get_container().ingest.upsert_fulfillment(
                order_gid,
                Fulfillment(
                    gid="gid://shopify/Fulfillment/50001",
                    status="success",
                    tracking_company="Delhivery",
                    tracking_number="AWB50001",
                    tracking_url="https://track.example/AWB50001",
                ),
            )
        )
    return asyncio.run(get_container().conversations.get_or_create(phone))


def test_list_templates_requires_auth(client: TestClient) -> None:
    resp = client.get("/admin/conversations/1/templates")
    assert resp.status_code == 401


def test_list_templates_unknown_thread_returns_404(client: TestClient) -> None:
    login(client)
    resp = client.get("/admin/conversations/900000000003/templates")
    assert resp.status_code == 404


def test_list_templates_returns_all_four_with_defaults(client: TestClient) -> None:
    login(client)
    # A fulfilled, non-cancelled order is eligible for all four templates (the state filter only
    # trims cancelled orders' confirmations and unfulfilled orders' shipped/delivered notices).
    thread_id = _seed_order_for_thread(fulfilled=True)
    resp = client.get(f"/admin/conversations/{thread_id}/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["orders"]) == 1
    keys = {t["key"] for t in data["orders"][0]["templates"]}
    assert keys == {"cod_confirmation", "prepaid_order", "order_shipped", "order_delivered"}
    cod = next(t for t in data["orders"][0]["templates"] if t["key"] == "cod_confirmation")
    assert cod["has_buttons"] is True
    order_id_field = next(f for f in cod["fields"] if f["key"] == "order_id")
    assert order_id_field["value"] == "tavas5001"
    # Finding 3: the pinned order-identity field is shown read-only in the dialog form.
    assert order_id_field["read_only"] is True


def test_list_templates_cancelled_order_excludes_confirmation_templates(
    client: TestClient,
) -> None:
    # Finding 10: a cancelled order must not offer cod_confirmation/prepaid_order (their live
    # Confirm/Cancel buttons make no sense for an already-cancelled order) -- support noise only,
    # not a mutation-safety gate. It is fulfilled, so the shipped/delivered notices still apply.
    login(client)
    thread_id = _seed_order_for_thread(
        phone="+919876500060", cancelled_at="2026-08-19T09:00:00+05:30", fulfilled=True,
    )
    resp = client.get(f"/admin/conversations/{thread_id}/templates")
    assert resp.status_code == 200
    keys = {t["key"] for t in resp.json()["orders"][0]["templates"]}
    assert "cod_confirmation" not in keys
    assert "prepaid_order" not in keys
    assert keys == {"order_shipped", "order_delivered"}


def test_list_templates_prepaid_order_excludes_cod_confirmation(client: TestClient) -> None:
    # Security review (2026-08-22): cod_confirmation is the only template carrying the live
    # Confirm/Cancel buttons, so offering it for a PREPAID order would emit an order:cancel button
    # to a customer whose order can never be cancelled. Gate it on the gateway-only COD check.
    login(client)
    thread_id = _seed_order_for_thread(
        phone="+919876500062", payment_gateway_names=["Razorpay"],
    )
    resp = client.get(f"/admin/conversations/{thread_id}/templates")
    assert resp.status_code == 200
    keys = {t["key"] for t in resp.json()["orders"][0]["templates"]}
    assert "cod_confirmation" not in keys
    # prepaid_order (no buttons) still applies for a prepaid, unfulfilled order.
    assert keys == {"prepaid_order"}


def test_list_templates_cod_order_still_includes_cod_confirmation(client: TestClient) -> None:
    # The gateway-only gate must NOT over-trim: a genuine COD order still offers cod_confirmation.
    login(client)
    thread_id = _seed_order_for_thread(
        phone="+919876500063", payment_gateway_names=["Cash on Delivery (COD)"],
    )
    resp = client.get(f"/admin/conversations/{thread_id}/templates")
    assert resp.status_code == 200
    keys = {t["key"] for t in resp.json()["orders"][0]["templates"]}
    assert "cod_confirmation" in keys


def test_list_templates_unfulfilled_order_excludes_shipped_and_delivered(
    client: TestClient,
) -> None:
    # Finding 10: an order with no fulfillments has nothing to report shipped/delivered on.
    login(client)
    thread_id = _seed_order_for_thread(phone="+919876500061", fulfilled=False)
    resp = client.get(f"/admin/conversations/{thread_id}/templates")
    assert resp.status_code == 200
    keys = {t["key"] for t in resp.json()["orders"][0]["templates"]}
    assert "order_shipped" not in keys
    assert "order_delivered" not in keys
    assert keys == {"cod_confirmation", "prepaid_order"}


def test_send_template_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/admin/conversations/1/templates",
        json={"order_name": "tavas1", "template": "order_shipped", "values": {}},
    )
    assert resp.status_code == 401


def test_send_template_rejects_unknown_template_key(client: TestClient) -> None:
    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(phone="+919876500051")
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5001", "template": "not_a_real_template", "values": {}},
    )
    assert resp.status_code == 400


def test_send_template_rejects_unknown_order_name(client: TestClient) -> None:
    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(phone="+919876500052")
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas_does_not_exist", "template": "order_shipped", "values": {}},
    )
    assert resp.status_code == 404


def test_send_template_cod_confirmation_rejected_for_prepaid_order(
    client: TestClient,
) -> None:
    # Security review (2026-08-22): the POST send path must re-apply the same
    # _template_applies_to_order filter as the GET list endpoint. A direct POST of
    # cod_confirmation (the only template with live Confirm/Cancel buttons) for a PREPAID order
    # must be rejected outright with 400 rather than silently emit an order:cancel button the
    # customer's order can never honour (safely refused if tapped, but a dead button).
    login(client)
    _seed_whatsapp_config()
    thread_id = _seed_order_for_thread(
        order_name="tavas5090", phone="+919876500090", payment_gateway_names=["Razorpay"],
    )
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5090", "template": "cod_confirmation", "values": {}},
    )
    assert resp.status_code == 400


def test_send_template_cod_confirmation_still_succeeds_for_cod_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for the filter above: a GENUINE COD order (COD gateway) must still be able
    # to receive cod_confirmation via the POST send path.
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    thread_id = _seed_order_for_thread(
        order_name="tavas5091", phone="+919876500091",
        payment_gateway_names=["Cash on Delivery (COD)"],
    )
    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPLCOD", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5091", "template": "cod_confirmation", "values": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_send_template_positional_shipped_sends_and_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    # send_mode defaults to "off" (AdminControls.send_mode) -- this endpoint respects the kill
    # switch same as every automatic template send, so a genuine send requires "live" here.
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    # order_shipped only applies once the order is fulfilled (the POST re-applies the same
    # _template_applies_to_order filter as the list endpoint -- security review 2026-08-22).
    thread_id = _seed_order_for_thread(
        order_name="tavas5002", phone="+919876500053", fulfilled=True,
    )

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL1", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={
            "order_name": "tavas5002", "template": "order_shipped",
            "values": {"tracking_company": "Delhivery"},
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "sent"}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["template"] == "order_shipped"
    assert isinstance(call["body_params"], list)
    assert call["body_params"][1] == "tavas5002"  # order_name field, positional index 1
    assert call["body_params"][2] == "Delhivery"  # admin-edited override
    assert call["button_payloads"] == []


def test_send_template_cod_confirmation_uses_server_resolved_gid_for_buttons(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    thread_id = _seed_order_for_thread(order_name="tavas5003", phone="+919876500054")

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL2", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    # Request tries to smuggle an unrelated gid inside `values` -- it must be ignored, since gid
    # is never read from the request body at all, only re-resolved server-side by order_name.
    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={
            "order_name": "tavas5003", "template": "cod_confirmation",
            "values": {"gid": "gid://shopify/Order/ATTACKER"},
        },
    )
    assert resp.status_code == 200
    call = fake.calls[0]
    assert call["button_payloads"] == [
        "order:confirm:gid://shopify/Order/50001", "order:cancel:gid://shopify/Order/50001",
    ]


def test_send_template_kill_switch_off_leaves_it_queued_not_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike send_manual_reply's free text, a template resend respects send_mode -- it goes
    through the same enqueue_outbound + send_inline_outbound pipeline as every automatic template
    send, and send_inline_outbound leaves an 'off'-mode row queued for the backstop drain rather
    than sending immediately or failing. Enqueuing successfully is reported as {"ok": true}: it
    is not a failure, the row is just not sent YET."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    # order_delivered only applies to a fulfilled order (POST re-applies the filter).
    thread_id = _seed_order_for_thread(
        order_name="tavas5004", phone="+919876500055", fulfilled=True,
    )
    asyncio.run(save_controls(
        get_container().config,
        AdminControls(
            send_mode="off", allowlist_phones=[], owner_alert_number="", default_language="en",
        ),
    ))

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL3", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5004", "template": "order_delivered", "values": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "queued"}
    assert len(fake.calls) == 0  # never reached Meta -- send_mode="off" left the row queued


def test_send_template_allowlist_miss_reports_success_not_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A phone not on the allowlist is a POLICY-WITHHELD send (send_decision -> "suppress" ->
    OUTCOME_SUPPRESSED), not a failure the admin needs to retry -- same category as send_mode="off"
    leaving the row queued. Regression for the bug where OUTCOME_SUPPRESSED was lumped in with the
    genuine failure outcomes and this returned a 502."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    # order_delivered only applies to a fulfilled order (POST re-applies the filter).
    thread_id = _seed_order_for_thread(
        order_name="tavas5005", phone="+919876500056", fulfilled=True,
    )
    asyncio.run(save_controls(
        get_container().config,
        AdminControls(
            send_mode="allowlist", allowlist_phones=[], owner_alert_number="",
            default_language="en",
        ),
    ))

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL4", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5005", "template": "order_delivered", "values": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "suppressed"}
    assert len(fake.calls) == 0  # never reached Meta -- suppressed by the empty allowlist


def test_send_template_blank_field_sends_placeholder_not_empty_string(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: Meta rejects an empty template body parameter, so a blank field must be
    substituted with EMPTY_PARAM_PLACEHOLDER ("-") in body_params, never sent as "". Here a
    fulfilled order (so order_shipped applies -- the POST re-applies _template_applies_to_order,
    security review 2026-08-22) with NO customer record leaves the `name` field blank."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.copy import EMPTY_PARAM_PLACEHOLDER
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    # Fulfilled order (order_shipped applies), but the seeded order carries no customer record, so
    # the `name` field (positional index 0) defaults to "" with no admin override.
    thread_id = _seed_order_for_thread(
        order_name="tavas5006", phone="+919876500057", fulfilled=True,
    )

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL5", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5006", "template": "order_shipped", "values": {}},
    )
    assert resp.status_code == 200
    assert len(fake.calls) == 1
    body_params = fake.calls[0]["body_params"]
    assert isinstance(body_params, list)
    # name (index 0) had no value -> placeholder, not "".
    assert body_params[0] == EMPTY_PARAM_PLACEHOLDER
    assert "" not in body_params


def test_send_template_order_id_pinned_to_server_value_ignoring_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: the admin-editable order_id field must be force-overwritten with the
    server-resolved order.name, so the visible order text can never desync from the button
    payloads (which are pinned to the server gid)."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    thread_id = _seed_order_for_thread(order_name="tavas5007", phone="+919876500058")

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL6", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={
            "order_name": "tavas5007", "template": "cod_confirmation",
            "values": {"order_id": "some-other-order"},
        },
    )
    assert resp.status_code == 200
    body_params = fake.calls[0]["body_params"]
    assert isinstance(body_params, dict)
    assert body_params["order_id"] == "tavas5007"  # server value, NOT the submitted override


def test_send_template_drops_values_key_not_in_template_field_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 8: a `values` key that is not one of the picked template's fields must never appear
    anywhere in the body_params sent to Meta."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    thread_id = _seed_order_for_thread(order_name="tavas5008", phone="+919876500059")

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL7", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={
            "order_name": "tavas5008", "template": "cod_confirmation",
            "values": {
                "customer_name": "Legit", "gid": "gid://shopify/Order/999",
                "unexpected_key": "x",
            },
        },
    )
    assert resp.status_code == 200
    body_params = fake.calls[0]["body_params"]
    assert isinstance(body_params, dict)
    assert "gid" not in body_params
    assert "unexpected_key" not in body_params
    assert "gid" not in body_params.values()
    assert "x" not in body_params.values()


def test_send_template_does_not_touch_order_mappings_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 6: a resend uses the "admin_resend:" dedupe family (NOT the order-confirmation
    family), so a successful send must never advance order_mappings.status."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult
    from app.store.base import MappingUpsert

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    order_gid = "gid://shopify/Order/50060"
    thread_id = _seed_order_for_thread(
        order_name="tavas5060", phone="+919876500062", order_gid=order_gid,
    )
    # Seed an order_mappings row for the SAME gid with a known status ("pending").
    mapping = MappingUpsert(
        order_gid=order_gid, order_name="tavas5060", order_number_int=5060,
        phone_e164="+919876500062", customer_name="Test", email=None, language="en",
        financial_status_at_create="pending", is_cod=True,
    )
    outbound = OutboundDraft(
        dedupe_key=f"order_created:{order_gid}", kind="order_confirmation",
        phone_e164="+919876500062", payload_json="{}",
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            f"wh-{order_gid}", "orders/create", mapping, outbound
        )
    )

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL8", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5060", "template": "cod_confirmation", "values": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "sent"}

    mappings = {m.order_gid: m for m in asyncio.run(get_container().ingest.recent_mappings(20))}
    assert mappings[order_gid].status == "pending"  # unchanged by the resend


def test_queued_admin_resend_row_drains_via_backstop_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 7: a resend enqueued while send_mode="off" stays queued; once send_mode flips to
    "live", the backstop run_outbox_drain must actually send the previously-queued admin_resend
    row -- proving the payload shape is drain-compatible, protected by a real regression test."""
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult
    from app.jobs.outbox_drain import run_outbox_drain

    login(client)
    _seed_whatsapp_config()
    asyncio.run(save_controls(
        get_container().config,
        AdminControls(
            send_mode="off", allowlist_phones=[], owner_alert_number="", default_language="en",
        ),
    ))
    # order_delivered only applies to a fulfilled order (POST re-applies the filter).
    thread_id = _seed_order_for_thread(
        order_name="tavas5070", phone="+919876500063", fulfilled=True,
    )

    fake = _FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.TPL9", error=None)
    )
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", fake)

    resp = client.post(
        f"/admin/conversations/{thread_id}/templates",
        json={"order_name": "tavas5070", "template": "order_delivered", "values": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "queued"}
    assert len(fake.calls) == 0  # off -> nothing sent inline

    # Flip to live and run the backstop drain: the queued admin_resend row now actually sends.
    asyncio.run(save_controls(get_container().config, AdminControls(send_mode="live")))
    summary = asyncio.run(run_outbox_drain(get_container()))
    assert summary["sent"] == 1
    assert len(fake.calls) == 1
    assert fake.calls[0]["template"] == "order_delivered"
