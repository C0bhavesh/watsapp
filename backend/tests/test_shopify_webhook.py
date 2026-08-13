import base64
import hashlib
import hmac as hmac_lib
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.deps import get_container, reset_container
from app.shopify.models import Customer

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


async def test_webhook_queues_but_never_sends_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-001 regression: the webhook only queues + acks Shopify. It must NEVER run any send
    # logic itself — not inline, not via a background task. Delivery is the 1-minute cron's job
    # (`send_one_outbound`). Any call to send_template/send_one_outbound from the webhook path
    # would trip these guards. The row is left `queued` for the cron to pick up.
    import app.jobs.outbox_drain as drain

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the webhook path must not send WhatsApp — the cron does that")

    monkeypatch.setattr(drain, "send_template", _boom)
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

    assert HANDLED_TOPICS == {"orders/create", "orders/updated", "customers/update"}
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
