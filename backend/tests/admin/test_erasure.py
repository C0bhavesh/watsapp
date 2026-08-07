"""DPDP right-to-erasure admin endpoint: POST /admin/erasure (delete by phone)."""

import asyncio
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft

PHONE = "+919111111111"
OTHER = "+919222222222"
# The raw Meta `wa_id` for PHONE: same customer, different string (no `+`).
WA_ID = "919111111111"
WEBHOOK_SECRET = "app-secret-erasure"
PHONE_NUMBER_ID = "1298805403309058"


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


def _configure_conversation_engine() -> None:
    """Enable the WhatsApp edge + the conversation engine in shadow mode (persist, never send)."""
    c = get_container()

    async def _seed() -> None:
        await c.config.set_secret("whatsapp:access_token", "tok")
        await c.config.set_secret("whatsapp:app_secret", WEBHOOK_SECRET)
        await c.config.set_secret("whatsapp:verify_token", "verify-me")
        await c.config.set_plain("whatsapp:phone_number_id", PHONE_NUMBER_ID)
        await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
        await c.config.set_plain("whatsapp:api_version", "v23.0")
        await c.config.set_plain("send_mode", "shadow")

    asyncio.run(_seed())


def _post_inbound_text(client: TestClient, wa_id: str, text: str) -> None:
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "messages": [{
                "from": wa_id,
                "id": f"wamid.erasure-{wa_id}",
                "timestamp": "1",
                "type": "text",
                "text": {"body": text},
            }],
        }}]}],
    }).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post(
        "/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert r.status_code == 200, r.text


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


def test_erasure_rejects_unicode_digit_phone(client: TestClient) -> None:
    login(client)
    # Arabic-Indic digits satisfy a Unicode-aware \\d but match zero real rows — they would
    # otherwise produce a false "success" with a fake audit line. ASCII-only [0-9] rejects them.
    r = client.post("/admin/erasure", json={"phone": "+٩١٩١١١١١١١١١"})
    assert r.status_code == 422


def test_conversation_is_stored_under_the_phone_erasure_deletes_by(client: TestClient) -> None:
    """DPDP: a conversation must be keyed on the SAME E.164 phone /admin/erasure deletes by.

    The engine used to store history under the raw Meta ``wa_id`` ("919111111111") while the
    erasure delete binds ``phone_e164`` ("+919111111111") to ``WHERE user_id = $1`` — different
    strings for one customer, so the delete matched zero rows and the endpoint reported a
    success that never removed the chat history. (The Postgres delete's own binding is asserted
    in tests/store/test_deletion.py; the in-memory ingest store has no conversation tables, so
    this half proves the key the engine writes.)
    """
    login(client)
    _configure_conversation_engine()
    _post_inbound_text(client, WA_ID, "hello there")

    c = get_container()
    conversation_id = asyncio.run(c.conversations.get_or_create(PHONE))
    messages = asyncio.run(c.conversations.recent_messages(conversation_id, 10))
    # The turn was persisted under the E.164 key, not created fresh by the lookup above.
    assert [m.content for m in messages if m.role == "user"] == ["hello there"]
    # And the raw wa_id is not a key at all: looking it up creates a NEW, empty conversation.
    raw_id = asyncio.run(c.conversations.get_or_create(WA_ID))
    assert raw_id != conversation_id
    assert asyncio.run(c.conversations.recent_messages(raw_id, 10)) == []


def test_erasure_is_rate_limited(client: TestClient) -> None:
    login(client)
    # Destructive, irreversible, PII-touching: throttle it like /admin/login. 10/minute.
    for _ in range(10):
        assert client.post("/admin/erasure", json={"phone": "+910000000000"}).status_code == 200
    assert client.post("/admin/erasure", json={"phone": "+910000000000"}).status_code == 429
