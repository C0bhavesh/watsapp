import asyncio
import base64
import hashlib
import hmac as hmac_lib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.deps import get_container, reset_container
from app.shopify.models import Customer

SECRET = "csec-webhook"
# The per-store "webhook signing secret" shown on Admin -> Settings -> Notifications, used to
# sign webhooks the owner creates there (distinct from the app's own client_secret above).
WEBHOOK_SIGNING_SECRET = "wss-admin-created"


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
        "customer": {
            "admin_graphql_api_id": "gid://shopify/Customer/9001",
            "first_name": "Suman", "last_name": "B",
        },
        "shipping_address": {"phone": "+919664290413"},
        "tags": "COD",
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "949.00",
        "created_at": datetime.now(UTC).isoformat(),
        "customer_locale": "hi-IN",
    }


def sign(body: bytes) -> str:
    return base64.b64encode(hmac_lib.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


def sign_with(secret: str, body: bytes) -> str:
    return base64.b64encode(
        hmac_lib.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


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
    assert params["template"] == "cod_confirmation"
    # cod_confirmation is en-only on the WABA, so the template send is pinned to en even for a
    # hi-IN customer locale (the free-form conversation still uses the customer's language).
    assert params["language"] == "en"
    assert params["buttons"] == [
        "order:confirm:gid://shopify/Order/1", "order:cancel:gid://shopify/Order/1",
    ]
    assert params["body_params"]["customer_name"] == "Suman B"
    assert params["body_params"]["order_id"] == "tavas3733"
    assert params["body_params"]["product_amount"] == "949.00"
    # This payload() carries no line_items, so the product fields degrade to the placeholder and
    # no header image is resolved (no Shopify call made).
    assert params["body_params"]["product_name"] == "-"
    assert params["body_params"]["product_color"] == "-"
    assert params["body_params"]["product_size"] == "-"
    assert "image_url" not in params
    assert draft.phone_e164 == "+919664290413"


async def test_prepaid_order_routes_to_prepaid_template() -> None:
    # A prepaid order is only push-eligible under an "all"/"all_prepaid_no_buttons" policy; the
    # default "cod_only" would filter it out before routing (is_eligible_for_push), so set a policy
    # that pushes prepaid orders to actually exercise the COD-vs-prepaid template selection.
    await get_container().config.set_plain("push_policy", "all")
    body_dict = payload()
    body_dict["tags"] = ""  # no "cod" tag
    body_dict["payment_gateway_names"] = ["Razorpay"]  # not a COD gateway
    body = json.dumps(body_dict).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    store = get_container().ingest
    draft = store.outbound["order_created:gid://shopify/Order/1"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "prepaid_order"
    assert params["language"] == "en"
    assert "buttons" not in params


async def test_prepaid_order_with_cod_tag_gets_prepaid_template_no_buttons() -> None:
    # Security review (2026-08-22): the "cod" ORDER TAG is app-writable (add_tags, third-party
    # apps, Shopify Flow), so a genuinely prepaid order carrying a stray "cod" tag must NOT be
    # pushed the cod_confirmation template with a live-looking Cancel button (safely refused if
    # tapped, but confusing). is_eligible_for_push still uses the generous tag-inclusive is_cod()
    # (unchanged), so under the default cod_only policy the tag alone makes this order push --
    # but the template/button choice now uses the gateway-only check, so it must land on
    # prepaid_order with NO Confirm/Cancel buttons.
    body_dict = payload()
    body_dict["tags"] = "COD"  # app-writable tag present ...
    body_dict["payment_gateway_names"] = ["Razorpay"]  # ... but NOT a COD gateway
    body = json.dumps(body_dict).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    assert resp.json()["queued"] is True  # still pushed via is_eligible_for_push (tag match)
    store = get_container().ingest
    draft = store.outbound["order_created:gid://shopify/Order/1"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "prepaid_order"
    assert "buttons" not in params


def _payload_with_product(gid: str = "gid://shopify/Order/img1") -> dict:
    p = payload(gid)
    p["line_items"] = [
        {
            "title": "Chic Kurta Set", "product_id": 15061451407728,
            "variant_id": 53472318620016,
            "variant_title": "Cream / M", "quantity": 1, "price": "1299.00",
        }
    ]
    return p


async def test_orders_create_resolves_product_image_into_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Q19a: the product photo is fetched live at ingest and cached in payload_json (so the inline
    # send + backstop drain + reminder all reuse it with no extra Shopify call).
    c = get_container()
    seen: dict = {}

    async def fake_image(product_gid: str, variant_gid: str | None = None) -> str:
        seen["gid"] = product_gid
        seen["variant_gid"] = variant_gid
        return "https://cdn.shopify.com/s/files/1/kurta.jpg"

    monkeypatch.setattr(c.shopify, "get_product_image_url", fake_image)

    body = json.dumps(_payload_with_product()).encode()
    resp = await post(body, headers(body, webhook_id="wh-img1"))

    assert resp.status_code == 200
    draft = c.ingest.outbound["order_created:gid://shopify/Order/img1"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert seen["gid"] == "gid://shopify/Product/15061451407728"
    # Bug fix: the ordered variant's own gid must be threaded all the way to the client call, not
    # just the product gid -- this is the assertion that would have caught the original bug.
    assert seen["variant_gid"] == "gid://shopify/ProductVariant/53472318620016"
    assert params["body_params"]["product_name"] == "Chic Kurta Set"
    assert params["body_params"]["product_color"] == "Cream"
    assert params["body_params"]["product_size"] == "M"
    assert params["image_url"] == "https://cdn.shopify.com/s/files/1/kurta.jpg"


async def test_orders_create_degrades_gracefully_when_image_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Shopify hiccup during image resolution must NOT block the confirmation: still 200, still
    # queued, just no image_url (the send goes out with no header).
    c = get_container()

    async def boom(product_gid: str, variant_gid: str | None = None) -> str:
        raise RuntimeError("shopify down")

    monkeypatch.setattr(c.shopify, "get_product_image_url", boom)

    body = json.dumps(_payload_with_product("gid://shopify/Order/img2")).encode()
    resp = await post(body, headers(body, webhook_id="wh-img2"))

    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    draft = c.ingest.outbound["order_created:gid://shopify/Order/img2"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["body_params"]["product_name"] == "Chic Kurta Set"
    assert "image_url" not in params


async def test_webhook_does_not_send_inline_when_send_mode_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The inline send (ADR-001 amendment) honours the send_mode kill switch exactly like the
    # drain: under the default `off` mode it claims nothing and sends nothing, leaving the row
    # `queued` for the backstop drain. We patch `send_template` at its SOURCE module and
    # `send_one_outbound` at its definition site so the guard holds regardless of import path.
    import app.channels.whatsapp_sender as whatsapp_sender
    import app.jobs.outbox_drain as drain

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("send_mode off must not send inline")

    monkeypatch.setattr(whatsapp_sender, "send_template", _boom)
    monkeypatch.setattr(drain, "send_one_outbound", _boom)

    body = json.dumps(payload("gid://shopify/Order/nosend")).encode()
    resp = await post(body, headers(body, webhook_id="wh-nosend"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    store = get_container().ingest
    (row,) = await store.claim_queued_outbound()  # still queued (pre-claim), nothing sent it
    assert row.dedupe_key == "order_created:gid://shopify/Order/nosend"


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
    resp = await post(body, headers(body, topic="products/create"))
    assert resp.json() == {"ok": True, "ignored": True}


def test_handled_topics_stay_in_sync_with_the_subscribed_topics() -> None:
    """One declaration of the three topic names, not two that can drift apart.

    A topic subscribed in `REQUIRED_TOPICS` but missing from the handler's set is a delivery
    Shopify sends and we silently drop; the reverse is code that can never run. The expected
    set is spelled out literally -- recomputing the implementation's own expression here would
    pass no matter how wrong that expression is.
    """
    from app.channels.shopify_webhook import HANDLED_TOPICS
    from app.shopify.subscriptions import REQUIRED_TOPICS

    assert HANDLED_TOPICS == {
        "orders/create",
        "orders/updated",
        "customers/update",
        "fulfillments/create",
        "fulfillments/update",
    }
    assert len(HANDLED_TOPICS) == len(REQUIRED_TOPICS)


def test_topic_header_name_splits_only_the_resource_prefix() -> None:
    # Shopify's header form replaces ONLY the first underscore: the GraphQL enum
    # ORDERS_PARTIALLY_FULFILLED is delivered as `orders/partially_fulfilled`.
    from app.channels.shopify_webhook import _topic_header_name

    assert _topic_header_name("ORDERS_CREATE") == "orders/create"
    assert _topic_header_name("CUSTOMERS_UPDATE") == "customers/update"
    assert _topic_header_name("ORDERS_PARTIALLY_FULFILLED") == "orders/partially_fulfilled"


async def test_orders_updated_populates_the_mirror() -> None:
    p = payload("gid://shopify/Order/mirror1")
    p["fulfillment_status"] = "fulfilled"
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, topic="orders/updated", webhook_id="wh-mirror1"))
    assert resp.status_code == 200
    store = get_container().ingest
    assert store.orders["gid://shopify/Order/mirror1"].fulfillment_status == "fulfilled"  # type: ignore[attr-defined]
    # orders/updated must NOT run any of the orders/create-only work: a mapping or a queued
    # outbound here would re-send the order-confirmation template to a real customer.
    assert not store.mappings  # type: ignore[attr-defined]
    assert not store.outbound  # type: ignore[attr-defined]


async def test_a_newly_subscribed_topic_does_not_fall_through_to_orders_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a topic to REQUIRED_TOPICS must never silently reuse the orders/create path.

    HANDLED_TOPICS is derived from the subscribed topics, so a future addition passes the gate
    automatically; without an explicit orders/create check it would then run the mapping/outbox
    logic and could queue a duplicate order-confirmation template for an unrelated event.
    """
    import app.channels.shopify_webhook as webhook_module

    monkeypatch.setattr(
        webhook_module,
        "HANDLED_TOPICS",
        frozenset({*webhook_module.HANDLED_TOPICS, "orders/fulfilled"}),
    )
    body = json.dumps(payload("gid://shopify/Order/future")).encode()
    resp = await post(body, headers(body, topic="orders/fulfilled", webhook_id="wh-future"))
    assert resp.json() == {"ok": True, "ignored": True}
    store = get_container().ingest
    assert not store.mappings  # type: ignore[attr-defined]
    assert not store.outbound  # type: ignore[attr-defined]
    assert store.orders == {}  # type: ignore[attr-defined]


def fulfillment_payload(
    order_gid: str = "gid://shopify/Order/1",
    fulfillment_gid: str = "gid://shopify/Fulfillment/1",
) -> dict:
    order_id = order_gid.rsplit("/", 1)[-1]
    return {
        "id": int(order_id) * 10 + 1,
        "admin_graphql_api_id": fulfillment_gid,
        "order_id": int(order_id),
        "status": "success",
        "tracking_company": "Delhivery",
        "tracking_number": "AWB0099887766",
        "tracking_url": "https://www.delhivery.com/track/AWB0099887766",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.parametrize("topic", ["fulfillments/create", "fulfillments/update"])
async def test_fulfillments_topic_populates_the_mirror(topic: str) -> None:
    # The order must be mirrored first (the FK / in-memory parity skips a fulfillment for an
    # unknown order) -- an order is always created before it is fulfilled.
    order_gid = f"gid://shopify/Order/{100 + hash(topic) % 100}"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id=f"wh-o-{topic}"))

    fbody = json.dumps(fulfillment_payload(order_gid)).encode()
    resp = await post(fbody, headers(fbody, topic=topic, webhook_id=f"wh-f-{topic}"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": False}
    order = await get_container().ingest.get_mirrored_order(order_gid)
    assert order is not None
    assert len(order.fulfillments) == 1
    assert order.fulfillments[0].tracking_number == "AWB0099887766"
    assert order.fulfillments[0].tracking_company == "Delhivery"
    # The fulfillment mirror write itself never re-sends the ORIGINAL confirmation template -- but
    # it DOES now queue its own order_shipped notification (this feature), since the default
    # fulfillment_payload() already carries tracking info.
    store = get_container().ingest
    fulfillment_gid = fulfillment_payload(order_gid).get("admin_graphql_api_id")
    assert set(store.outbound) == {  # type: ignore[attr-defined]
        f"order_created:{order_gid}", f"fulfillment_shipped:{fulfillment_gid}",
    }


async def test_fulfillment_with_tracking_sends_order_shipped() -> None:
    order_gid = "gid://shopify/Order/500"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-500"))

    fulfillment_gid = "gid://shopify/Fulfillment/500"
    fbody = json.dumps(
        fulfillment_payload(order_gid, fulfillment_gid)
    ).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-500"))

    assert resp.status_code == 200
    store = get_container().ingest
    assert f"fulfillment_shipped:{fulfillment_gid}" in store.outbound  # type: ignore[attr-defined]
    draft = store.outbound[f"fulfillment_shipped:{fulfillment_gid}"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["template"] == "order_shipped"
    assert params["language"] == "en"
    assert params["body_params"] == [
        "Suman B", "tavas3733", "Delhivery", "https://www.delhivery.com/track/AWB0099887766",
    ]
    assert "buttons" not in params
    # No delivered notification -- shipment_status is absent from this payload.
    assert f"fulfillment_delivered:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]
    # And no pending-delivery-confirmation row either -- nothing was "delivered".
    due = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(days=1))
    assert not [r for r in due if r.fulfillment_gid == fulfillment_gid]


async def test_fulfillment_replay_does_not_re_enqueue_shipped() -> None:
    # A fulfillments/update replay with tracking ALREADY present must not queue a second
    # order_shipped -- the dedupe key (fulfillment_gid-keyed) is the entire guarantee.
    order_gid = "gid://shopify/Order/501"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-501"))

    fulfillment_gid = "gid://shopify/Fulfillment/501"
    fbody = json.dumps(fulfillment_payload(order_gid, fulfillment_gid)).encode()
    await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-501a"))
    await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-501b"))

    store = get_container().ingest
    shipped_rows = [
        k for k in store.outbound  # type: ignore[attr-defined]
        if k == f"fulfillment_shipped:{fulfillment_gid}"
    ]
    assert len(shipped_rows) == 1


async def test_fulfillment_delivered_records_pending_confirmation_instead_of_sending() -> None:
    # RTO-aware change: a "delivered" shipment_status must NOT send order_delivered immediately
    # (Delhivery stamps the SAME status on an RTO's final "delivered back to origin" scan). Instead
    # it parks a pending-delivery-confirmation row for the sweep job to re-check ~2h later.
    order_gid = "gid://shopify/Order/502"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-502"))

    fulfillment_gid = "gid://shopify/Fulfillment/502"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "delivered"
    fbody = json.dumps(fpayload).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-502"))

    assert resp.status_code == 200
    store = get_container().ingest
    # No order_delivered send happens here any more.
    assert f"fulfillment_delivered:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]
    # A single pending-confirmation row is parked, routed to the resolved (mapping) phone, with a
    # due_at ~2h out (comfortably inside a 3h window).
    due = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(hours=3))
    rows = [r for r in due if r.fulfillment_gid == fulfillment_gid]
    assert len(rows) == 1
    assert rows[0].order_gid == order_gid
    assert rows[0].phone_e164 == "+919664290413"
    assert rows[0].state == "pending"
    # It is genuinely deferred: nothing is due within the next few minutes.
    soon = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(minutes=5))
    assert not [r for r in soon if r.fulfillment_gid == fulfillment_gid]
    # This payload ALSO has tracking info (fulfillment_payload's defaults) -> shipped still fires.
    assert f"fulfillment_shipped:{fulfillment_gid}" in store.outbound  # type: ignore[attr-defined]


async def test_fulfillment_delivered_twice_records_one_pending_row() -> None:
    # Shopify may replay a delivered webhook; the pending row is idempotent on fulfillment_gid, so
    # the second delivery must NOT create a second row nor reset the first row's due_at.
    order_gid = "gid://shopify/Order/5022"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-5022"))

    fulfillment_gid = "gid://shopify/Fulfillment/5022"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "delivered"
    fbody = json.dumps(fpayload).encode()
    await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-5022-1"))
    store = get_container().ingest
    first = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(hours=3))
    first_row = next(r for r in first if r.fulfillment_gid == fulfillment_gid)
    first_due = first_row.due_at

    await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-5022-2"))
    again = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(hours=3))
    rows = [r for r in again if r.fulfillment_gid == fulfillment_gid]
    assert len(rows) == 1
    assert rows[0].due_at == first_due


async def test_fulfillment_delivered_no_mirrored_order_records_no_pending_row() -> None:
    # A "delivered" for an order this bot never saw orders/create for -- no mirror row, so no
    # pending confirmation is parked and the webhook still acks 200 (never a 500).
    order_gid = "gid://shopify/Order/5023"  # deliberately never posted via orders/create
    fulfillment_gid = "gid://shopify/Fulfillment/5023"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "delivered"
    fbody = json.dumps(fpayload).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-5023"))

    assert resp.status_code == 200
    store = get_container().ingest
    due = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(days=1))
    assert not [r for r in due if r.fulfillment_gid == fulfillment_gid]


async def test_fulfillment_non_delivered_shipment_status_does_not_send_delivered() -> None:
    order_gid = "gid://shopify/Order/503"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-503"))

    fulfillment_gid = "gid://shopify/Fulfillment/503"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "in_transit"
    fbody = json.dumps(fpayload).encode()
    await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-503"))

    store = get_container().ingest
    assert f"fulfillment_delivered:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]
    # A non-"delivered" status also parks no pending-delivery-confirmation row.
    due = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(days=1))
    assert not [r for r in due if r.fulfillment_gid == fulfillment_gid]


async def test_fulfillment_no_mirrored_order_skips_notification() -> None:
    # A fulfillment for an order this bot never saw orders/create for -- no mirror row, no
    # notification (never a 500; the webhook still acks 200 normally).
    order_gid = "gid://shopify/Order/999999"  # deliberately never posted via orders/create
    fulfillment_gid = "gid://shopify/Fulfillment/999999"
    fbody = json.dumps(fulfillment_payload(order_gid, fulfillment_gid)).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-999999"))

    assert resp.status_code == 200
    store = get_container().ingest
    assert f"fulfillment_shipped:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]


async def test_split_shipment_fulfillments_get_independent_notifications() -> None:
    order_gid = "gid://shopify/Order/504"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-504"))

    gid_a = "gid://shopify/Fulfillment/504a"
    gid_b = "gid://shopify/Fulfillment/504b"
    fbody_a = json.dumps(fulfillment_payload(order_gid, gid_a)).encode()
    fbody_b = json.dumps(fulfillment_payload(order_gid, gid_b)).encode()
    await post(fbody_a, headers(fbody_a, topic="fulfillments/create", webhook_id="wh-f-504a"))
    await post(fbody_b, headers(fbody_b, topic="fulfillments/create", webhook_id="wh-f-504b"))

    store = get_container().ingest
    assert f"fulfillment_shipped:{gid_a}" in store.outbound  # type: ignore[attr-defined]
    assert f"fulfillment_shipped:{gid_b}" in store.outbound  # type: ignore[attr-defined]


async def test_fulfillment_notification_failure_still_acks_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A DB error while resolving the order / enqueuing the notification must degrade to
    # "notification not sent, logged" -- never a 500 on a signed delivery (which would burn
    # Shopify's 19-failure retry budget and can delete the subscription).
    order_gid = "gid://shopify/Order/505"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-505"))

    store = get_container().ingest

    async def boom(gid: object) -> object:
        raise RuntimeError("mirror read unavailable")

    monkeypatch.setattr(store, "get_mirrored_order", boom)
    fulfillment_gid = "gid://shopify/Fulfillment/505"
    fbody = json.dumps(fulfillment_payload(order_gid, fulfillment_gid)).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-505"))

    assert resp.status_code == 200
    assert f"fulfillment_shipped:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]


async def test_fulfillment_notification_routes_to_mapping_phone_not_shipping_phone() -> None:
    # Regression guard (mirrors the reconcile cod_cancel fix): the notification must go to the SAME
    # number the original confirmation did -- the mapping phone, which ranks the customer's own
    # phone ABOVE the shipping-address phone. Order.best_phone() only knows phone/shipping/billing
    # (never customer.phone), so a gift/relative order (customer phone != shipping phone) would
    # otherwise silently message a stranger.
    order_gid = "gid://shopify/Order/507"
    p = payload(order_gid)
    p["customer"]["phone"] = "+919812345678"  # the customer's own number (held the conversation)
    p["shipping_address"] = {"phone": "+919000000000"}  # a different recipient/reception number
    body = json.dumps(p).encode()
    await post(body, headers(body, webhook_id="wh-o-507"))

    fulfillment_gid = "gid://shopify/Fulfillment/507"
    fbody = json.dumps(fulfillment_payload(order_gid, fulfillment_gid)).encode()
    await post(fbody, headers(fbody, topic="fulfillments/create", webhook_id="wh-f-507"))

    store = get_container().ingest
    draft = store.outbound[f"fulfillment_shipped:{fulfillment_gid}"]  # type: ignore[attr-defined]
    assert draft.phone_e164 == "+919812345678"


async def test_shipped_notification_failure_does_not_block_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Per-notification isolation: when BOTH shipped and delivered are newly true in one event (a
    # delivered payload still carries tracking), a failure enqueuing the shipped notification must
    # NOT prevent the delivered side from being handled -- otherwise the pending-delivery-
    # confirmation row is never parked and the delivered flow is lost until a future webhook
    # re-triggers it. (Delivered no longer sends; it records a pending confirmation for the sweep.)
    order_gid = "gid://shopify/Order/508"
    body = json.dumps(payload(order_gid)).encode()
    await post(body, headers(body, webhook_id="wh-o-508"))

    store = get_container().ingest
    original_enqueue = store.enqueue_outbound

    async def selective(draft: object) -> object:
        if draft.dedupe_key.startswith("fulfillment_shipped:"):  # type: ignore[attr-defined]
            raise RuntimeError("shipped enqueue unavailable")
        return await original_enqueue(draft)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "enqueue_outbound", selective)

    fulfillment_gid = "gid://shopify/Fulfillment/508"
    fpayload = fulfillment_payload(order_gid, fulfillment_gid)
    fpayload["shipment_status"] = "delivered"  # both is_shipped AND is_delivered true this event
    fbody = json.dumps(fpayload).encode()
    resp = await post(fbody, headers(fbody, topic="fulfillments/update", webhook_id="wh-f-508"))

    assert resp.status_code == 200
    # Shipped failed, but the delivered side was still handled and parked a pending confirmation.
    assert f"fulfillment_shipped:{fulfillment_gid}" not in store.outbound  # type: ignore[attr-defined]
    due = await store.due_delivery_confirmations(datetime.now(UTC) + timedelta(hours=3))
    assert [r for r in due if r.fulfillment_gid == fulfillment_gid]


async def test_fulfillments_malformed_payload_ignored() -> None:
    # No order_id -> unlinkable -> ignored, never a 500 on a signed delivery.
    body = json.dumps({"admin_graphql_api_id": "gid://shopify/Fulfillment/x"}).encode()
    resp = await post(body, headers(body, topic="fulfillments/create", webhook_id="wh-f-bad"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_fulfillments_still_acks_200_when_the_mirror_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_container().ingest

    async def boom(order_gid: object, fulfillment: object) -> None:
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(store, "upsert_fulfillment", boom)
    body = json.dumps(fulfillment_payload()).encode()
    resp = await post(body, headers(body, topic="fulfillments/create", webhook_id="wh-f-down"))
    # A 500 here would burn Shopify's 19-failure retry budget; degrade to "logged and acked".
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_orders_updated_malformed_payload_ignored() -> None:
    body = json.dumps({"admin_graphql_api_id": "gid://shopify/Order/nameless"}).encode()
    resp = await post(body, headers(body, topic="orders/updated", webhook_id="wh-upd-bad"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}
    assert get_container().ingest.orders == {}  # type: ignore[attr-defined]


async def test_orders_updated_still_acks_200_when_the_mirror_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_container().ingest

    async def boom(order: object) -> None:
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(store, "upsert_order_mirror", boom)
    body = json.dumps(payload("gid://shopify/Order/mirror-down")).encode()
    resp = await post(body, headers(body, topic="orders/updated", webhook_id="wh-upd-down"))
    # A 500 here would make Shopify retry against a still-broken mirror and burn its
    # 19-failure budget before deleting the subscription; degrade to "logged and acked".
    assert resp.status_code == 200


async def test_orders_create_also_populates_the_mirror() -> None:
    body = json.dumps(payload("gid://shopify/Order/mirror2")).encode()
    resp = await post(body, headers(body, webhook_id="wh-mirror2"))
    assert resp.status_code == 200
    store = get_container().ingest
    assert "gid://shopify/Order/mirror2" in store.orders  # type: ignore[attr-defined]
    # The existing mapping/push-eligibility behavior for orders/create is unaffected:
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}


async def test_orders_create_writes_the_mapping_before_the_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer-visible write (mapping + outbox) must not be blocked by the mirror.

    The mirror has no live reader yet; the outbox row IS the confirmation message, so it goes
    first and the mirror is best-effort afterwards.
    """
    store = get_container().ingest
    calls: list[str] = []
    real_ingest = store.ingest_order_created
    real_mirror = store.upsert_order_mirror

    async def spy_ingest(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append("ingest")
        return await real_ingest(*args, **kwargs)  # type: ignore[arg-type]

    async def spy_mirror(order: object) -> None:
        calls.append("mirror")
        await real_mirror(order)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "ingest_order_created", spy_ingest)
    monkeypatch.setattr(store, "upsert_order_mirror", spy_mirror)
    body = json.dumps(payload("gid://shopify/Order/mirror-order")).encode()
    resp = await post(body, headers(body, webhook_id="wh-mirror-order"))
    assert resp.status_code == 200
    assert calls == ["ingest", "mirror"]


async def test_orders_create_still_maps_and_queues_when_the_mirror_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_container().ingest

    async def boom(order: object) -> None:
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(store, "upsert_order_mirror", boom)
    body = json.dumps(payload("gid://shopify/Order/mirror3")).encode()
    resp = await post(body, headers(body, webhook_id="wh-mirror3"))
    # A mirror failure must never cost the customer their confirmation message.
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    assert "gid://shopify/Order/mirror3" in store.mappings  # type: ignore[attr-defined]
    assert "order_created:gid://shopify/Order/mirror3" in store.outbound  # type: ignore[attr-defined]


def customer_payload(gid: str = "gid://shopify/Customer/555", city: str = "Pune") -> dict:
    return {
        "id": 555, "admin_graphql_api_id": gid,
        "first_name": "Anita", "last_name": "Rao", "email": "a@example.com",
        "phone": "+919888888888", "default_address": {"city": city},
    }


async def test_customers_update_refreshes_a_known_customer_only() -> None:
    store = get_container().ingest
    await store.upsert_customer(  # the bot only knows customers whose order it mirrored
        Customer(
            gid="gid://shopify/Customer/555", first_name="Anita", last_name="Rao",
            email="a@example.com", phone="+919888888888", address_line1=None,
            address_line2=None, city="Mumbai", state=None, postal_code=None, country=None,
        )
    )
    body = json.dumps(customer_payload()).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-cust1"))
    assert resp.status_code == 200
    assert store.customers["gid://shopify/Customer/555"].city == "Pune"  # type: ignore[attr-defined]
    assert store.orders == {}  # type: ignore[attr-defined]
    assert not store.mappings  # type: ignore[attr-defined]  # no order-mapping side effect


async def test_customers_update_does_not_create_an_unknown_customer() -> None:
    """Owner decision 2026-08-11: mirror only customers the bot already knows.

    Shopify sends customers/update for every customer in the store, including people who have
    never ordered or messaged the bot -- mirroring those would import personal data we have no
    reason to hold.
    """
    body = json.dumps(customer_payload("gid://shopify/Customer/stranger")).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-cust-new"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}
    assert get_container().ingest.customers == {}  # type: ignore[attr-defined]


async def test_customers_update_malformed_payload_ignored() -> None:
    body = json.dumps({"first_name": "no id"}).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-cust2"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}
    assert get_container().ingest.customers == {}  # type: ignore[attr-defined]


async def test_customers_update_still_acks_200_when_the_mirror_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_container().ingest

    async def boom(gid: str) -> bool:
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(store, "customer_exists", boom)
    body = json.dumps(customer_payload()).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-cust-down"))
    assert resp.status_code == 200


async def test_garbage_body_with_valid_hmac_ignored() -> None:
    body = b"not-json"
    resp = await post(body, headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


@pytest.mark.parametrize("topic", ["orders/create", "orders/updated", "customers/update"])
async def test_type_confused_signed_payload_does_not_500(topic: str) -> None:
    # Every handled topic parses attacker-typed JSON OUTSIDE the try/except that wraps the
    # mirror write, so each one needs its own proof that a poison payload cannot 500 a signed
    # delivery (a 500 burns Shopify's 19-failure budget before it deletes the subscription).
    p = {
        "admin_graphql_api_id": "gid://shopify/Order/poison",
        "name": "tavasX",
        "phone": 919664290413,
        "customer": "pwn",
        "payment_gateway_names": 5,
        "customer_locale": 5,
        "email": {"x": 1},
    }
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, topic=topic, webhook_id=f"wh-poison-{topic}"))
    assert resp.status_code == 200


async def test_deeply_nested_json_with_valid_hmac_ignored() -> None:
    body = ("[" * 3000 + "]" * 3000).encode()
    resp = await post(body, headers(body, webhook_id="wh-nested"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_corrupt_staleness_config_does_not_500_uses_default() -> None:
    # push_staleness_hours is operator/attacker-typed config. A non-numeric stored value must
    # NOT 500 the signed webhook — a 500 here burns Shopify's 19-failure retry budget before
    # the subscription is deleted. The handler degrades to the default staleness window.
    await get_container().config.set_plain("push_staleness_hours", "abc")
    body = json.dumps(payload("gid://shopify/Order/staleness")).encode()
    resp = await post(body, headers(body, webhook_id="wh-staleness"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}


def test_staleness_hardening_falls_back_to_default() -> None:
    from app.channels.shopify_webhook import DEFAULT_STALENESS_HOURS, _staleness_hours

    # (1) Unicode digits: float("٣٠") == 30.0 — a bare float() guard accepts a corrupt value.
    # (2) Non-finite: float("nan"/"inf"/"Infinity"/"1e400") NEVER raises, and a nan/inf makes
    #     `age > staleness*3600` always False -> the staleness guard is fully DISABLED, so every
    #     historical order looks fresh and push-eligible again (mass unwanted template re-sends).
    # (3) In-range-parseable but out of the field's Field(ge=1, le=168) bound -> also unsafe.
    # All must degrade to the safe default, none may disable the guard.
    for bad in ("٣٠", "nan", "inf", "Infinity", "1e400", "9999", "0", "-5", ""):
        assert _staleness_hours(bad) == DEFAULT_STALENESS_HOURS, bad
    # A genuine in-bound integer value is honored.
    assert _staleness_hours("12") == 12.0


async def test_oversized_body_413_and_nothing_ingested() -> None:
    body = b"x" * (1_048_576 + 1)
    resp = await post(
        body,
        {"X-Shopify-Topic": "orders/create", "X-Shopify-Webhook-Id": "wh-big"},
    )
    assert resp.status_code == 413
    assert not get_container().ingest.webhooks  # type: ignore[attr-defined]


async def test_unset_secret_fails_closed_403() -> None:
    # Ops-error path: the client secret was never configured (fresh, unseeded container ->
    # get_secret returns None -> the `not secret` short-circuit). A validly-signed request
    # still gets 403 — intentionally indistinguishable from a bad-signature attack; giving
    # ops visibility into "misconfigured vs attacked" is deferred to the F13 observability work.
    reset_container()
    c = get_container()  # rebuilt WITHOUT seeding shopify:client_secret
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 403
    assert not c.ingest.webhooks  # type: ignore[attr-defined]


async def test_corrupt_secret_fails_closed_403() -> None:
    await get_container().config_repo.set("shopify:client_secret", "gAAAAAcorrupt")
    body = json.dumps(payload()).encode()
    resp = await post(body, headers(body))
    assert resp.status_code == 403


async def test_webhook_signing_secret_also_verifies() -> None:
    # Transition period: a webhook the owner creates in Admin -> Settings -> Notifications is
    # signed with the per-store webhook signing secret, NOT the app's client_secret. Once that
    # secret is configured, such a delivery must verify.
    await get_container().config.set_secret(
        "shopify:webhook_signing_secret", WEBHOOK_SIGNING_SECRET
    )
    body = json.dumps(payload("gid://shopify/Order/adminwh")).encode()
    hdrs = headers(body, webhook_id="wh-admin")
    hdrs["X-Shopify-Hmac-Sha256"] = sign_with(WEBHOOK_SIGNING_SECRET, body)
    resp = await post(body, hdrs)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}


async def test_client_secret_still_verifies_when_signing_secret_also_configured() -> None:
    # Regression: the currently-live app-created path (signed with client_secret) must keep
    # verifying even after the second secret is added — both delivery sources coexist.
    await get_container().config.set_secret(
        "shopify:webhook_signing_secret", WEBHOOK_SIGNING_SECRET
    )
    body = json.dumps(payload("gid://shopify/Order/appwh")).encode()
    resp = await post(body, headers(body, webhook_id="wh-app"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}


async def test_neither_secret_verifies_rejected_403() -> None:
    # A garbage signature is still rejected even with both secrets configured.
    await get_container().config.set_secret(
        "shopify:webhook_signing_secret", WEBHOOK_SIGNING_SECRET
    )
    body = json.dumps(payload()).encode()
    hdrs = headers(body)
    hdrs["X-Shopify-Hmac-Sha256"] = sign_with("some-other-secret", body)
    resp = await post(body, hdrs)
    assert resp.status_code == 403
    assert not get_container().ingest.webhooks  # type: ignore[attr-defined]


async def test_signing_secret_signed_delivery_rejected_when_unconfigured() -> None:
    # The second secret is only honored once configured. With it unset (autouse fixture seeds
    # only client_secret), a delivery signed with it must NOT verify — behaves exactly as today.
    body = json.dumps(payload()).encode()
    hdrs = headers(body)
    hdrs["X-Shopify-Hmac-Sha256"] = sign_with(WEBHOOK_SIGNING_SECRET, body)
    resp = await post(body, hdrs)
    assert resp.status_code == 403


async def test_corrupt_signing_secret_does_not_break_client_secret_path() -> None:
    # A VaultError reading the SECONDARY secret must fail that check closed — never crash the
    # request nor block a valid client_secret-signed (app-created) delivery.
    await get_container().config_repo.set("shopify:webhook_signing_secret", "gAAAAAcorrupt")
    body = json.dumps(payload("gid://shopify/Order/appok")).encode()
    resp = await post(body, headers(body, webhook_id="wh-appok"))
    assert resp.status_code == 200


async def test_corrupt_client_secret_falls_through_to_signing_secret() -> None:
    # A VaultError reading the PRIMARY secret fails that check closed but must NOT reject the
    # request when the secondary (Admin-created) signing secret validates it.
    await get_container().config_repo.set("shopify:client_secret", "gAAAAAcorrupt")
    await get_container().config.set_secret(
        "shopify:webhook_signing_secret", WEBHOOK_SIGNING_SECRET
    )
    body = json.dumps(payload("gid://shopify/Order/adminok")).encode()
    hdrs = headers(body, webhook_id="wh-adminok")
    hdrs["X-Shopify-Hmac-Sha256"] = sign_with(WEBHOOK_SIGNING_SECRET, body)
    resp = await post(body, hdrs)
    assert resp.status_code == 200


async def test_foreign_shop_domain_403_and_nothing_ingested() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(
        body, {**headers(body), "X-Shopify-Shop-Domain": "evil.myshopify.com"}
    )
    assert resp.status_code == 403
    assert not get_container().ingest.webhooks  # type: ignore[attr-defined]


async def test_matching_shop_domain_allowed() -> None:
    body = json.dumps(payload()).encode()
    resp = await post(
        body, {**headers(body), "X-Shopify-Shop-Domain": "thetavas.myshopify.com"}
    )
    assert resp.status_code == 200


async def test_oversized_field_is_clipped_to_256() -> None:
    p = payload("gid://shopify/Order/longname")
    p["name"] = "n" * 200_000
    body = json.dumps(p).encode()
    resp = await post(body, headers(body, webhook_id="wh-long"))
    assert resp.status_code == 200
    stored = get_container().ingest.mappings["gid://shopify/Order/longname"]  # type: ignore[attr-defined]
    assert len(stored.order_name) == 256


# --- Inline order-confirmation send (ADR-001 amendment, 2026-08-15) ---------------------------
#
# The webhook now sends the confirmation INLINE (a genuine await before the ack), bounded by a
# short timeout, claiming ONLY the row it just queued. These tests drive the real ASGI handler
# and assert the send happened within the SAME request/response cycle (the fake sender records
# its calls during the POST), that the ack is always 200 even when the send fails/raises, and
# that the send_mode kill switch and single-row isolation both hold.


class _RecordingSender:
    """Records send_template calls made during a request; returns/raises a scripted result."""

    def __init__(self, result: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ):
        self.calls.append(
            {"to": to, "template": template_name, "button_payloads": list(button_payloads),
             "header_image_url": header_image_url, "timeout": timeout}
        )
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


async def _enable_live_sending() -> None:
    from app.admin.controls import AdminControls, save_controls

    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", "sec")
    await c.config.set_secret("whatsapp:verify_token", "ver")
    await c.config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    await save_controls(c.config, AdminControls(send_mode="live"))


async def test_inline_send_fires_within_the_webhook_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.whatsapp_sender import SendResult

    await _enable_live_sending()
    sender = _RecordingSender(SendResult(ok=True, status_code=200, wamid="wamid.1", error=None))
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)

    gid = "gid://shopify/Order/inline1"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-inline1"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    # The send happened inline (recorded during the handler), with the confirm/cancel buttons and
    # the SHORT inline timeout (distinct from the cron 20s default).
    assert len(sender.calls) == 1
    assert sender.calls[0]["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    assert sender.calls[0]["timeout"] == 3.0
    # The row was marked sent by the inline path -> nothing left for the backstop drain.
    store = get_container().ingest
    assert await store.claim_queued_outbound() == []
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "sent"


async def test_inline_send_failure_still_acks_200(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.channels.whatsapp_sender import WhatsAppSendError

    await _enable_live_sending()
    # A transport error inside the sender must not turn the ack into a 500.
    sender = _RecordingSender(WhatsAppSendError("network down"))
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)

    gid = "gid://shopify/Order/inlinefail"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-inlinefail"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    assert len(sender.calls) == 1
    # The transport error bumped the row back to 'queued' for the backstop drain to retry.
    store = get_container().ingest
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "queued"


async def test_inline_send_unexpected_error_never_500s(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even an UNEXPECTED error deeper in the inline path (not a WhatsAppSendError) must never
    # propagate out and 500 the signed delivery — send_inline_outbound's outer guard swallows it.
    await _enable_live_sending()

    async def _boom(*_args: object, **_kwargs: object):
        raise RuntimeError("unexpected")

    # claim_outbound_by_id lives on the ingest store; make it raise an unexpected error mid-path.
    store = get_container().ingest
    monkeypatch.setattr(store, "claim_outbound_by_id", _boom)

    gid = "gid://shopify/Order/inlineboom"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-inlineboom"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}


async def test_inline_send_suppressed_in_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin.controls import AdminControls, save_controls
    from app.channels.whatsapp_sender import SendResult

    await _enable_live_sending()
    await save_controls(get_container().config, AdminControls(send_mode="shadow"))
    sender = _RecordingSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)

    gid = "gid://shopify/Order/shadow"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-shadow"))

    assert resp.status_code == 200
    assert sender.calls == []  # shadow -> zero real Meta calls
    store = get_container().ingest
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "suppressed"


async def test_inline_send_touches_only_its_own_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two orders arriving one after another: each webhook's inline send affects ONLY the row it
    # created (no shared-claim interaction). We pre-seed an UNRELATED queued row and prove the
    # first order's inline send never touches it, then the second order sends its own row.
    from app.channels.whatsapp_sender import SendResult
    from app.store.base import MappingUpsert, OutboundDraft

    await _enable_live_sending()
    sender = _RecordingSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)

    store = get_container().ingest
    # An unrelated queued row that belongs to the backstop drain, not the inline path.
    other_gid = "gid://shopify/Order/other"
    await store.ingest_order_created(
        "wh-other", "orders/create",
        MappingUpsert(
            order_gid=other_gid, order_name="tavasX", order_number_int=9,
            phone_e164="+911111111111", customer_name="X", email=None, language="en",
            financial_status_at_create="PENDING", is_cod=True,
        ),
        OutboundDraft(
            dedupe_key=f"order_created:{other_gid}", kind="order_confirmation",
            phone_e164="+911111111111", payload_json="{}",
        ),
    )

    gid_a = "gid://shopify/Order/isoA"
    body_a = json.dumps(payload(gid_a)).encode()
    resp_a = await post(body_a, headers(body_a, webhook_id="wh-isoA"))
    assert resp_a.status_code == 200

    # Only order A's row was sent; the unrelated row is still queued (untouched by the inline path).
    assert [call["button_payloads"][0] for call in sender.calls] == [f"order:confirm:{gid_a}"]
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid_a}"].state == "sent"
    assert views[f"order_created:{other_gid}"].state == "queued"

    gid_b = "gid://shopify/Order/isoB"
    body_b = json.dumps(payload(gid_b)).encode()
    resp_b = await post(body_b, headers(body_b, webhook_id="wh-isoB"))
    assert resp_b.status_code == 200
    # Order B sent its OWN row; still nothing touched the unrelated backstop row.
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid_b}"].state == "sent"
    assert views[f"order_created:{other_gid}"].state == "queued"


# --- Owner-requested raw-payload debug logging (2026-08-15) -----------------------------------
#
# The handler logs a "SHOPIFY WEBHOOK RECEIVED" line + the full parsed payload JSON AFTER HMAC
# verify + shop-domain check + json.loads succeed, before any topic branching. This is an
# owner-approved, deliberate exception to the no-PII-logging rule (the raw payload includes
# customer name/phone/email/address); see error_learnings.md. The security-relevant property is
# that the log sits AFTER verification, so a forged/unsigned probe never gets its body logged.
#
# The logs run inside the ASGI handler in-process, so caplog captures them during the POST.


async def test_debug_log_fires_for_a_valid_signed_webhook(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.channels.shopify_webhook")
    gid = "gid://shopify/Order/logvalid"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-logvalid"))

    assert resp.status_code == 200
    text = caplog.text
    # The receipt line carries the topic, shop (None header here), and webhook id.
    assert "SHOPIFY WEBHOOK RECEIVED" in text
    assert "topic=orders/create" in text
    assert "webhook_id=wh-logvalid" in text
    # The full parsed payload JSON is logged (incl. the customer phone, deliberately).
    assert "ORDER JSON" in text
    assert gid in text
    assert "+919664290413" in text


async def test_debug_log_fires_uniformly_for_a_non_order_topic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The log sits BEFORE topic branching, so every handled topic (not just orders/create) is
    # visible — here customers/update, which never reaches the orders/create code path.
    caplog.set_level(logging.INFO, logger="app.channels.shopify_webhook")
    body = json.dumps(customer_payload()).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-logcust"))

    assert resp.status_code == 200
    text = caplog.text
    assert "SHOPIFY WEBHOOK RECEIVED" in text
    assert "topic=customers/update" in text
    assert "webhook_id=wh-logcust" in text
    assert "ORDER JSON" in text


async def test_debug_log_does_not_fire_for_an_invalid_signature(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # SECURITY: the log must sit AFTER HMAC verification. A forged/garbage-signature probe is
    # rejected 403 and must NEVER have its payload logged — otherwise the debug log becomes a
    # pre-auth information source that echoes attacker-supplied bodies as if legitimate.
    caplog.set_level(logging.INFO, logger="app.channels.shopify_webhook")
    body = json.dumps(payload("gid://shopify/Order/forged")).encode()
    resp = await post(body, {**headers(body), "X-Shopify-Hmac-Sha256": "AAAA"})

    assert resp.status_code == 403
    assert "SHOPIFY WEBHOOK RECEIVED" not in caplog.text
    assert "ORDER JSON" not in caplog.text
    assert "gid://shopify/Order/forged" not in caplog.text


async def test_debug_log_does_not_fire_for_a_foreign_shop_domain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The log also sits AFTER the shop-domain check: a delivery signed with our secret but bearing
    # a foreign shop domain is rejected 403 and must not be logged either.
    caplog.set_level(logging.INFO, logger="app.channels.shopify_webhook")
    body = json.dumps(payload("gid://shopify/Order/foreign")).encode()
    resp = await post(body, {**headers(body), "X-Shopify-Shop-Domain": "evil.myshopify.com"})

    assert resp.status_code == 403
    assert "SHOPIFY WEBHOOK RECEIVED" not in caplog.text
    assert "gid://shopify/Order/foreign" not in caplog.text


async def test_debug_log_json_is_compact_not_pretty_printed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A validly-signed but deeply-nested payload (well within MAX_WEBHOOK_BODY_BYTES) must NOT be
    # pretty-printed: json.dumps(payload, indent=2) amplifies the rendered size ~quadratically in
    # nesting depth (the reviewer measured 1MB -> ~5GB), and it is built eagerly on EVERY delivery
    # regardless of log level. Compact dumps keeps it at ~1x. The log must still carry the COMPLETE
    # raw payload -- only the indentation is gone.
    caplog.set_level(logging.INFO, logger="app.channels.shopify_webhook")
    nested: object = "x"
    for _ in range(150):
        nested = {"k": nested}
    body = json.dumps({**customer_payload(), "deep": nested}).encode()
    resp = await post(body, headers(body, topic="customers/update", webhook_id="wh-nested"))

    assert resp.status_code == 200
    order_json_records = [r for r in caplog.records if r.getMessage().startswith("ORDER JSON: ")]
    assert len(order_json_records) == 1
    rendered = order_json_records[0].getMessage().removeprefix("ORDER JSON: ")
    # Byte-identical to a no-indent json.dumps of the round-tripped body: no newlines, no
    # indentation. Under indent=2 this would carry thousands of indentation chars for depth 150
    # (and fail), while still proving the full payload is logged.
    assert "\n" not in rendered
    assert rendered == json.dumps(json.loads(body))


async def test_inline_send_bounded_by_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A send that takes far longer than the inline budget in TOTAL — even though NO single httpx
    # per-operation sub-timeout would ever fire (this sender bypasses httpx entirely and just
    # sleeps) — must be cut off at ~3s by asyncio.wait_for, so the Shopify ack is never held past
    # the budget. A bare-float httpx timeout would NOT bound this: it caps each connect/read/write
    # op, not the total elapsed. Without the wait_for wrapper the sleep would run to completion and
    # the ack would arrive ~8s later, blowing Shopify's <5s budget.
    await _enable_live_sending()

    async def _slow_sender(*_args: object, **_kwargs: object):
        await asyncio.sleep(8)
        raise AssertionError("inline send should have been cancelled before completing")

    monkeypatch.setattr("app.jobs.outbox_drain.send_template", _slow_sender)

    gid = "gid://shopify/Order/slowsend"
    body = json.dumps(payload(gid)).encode()
    start = time.monotonic()
    resp = await post(body, headers(body, webhook_id="wh-slowsend"))
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    # Cut off near the 3s inline bound, well under Shopify's 5s ack budget — NOT the 8s send.
    assert elapsed < 5.0
    # The timed-out send left the row un-sent; it stays recoverable for the backstop drain.
    store = get_container().ingest
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state != "sent"


async def test_slow_image_fetch_alone_stays_bounded_and_drops_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Item 2: exercise the ACTUAL asyncio.wait_for timeout branch for the image fetch (not just an
    # exception). The mock sleeps far past _IMAGE_FETCH_TIMEOUT_SECONDS, so wait_for cancels it;
    # the handler must still complete quickly, ack 200, and queue the row WITHOUT an image_url.
    # send_mode is the default `off` here, so no inline send runs — this isolates the image bound.
    c = get_container()

    async def _slow_image(product_gid: str, variant_gid: str | None = None) -> str:
        await asyncio.sleep(8)
        raise AssertionError("image fetch should have been cancelled by the timeout")

    monkeypatch.setattr(c.shopify, "get_product_image_url", _slow_image)

    body = json.dumps(_payload_with_product("gid://shopify/Order/slowimg")).encode()
    start = time.monotonic()
    resp = await post(body, headers(body, webhook_id="wh-slowimg"))
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    # Cut off at the ~1s image bound, nowhere near the 8s sleep.
    assert elapsed < 3.0
    draft = c.ingest.outbound["order_created:gid://shopify/Order/slowimg"]  # type: ignore[attr-defined]
    params = json.loads(draft.payload_json)
    assert params["body_params"]["product_name"] == "Chic Kurta Set"
    assert "image_url" not in params


async def test_slow_image_fetch_and_slow_send_together_stay_under_ack_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Item 1 (MAJOR): the WHOLE inline path — the product-image fetch AND the WhatsApp send — must
    # stay under Shopify's <5s ack budget even when BOTH are slow at once. The prior timing test
    # only slowed the send against a payload with no line items, so the image-fetch delay never
    # entered it. Here a product IS present (image fetch attempted) AND live sending is on, so both
    # bounds stack: 1.0s image + 3.0s send = ~4.0s worst case, with real margin under 5s.
    await _enable_live_sending()

    async def _slow_image(product_gid: str, variant_gid: str | None = None) -> str:
        await asyncio.sleep(8)
        raise AssertionError("image fetch should have been cancelled by the timeout")

    async def _slow_sender(*_args: object, **_kwargs: object):
        await asyncio.sleep(8)
        raise AssertionError("inline send should have been cancelled before completing")

    monkeypatch.setattr(get_container().shopify, "get_product_image_url", _slow_image)
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", _slow_sender)

    gid = "gid://shopify/Order/slowboth"
    body = json.dumps(_payload_with_product(gid)).encode()
    start = time.monotonic()
    resp = await post(body, headers(body, webhook_id="wh-slowboth"))
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    # Both bounds stacked (image ~1s + send ~3s) must still land safely under the 5s ack budget.
    assert elapsed < 5.0
    # The image never resolved (dropped), and the timed-out send left the row recoverable.
    store = get_container().ingest
    draft = store.outbound[f"order_created:{gid}"]  # type: ignore[attr-defined]
    assert "image_url" not in json.loads(draft.payload_json)
    views = {v.dedupe_key: v for v in await store.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state != "sent"
