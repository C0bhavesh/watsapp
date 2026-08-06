import hashlib
import hmac as hmac_lib
import json

import httpx
import pytest

from app.deps import get_container, reset_container

SECRET = "app-secret-webhook"
VERIFY_TOKEN = "verify-me"
PHONE_NUMBER_ID = "1298805403309058"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", SECRET)
    await c.config.set_secret("whatsapp:verify_token", VERIFY_TOKEN)
    await c.config.set_plain("whatsapp:phone_number_id", PHONE_NUMBER_ID)
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    yield
    reset_container()


def envelope(message: dict, phone_number_id: str = PHONE_NUMBER_ID) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "messages": [message],
                },
            }],
        }],
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac_lib.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def get(path: str, params: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def post(body: bytes, headers: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook/whatsapp", content=body, headers=headers)


async def test_get_verify_success() -> None:
    resp = await get(
        "/webhook/whatsapp",
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "xyz123"},
    )
    assert resp.status_code == 200
    assert resp.text == "xyz123"


async def test_get_verify_wrong_token_403() -> None:
    resp = await get(
        "/webhook/whatsapp",
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "xyz123"},
    )
    assert resp.status_code == 403


async def test_post_bad_hmac_403() -> None:
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.1", "type": "text", "text": {"body": "hi"}}
    )).encode()
    resp = await post(body, {"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert resp.status_code == 403


async def test_post_new_text_event_acknowledged() -> None:
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.1", "timestamp": "1",
         "type": "text", "text": {"body": "hi"}}
    )).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["processed"] == 1
    assert data["duplicate"] == 0
    assert data["results"] == [
        {"message_id": "wamid.1", "duplicate": False, "event_type": "InboundText"}
    ]


async def test_post_replay_is_duplicate() -> None:
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.2", "timestamp": "1",
         "type": "text", "text": {"body": "hi"}}
    )).encode()
    headers = {"X-Hub-Signature-256": sign(body)}
    await post(body, headers)
    resp = await post(body, headers)
    data = resp.json()
    assert data["processed"] == 0
    assert data["duplicate"] == 1


async def test_post_status_callback_ignored() -> None:
    body = json.dumps(
        {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.s1"}]}}]}]}
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_foreign_phone_number_id_ignored() -> None:
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.3", "timestamp": "1",
         "type": "text", "text": {"body": "hi"}},
        phone_number_id="9999999999999",
    )).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_missing_metadata_not_processed() -> None:
    # No metadata block at all: the tenant guard must fail CLOSED, not process it.
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "messages": [{"from": "919999999999", "id": "wamid.nm", "timestamp": "1",
                          "type": "text", "text": {"body": "hi"}}],
        }}]}],
    }).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_non_str_phone_number_id_not_processed() -> None:
    # phone_number_id present but not a str: fail CLOSED.
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": 1298805403309058},
            "messages": [{"from": "919999999999", "id": "wamid.ns", "timestamp": "1",
                          "type": "text", "text": {"body": "hi"}}],
        }}]}],
    }).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_mismatched_phone_number_id_not_processed() -> None:
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.mm", "timestamp": "1",
         "type": "text", "text": {"body": "hi"}},
        phone_number_id="9999999999999",
    )).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_batch_of_two_both_processed() -> None:
    # Meta may batch multiple messages; neither may be dropped.
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "messages": [
                {"from": "919999999999", "id": "wamid.b1", "timestamp": "1",
                 "type": "text", "text": {"body": "cancel my order"}},
                {"from": "919999999999", "id": "wamid.b2", "timestamp": "2",
                 "type": "text", "text": {"body": "please"}},
            ],
        }}]}],
    }).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["processed"] == 2
    assert data["duplicate"] == 0
    assert {r["message_id"] for r in data["results"]} == {"wamid.b1", "wamid.b2"}


async def test_post_batch_dedupe_second_delivery() -> None:
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "messages": [
                {"from": "919999999999", "id": "wamid.d1", "timestamp": "1",
                 "type": "text", "text": {"body": "one"}},
                {"from": "919999999999", "id": "wamid.d2", "timestamp": "2",
                 "type": "text", "text": {"body": "two"}},
            ],
        }}]}],
    }).encode()
    headers = {"X-Hub-Signature-256": sign(body)}
    await post(body, headers)
    resp = await post(body, headers)
    data = resp.json()
    assert data["processed"] == 0
    assert data["duplicate"] == 2


async def test_post_button_tap_event_type() -> None:
    body = json.dumps(envelope({
        "from": "919999999999", "id": "wamid.4", "timestamp": "1", "type": "button",
        "button": {"text": "Confirm Order", "payload": "order:confirm:gid://1"},
    })).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    data = resp.json()
    assert data["processed"] == 1
    assert data["results"][0]["event_type"] == "InboundButton"


async def test_post_garbage_body_ignored() -> None:
    body = b"not-json"
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}


async def test_get_verify_corrupt_secret_fails_closed_403() -> None:
    # Master key rotated / stored secret corrupt -> vault.decrypt raises VaultError.
    # The GET verify route must fail closed to 403, never surface a 500.
    await get_container().config_repo.set("whatsapp:app_secret", "gAAAAAcorrupt")
    resp = await get(
        "/webhook/whatsapp",
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "xyz123"},
    )
    assert resp.status_code == 403


async def test_post_corrupt_secret_fails_closed_403() -> None:
    # Same scenario on the POST receive route: VaultError -> 403, not 500.
    await get_container().config_repo.set("whatsapp:app_secret", "gAAAAAcorrupt")
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.9", "timestamp": "1",
         "type": "text", "text": {"body": "hi"}}
    )).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 403


async def test_post_text_event_without_llm_configured_sends_safe_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["to"] = to
        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="wamid.reply", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "where is my order"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert resp.json()["processed"] == 1
    assert sent["to"] == "919999999999"
    assert "team" in sent["body"]


async def test_post_text_event_send_mode_off_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    # send_mode defaults to "off" -- no controls saved in this test.

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text2",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False


async def test_post_text_event_paused_conversation_stays_silent_but_records_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    from datetime import UTC, datetime, timedelta

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    conversation_id = await c.conversations.get_or_create("919999999999")
    await c.conversations.pause_until(conversation_id, datetime.now(UTC) + timedelta(hours=1))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text3",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "still there?"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert any(m.content == "still there?" for m in messages)


async def test_post_text_event_shadow_mode_processes_but_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="shadow"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.text4",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False
    conversation_id = await c.conversations.get_or_create("919999999999")
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert len(messages) == 2  # user turn + assistant turn, persisted even though not sent


async def test_post_button_tap_still_unaffected_by_conversation_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 4 only wires InboundText -- button/interactive taps still just echo event_type."""
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        called["sent"] = True
        raise AssertionError("send_text must not be called for a button tap")

    monkeypatch.setattr("app.channels.whatsapp.send_text", fake_send_text)

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.btn1",
                "timestamp": "1",
                "type": "button",
                "button": {"text": "Confirm Order", "payload": "order:confirm:gid://1"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.json() == {
        "ok": True,
        "processed": 1,
        "duplicate": 0,
        "results": [
            {"message_id": "wamid.btn1", "duplicate": False, "event_type": "InboundButton"}
        ],
    }
    assert called["sent"] is False
