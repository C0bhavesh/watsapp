import asyncio
import hashlib
import hmac as hmac_lib
import json
import logging
from dataclasses import dataclass, field

import httpx
import pytest

from app.deps import get_container, reset_container
from app.providers.base import CompletionResult, Message

SECRET = "app-secret-webhook"
VERIFY_TOKEN = "verify-me"
PHONE_NUMBER_ID = "1298805403309058"

# Intent -> the agent module `core.conversation` dispatches to for it (module path used to
# monkeypatch `.run` directly, proving each dispatch arm wires to its OWN specialist).
_INTENT_MODULES = {
    "order_tracking": "app.agents.order_tracking",
    "product_search": "app.agents.product_search",
    "policy": "app.agents.policy",
    "recommendations": "app.agents.recommendations",
    "customer_support": "app.agents.customer_support",
}


@dataclass
class FakeProvider:
    """Scripted LLMProvider double -- returns each response text in order, one per call."""

    responses: list[str]
    calls: list[list[Message]] = field(default_factory=list)

    async def complete(
        self, model, messages, api_key, timeout, *, extra_params=None
    ) -> CompletionResult:
        self.calls.append(list(messages))
        return CompletionResult(text=self.responses[len(self.calls) - 1], model=model)


def _fake_active_llm(provider: FakeProvider):
    async def _active_llm(settings, config):
        return provider, "fake-model", "fake-key", None

    return _active_llm


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


def _button_body(payload: str, mid: str) -> bytes:
    return json.dumps(envelope({
        "from": "919999999999", "id": mid, "timestamp": "1", "type": "button",
        "button": {"text": "Confirm Order", "payload": payload},
    })).encode()


def _interactive_body(button_id: str, mid: str) -> bytes:
    return json.dumps(envelope({
        "from": "919999999999", "id": mid, "timestamp": "1", "type": "interactive",
        "interactive": {
            "type": "button_reply", "button_reply": {"id": button_id, "title": "Yes, cancel"}
        },
    })).encode()


async def _set_send_mode(mode: str) -> None:
    from app.admin.controls import AdminControls, save_controls

    await save_controls(get_container().config, AdminControls(send_mode=mode))


async def test_post_button_tap_dispatched_when_active(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    async def fake_dispatch(c, event):
        calls.append(event)

    monkeypatch.setattr("app.channels.whatsapp.dispatch_button", fake_dispatch)
    await _set_send_mode("live")
    body = _button_body("order:confirm:gid://shopify/Order/1", "wamid.btnA")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].payload == "order:confirm:gid://shopify/Order/1"  # type: ignore[attr-defined]
    assert resp.json()["results"][0]["event_type"] == "InboundButton"


async def test_post_interactive_tap_dispatched_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def fake_dispatch(c, event):
        calls.append(event)

    monkeypatch.setattr("app.channels.whatsapp.dispatch_button", fake_dispatch)
    await _set_send_mode("live")
    body = _interactive_body("order:cancel:confirm:gid://shopify/Order/1", "wamid.btnC")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].button_id == "order:cancel:confirm:gid://shopify/Order/1"  # type: ignore[attr-defined]
    assert resp.json()["results"][0]["event_type"] == "InboundInteractive"


async def test_post_button_tap_not_dispatched_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    async def fake_dispatch(c, event):
        calls.append(event)

    monkeypatch.setattr("app.channels.whatsapp.dispatch_button", fake_dispatch)
    # send_mode defaults to "off" (no controls saved) -> the kill switch disables the tap.
    body = _button_body("order:cancel:gid://shopify/Order/1", "wamid.btnB")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert calls == []
    assert resp.json()["processed"] == 1


async def test_post_button_tap_handler_raises_still_acks_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exploding_dispatch(c, event):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.channels.whatsapp.dispatch_button", exploding_dispatch)
    await _set_send_mode("live")
    body = _button_body("order:confirm:gid://shopify/Order/1", "wamid.btnD")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json()["processed"] == 1


class _ButtonFakeShopify:
    """Captures the mutations the REAL dispatch_button performs (get_order is unused because
    resolve_by_gid is stubbed)."""

    def __init__(self) -> None:
        self.add_tags_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def add_tags(self, auth: object, tags: object) -> None:
        self.add_tags_calls.append(auth.order.gid)  # type: ignore[attr-defined]

    async def cancel_order(
        self, auth: object, *, reason: str = "CUSTOMER", restock: bool = True
    ) -> object:
        from app.shopify.models import CancelRequested

        self.cancel_calls.append(auth.order.gid)  # type: ignore[attr-defined]
        return CancelRequested(job_id="gid://shopify/Job/1")


def _wire_real_dispatch(
    monkeypatch: pytest.MonkeyPatch, order: object
) -> tuple[_ButtonFakeShopify, list[str], list[tuple[str, str]]]:
    """Route the REAL dispatch_button through a fake Shopify + a resolve stub + captured sends.

    Returns (fake_shopify, resolve_calls, sends). resolve_calls stays empty when the kill switch
    suppresses the tap (suppression happens BEFORE the live re-fetch).
    """
    fake = _ButtonFakeShopify()
    monkeypatch.setattr(get_container(), "shopify", fake)

    resolve_calls: list[str] = []

    async def fake_resolve_by_gid(shopify: object, wa_id: str, gid: str) -> object:
        resolve_calls.append(gid)
        return order

    monkeypatch.setattr("app.core.order_actions.resolve_by_gid", fake_resolve_by_gid)

    sends: list[tuple[str, str]] = []

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sends.append((to, body))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    async def fake_send_buttons(http, cfg, to, body_text, buttons, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sends.append((to, body_text))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    async def fake_send_template(
        http, cfg, to, template_name, language, body_params, button_payloads=(),
        header_image_url=None, timeout=20.0,
    ):
        from app.channels.whatsapp_sender import SendResult

        sends.append((to, template_name))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    monkeypatch.setattr("app.core.order_actions.send_text", fake_send_text)
    monkeypatch.setattr("app.core.order_actions.send_buttons", fake_send_buttons)
    monkeypatch.setattr("app.core.order_actions.send_template", fake_send_template)
    return fake, resolve_calls, sends


async def test_post_button_tap_shadow_mode_no_mutation_no_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # shadow reaches dispatch_button (the webhook's off-gate lets it through), but the kill switch
    # inside dispatch_button suppresses it: no re-fetch, no mutation, no reply.
    fake, resolve_calls, sends = _wire_real_dispatch(
        monkeypatch, _owned_order("gid://shopify/Order/1", "tavas1")
    )
    await _set_send_mode("shadow")
    body = _button_body("order:confirm:gid://shopify/Order/1", "wamid.kill1")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json()["processed"] == 1  # still deduped/acked
    assert resolve_calls == []
    assert fake.add_tags_calls == []
    assert sends == []


async def test_post_button_tap_allowlist_miss_no_mutation_no_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, resolve_calls, sends = _wire_real_dispatch(
        monkeypatch, _owned_order("gid://shopify/Order/1", "tavas1")
    )
    from app.admin.controls import AdminControls, save_controls

    # A DIFFERENT number is allowlisted; the tapper 919999999999 is not.
    await save_controls(
        get_container().config,
        AdminControls(send_mode="allowlist", allowlist_phones=["+911111111111"]),
    )
    body = _interactive_body("order:cancel:confirm:gid://shopify/Order/1", "wamid.kill2")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resolve_calls == []
    assert fake.cancel_calls == []
    assert sends == []


async def test_post_button_tap_allowlist_hit_full_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, resolve_calls, sends = _wire_real_dispatch(
        monkeypatch, _owned_order("gid://shopify/Order/1", "tavas1")
    )
    from app.admin.controls import AdminControls, save_controls

    await save_controls(
        get_container().config,
        AdminControls(send_mode="allowlist", allowlist_phones=["+919999999999"]),
    )
    body = _button_body("order:confirm:gid://shopify/Order/1", "wamid.kill3")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resolve_calls == ["gid://shopify/Order/1"]  # allowlist hit -> re-fetch runs
    assert fake.add_tags_calls == ["gid://shopify/Order/1"]  # and the mutation fires
    assert len(sends) == 1  # a confirm reply goes out


async def test_post_button_tap_live_mode_full_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, resolve_calls, sends = _wire_real_dispatch(
        monkeypatch, _owned_order("gid://shopify/Order/1", "tavas1")
    )
    await _set_send_mode("live")
    body = _button_body("order:confirm:gid://shopify/Order/1", "wamid.kill4")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resolve_calls == ["gid://shopify/Order/1"]
    assert fake.add_tags_calls == ["gid://shopify/Order/1"]
    assert len(sends) == 1


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

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
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


async def test_post_text_event_fallback_uses_the_configured_default_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-English-speaking customer must not get an English error message."""
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    from app.admin.controls import AdminControls, save_controls
    from app.channels.copy import copy_for
    from app.deps import get_container

    # No LLM configured -> the fixed fallback copy is what goes out.
    await save_controls(
        get_container().config, AdminControls(send_mode="live", default_language="hi")
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.lang1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "मेरा ऑर्डर कहाँ है"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert sent["body"] == copy_for("error_fallback", "hi")


async def test_post_text_event_send_mode_off_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strengthened (security-review follow-up): proves the gate stops the pipeline BEFORE any
    # Shopify/LLM/history call -- not just that the final send is suppressed. If the off-check
    # were ever moved to the wrong spot (e.g. after these calls), these flags would go True and
    # the test would fail even though "sent" would still correctly be False.
    called = {"sent": False, "resolved_orders": False, "active_llm": False, "loaded_history": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    async def fake_resolve_by_phone(*args, **kwargs):
        called["resolved_orders"] = True
        return []

    async def fake_active_llm(*args, **kwargs):
        called["active_llm"] = True
        return None

    async def fake_load_history(*args, **kwargs):
        called["loaded_history"] = True
        return 1, []

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)
    monkeypatch.setattr("app.core.conversation.active_llm", fake_active_llm)
    monkeypatch.setattr("app.core.conversation.load_history", fake_load_history)
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
    assert called["resolved_orders"] is False
    assert called["active_llm"] is False
    assert called["loaded_history"] is False


async def test_post_text_event_paused_conversation_stays_silent_but_records_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strengthened (security-review follow-up): proves the pause gate stops the pipeline before
    # Shopify/LLM calls, not just before the send. load_history running is already implicitly
    # proven below (the pause check needs conversation_id, and the assertion reads back the
    # persisted message), so it is not separately flag-checked here.
    called = {"sent": False, "resolved_orders": False, "active_llm": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    async def fake_resolve_by_phone(*args, **kwargs):
        called["resolved_orders"] = True
        return []

    async def fake_active_llm(*args, **kwargs):
        called["active_llm"] = True
        return None

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)
    monkeypatch.setattr("app.core.conversation.active_llm", fake_active_llm)
    from datetime import UTC, datetime, timedelta

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    # Conversations are keyed on the normalized E.164 phone (what DPDP erasure deletes by),
    # not the raw wa_id "919999999999" -- seed the pause under that same key.
    conversation_id = await c.conversations.get_or_create("+919999999999")
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
    assert called["resolved_orders"] is False
    assert called["active_llm"] is False
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert any(m.content == "still there?" for m in messages)


async def test_post_text_event_paused_conversation_logs_the_silent_drop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A paused conversation used to swallow every inbound message with zero logging, so a
    24h AI outage for a real customer was invisible until a human read the transcript. The drop
    must now emit an info log carrying the conversation id and the paused-until timestamp."""
    from datetime import UTC, datetime, timedelta

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    async def fake_active_llm(*args, **kwargs):
        return None

    monkeypatch.setattr("app.core.conversation.active_llm", fake_active_llm)

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    conversation_id = await c.conversations.get_or_create("+919999999999")
    paused_until = datetime.now(UTC) + timedelta(hours=1)
    await c.conversations.pause_until(conversation_id, paused_until)

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.pausedlog1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "any update?"},
            }
        )
    ).encode()
    with caplog.at_level(logging.INFO, logger="app.core.conversation"):
        resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert "paused until" in caplog.text
    assert str(conversation_id) in caplog.text


async def test_post_text_event_expired_pause_lets_the_ai_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the handoff pause: it self-expires after its window with no admin
    action, and the AI answers normally again."""
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(
        responses=[
            json.dumps({"intent": "policy"}),
            json.dumps({"reply": "Our returns window is 7 days."}),
        ]
    )
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from datetime import UTC, datetime, timedelta

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    conversation_id = await c.conversations.get_or_create("+919999999999")
    # Pause already elapsed (a handoff from more than 24h ago).
    await c.conversations.pause_until(conversation_id, datetime.now(UTC) - timedelta(minutes=1))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.resume1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what is your return policy?"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert sent["body"] == "Our returns window is 7 days."  # not silence
    assert len(provider.calls) == 2  # the router + agent pipeline really ran
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert len(messages) == 2  # user turn + assistant turn


async def test_post_text_event_shadow_mode_processes_but_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
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
    conversation_id = await c.conversations.get_or_create("+919999999999")
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

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

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


async def test_post_text_event_live_mode_uses_llm_pipeline_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the router + agent LLM path actually runs (not just the no-LLM fallback) --
    the provider is called twice (router classify, then the dispatched agent's completion) and
    the sent body is the agent's own reply text, not the fixed fallback copy."""
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(
        responses=[
            json.dumps({"intent": "policy"}),
            json.dumps({"reply": "Our returns window is 7 days."}),
        ]
    )
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.live1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what is your return policy?"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert len(provider.calls) == 2
    assert sent["body"] == "Our returns window is 7 days."


async def test_post_text_event_shadow_mode_uses_llm_pipeline_but_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(
        responses=[
            json.dumps({"intent": "policy"}),
            json.dumps({"reply": "Our returns window is 7 days."}),
        ]
    )
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="shadow"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.shadow-llm",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what is your return policy?"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False
    assert len(provider.calls) == 2
    conversation_id = await c.conversations.get_or_create("+919999999999")
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert len(messages) == 2


@pytest.mark.parametrize("intent", list(_INTENT_MODULES))
async def test_post_text_event_dispatches_to_correct_agent(
    intent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the 5 router intents must reach its OWN specialist agent -- not just 'some
    agent', which a single end-to-end test can't distinguish since every agent's reply flows
    through the same send_text call."""
    from app.agents.base import AgentReply

    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(responses=[json.dumps({"intent": intent})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    stub_reply = AgentReply(text=f"stub-reply-for-{intent}")

    async def fake_run(*args, **kwargs):
        return stub_reply

    monkeypatch.setattr(f"{_INTENT_MODULES[intent]}.run", fake_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": f"wamid.intent.{intent}",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "some message"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert sent["body"] == f"stub-reply-for-{intent}"


@pytest.mark.parametrize(
    ("intent", "expect_resolved"),
    [("order_tracking", True), ("policy", False), ("customer_support", False)],
)
async def test_post_text_event_resolves_orders_only_for_order_tracking(
    intent: str, expect_resolved: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_by_phone re-fetches every mapped order from Shopify. Only order_tracking reads
    context.orders, so every other intent must not pay that latency."""
    called = {"resolved_orders": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    async def fake_resolve_by_phone(*args, **kwargs):
        called["resolved_orders"] = True
        return []

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    provider = FakeProvider(responses=[json.dumps({"intent": intent})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.agents.base import AgentReply

    async def fake_run(*args, **kwargs):
        return AgentReply(text="ok")

    monkeypatch.setattr(f"{_INTENT_MODULES[intent]}.run", fake_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": f"wamid.resolve.{intent}",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "some message"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["resolved_orders"] is expect_resolved


async def test_post_text_event_threads_the_admins_reveal_fields_to_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin's disclosure control has to actually reach the specialist -- it was configured
    but never read, so a narrowed reveal_fields changed nothing about what the model was told."""
    from app.agents.base import AgentReply

    seen: dict[str, object] = {}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_run(context, *args, **kwargs):
        seen["reveal_fields"] = context.reveal_fields
        return AgentReply(text="ok")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(
        get_container().config,
        AdminControls(send_mode="live", reveal_fields=["order_number"]),
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.reveal1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "where is my order"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["reveal_fields"] == ("order_number",)


async def test_post_text_event_agent_handoff_pauses_the_conversation_for_any_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentReply.handoff must be honored for EVERY agent, not just customer_support --
    that is what lets a model-judged Hindi/Gujarati "get me a person" escalate."""
    from datetime import UTC, datetime

    from app.agents.base import AgentReply

    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(responses=[json.dumps({"intent": "policy"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_policy_run(*args, **kwargs):
        return AgentReply(text="Let me bring in a teammate.", handoff=True)

    monkeypatch.setattr("app.agents.policy.run", fake_policy_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.handoff1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "this is not going well"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert sent["body"] == "Let me bring in a teammate."  # the reply still goes out
    conversation_id = await c.conversations.get_or_create("+919999999999")
    paused = await c.conversations.get_paused_until(conversation_id)
    assert paused is not None and paused > datetime.now(UTC)
    assert await c.conversations.get_handoff_attempted_at(conversation_id) is not None


async def test_post_text_event_allowlist_mode_sends_to_allowed_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["to"] = to
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    # normalize_phone("919999999999") -> "+919999999999" -- the allowlist must be checked
    # against that same E.164 form, not the raw wa_id.
    await save_controls(
        get_container().config,
        AdminControls(send_mode="allowlist", allowlist_phones=["+919999999999"]),
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.allow1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert sent["to"] == "919999999999"


async def test_post_text_event_allowlist_mode_skips_non_allowed_number_but_still_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    # A DIFFERENT number is allowlisted -- 919999999999 must be skipped, but still processed.
    await save_controls(
        c.config, AdminControls(send_mode="allowlist", allowlist_phones=["+911111111111"])
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.allow2",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False
    conversation_id = await c.conversations.get_or_create("+919999999999")
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert len(messages) == 2


async def test_post_text_event_send_failure_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(
            ok=False, status_code=470, wamid=None, error="code=470; message=blocked"
        )

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.sendfail",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()

    with caplog.at_level(logging.WARNING, logger="app.core.conversation"):
        resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert "whatsapp send failed" in caplog.text
    assert "470" in caplog.text


async def test_post_text_event_empty_agent_reply_falls_back_to_safe_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    # Garbage router completion degrades to the documented "customer_support" fallback intent;
    # that agent is monkeypatched directly so the empty-reply guard is proven independent of
    # what any real completion happens to contain.
    provider = FakeProvider(responses=["not json"])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.agents.base import AgentReply

    async def fake_customer_support_run(*args, **kwargs):
        return AgentReply(text="   ")  # whitespace-only -- strip_markdown collapses it to ""

    monkeypatch.setattr("app.agents.customer_support.run", fake_customer_support_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.empty1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert sent["body"]
    assert "team" in sent["body"]  # the fixed error_fallback copy, never a blank message


async def test_post_text_event_agent_crash_still_sends_the_fallback_copy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-ProviderError inside an agent (a KeyError in prompt formatting, an unexpected
    store error) used to reach run_turn's blanket handler, which logs and sends NOTHING. Any
    specialist failure must degrade to the fixed error_fallback copy, never silence."""
    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(responses=[json.dumps({"intent": "policy"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def exploding_run(*args, **kwargs):
        raise KeyError("personality")

    monkeypatch.setattr("app.agents.policy.run", exploding_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.crash1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what is your return policy?"},
            }
        )
    ).encode()

    with caplog.at_level(logging.ERROR, logger="app.core.conversation"):
        resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert "team" in str(sent.get("body", ""))  # the fixed error_fallback copy went out
    assert "KeyError" in caplog.text
    conversation_id = await c.conversations.get_or_create("+919999999999")
    messages = await c.conversations.recent_messages(conversation_id, 10)
    assert len(messages) == 2  # the turn is still persisted, not lost


async def test_post_text_event_agent_crash_in_shadow_mode_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degraded reply goes through the same send_mode gates as a normal reply."""
    called = {"sent": False}

    async def fake_send_text(*args, **kwargs):
        from app.channels.whatsapp_sender import SendResult

        called["sent"] = True
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(responses=[json.dumps({"intent": "policy"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def exploding_run(*args, **kwargs):
        raise KeyError("personality")

    monkeypatch.setattr("app.agents.policy.run", exploding_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="shadow"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.crash2",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what is your return policy?"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert called["sent"] is False


async def test_post_text_event_timeout_is_caught_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.core.conversation.TURN_TIMEOUT_SECONDS", 0.01)

    async def slow_active_llm(*args, **kwargs):
        await asyncio.sleep(0.05)
        return None

    monkeypatch.setattr("app.core.conversation.active_llm", slow_active_llm)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.timeout1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            }
        )
    ).encode()

    with caplog.at_level(logging.WARNING, logger="app.core.conversation"):
        resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert "timed out" in caplog.text


async def test_post_batch_stops_running_turns_once_the_request_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Meta batches several messages into one delivery. A per-turn timeout gave an N-message
    batch an N x 55s ceiling; the budget must cover the whole request instead."""
    turns: list[str] = []

    async def slow_run_turn(c, event, budget_seconds=None):
        turns.append(event.message_id)
        await asyncio.sleep(0.06)

    monkeypatch.setattr("app.channels.whatsapp.run_turn", slow_run_turn)
    monkeypatch.setattr("app.channels.whatsapp.TURN_TIMEOUT_SECONDS", 0.05)

    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "messages": [
                {"from": "919999999999", "id": f"wamid.budget{i}", "timestamp": str(i),
                 "type": "text", "text": {"body": "hello"}}
                for i in range(1, 4)
            ],
        }}]}],
    }).encode()

    with caplog.at_level(logging.WARNING, logger="app.channels.whatsapp"):
        resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    data = resp.json()
    assert data["processed"] == 3  # every message is still deduped and acked
    assert turns == ["wamid.budget1"]  # the budget was spent by the first turn
    assert "budget" in caplog.text


async def test_post_batch_within_budget_runs_every_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns: list[str] = []

    async def fast_run_turn(c, event, budget_seconds=None):
        turns.append(event.message_id)

    monkeypatch.setattr("app.channels.whatsapp.run_turn", fast_run_turn)

    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "messages": [
                {"from": "919999999999", "id": f"wamid.ok{i}", "timestamp": str(i),
                 "type": "text", "text": {"body": "hello"}}
                for i in range(1, 4)
            ],
        }}]}],
    }).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert turns == ["wamid.ok1", "wamid.ok2", "wamid.ok3"]


def _handoff_scenario(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Wire a policy-agent handoff and record every (recipient, body) send_text takes."""
    from app.agents.base import AgentReply

    sends: list[tuple[str, str]] = []

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        sends.append((to, body))
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(responses=[json.dumps({"intent": "policy"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_policy_run(*args, **kwargs):
        return AgentReply(text="Let me bring in a teammate.", handoff=True)

    monkeypatch.setattr("app.agents.policy.run", fake_policy_run)
    return sends


async def _post_handoff_message(message_id: str) -> httpx.Response:
    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": message_id,
                "timestamp": "1",
                "type": "text",
                "text": {"body": "this is not going well"},
            }
        )
    ).encode()
    return await post(body, {"X-Hub-Signature-256": sign(body)})


async def test_post_text_event_handoff_alerts_the_configured_owner_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HANDOFF_MESSAGE promises "they'll continue helping you right here in this chat", but the
    bot just went silent for 24h and nobody was told. The owner must actually be paged."""
    sends = _handoff_scenario(monkeypatch)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(
        get_container().config,
        AdminControls(send_mode="live", owner_alert_number="+918888888888"),
    )

    assert (await _post_handoff_message("wamid.alert1")).status_code == 200

    assert len(sends) == 2
    assert sends[0] == ("919999999999", "Let me bring in a teammate.")
    owner_to, owner_body = sends[1]
    assert owner_to == "+918888888888"
    assert "+919999999999" in owner_body  # the customer's number, so the owner can reply


async def test_post_text_event_handoff_without_owner_number_only_sends_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured owner_alert_number degrades silently, like an empty allowlist."""
    sends = _handoff_scenario(monkeypatch)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    assert (await _post_handoff_message("wamid.alert2")).status_code == 200
    assert [to for to, _ in sends] == ["919999999999"]


async def test_post_text_event_handoff_in_shadow_mode_does_not_alert_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow suppresses ALL outbound WhatsApp traffic -- the owner alert included."""
    sends = _handoff_scenario(monkeypatch)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(
        get_container().config,
        AdminControls(send_mode="shadow", owner_alert_number="+918888888888"),
    )

    assert (await _post_handoff_message("wamid.alert3")).status_code == 200
    assert sends == []


async def test_post_text_event_no_handoff_never_alerts_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agents.base import AgentReply

    sends = _handoff_scenario(monkeypatch)

    async def fake_policy_run(*args, **kwargs):
        return AgentReply(text="Our returns window is 7 days.")

    monkeypatch.setattr("app.agents.policy.run", fake_policy_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(
        get_container().config,
        AdminControls(send_mode="live", owner_alert_number="+918888888888"),
    )

    assert (await _post_handoff_message("wamid.alert4")).status_code == 200
    assert [to for to, _ in sends] == ["919999999999"]


def _owned_order(gid: str, name: str) -> object:
    """Build an AuthorizedOrder owned by the +919999999999 test sender."""
    from app.shopify.models import AuthorizedOrder, Order

    return AuthorizedOrder(
        order=Order(
            gid=gid, name=name, email=None, phone="+919999999999", shipping_phone=None,
            billing_phone=None, financial_status="paid", fulfillment_status="UNFULFILLED",
            cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
            customer_locale=None,
        ),
        verified_phone="+919999999999",
    )


async def test_post_text_event_recovers_owned_order_from_order_name_when_phone_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MAJOR gap: a sender whose WhatsApp number maps to NO order, but who types an order
    number they own, must have that order looked up and folded into the order-tracking context
    (previously resolve_by_order_name had zero callers, so the bot just handed off)."""
    from app.agents.base import AgentReply

    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    # The sender's number maps to nothing.
    async def fake_resolve_by_phone(*args, **kwargs):
        return []

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    owned = _owned_order("gid://99", "tavas4242")

    async def fake_resolve_by_order_name(shopify, wa_id, raw_name):
        seen["raw_name"] = raw_name
        seen["wa_id"] = wa_id
        return owned

    monkeypatch.setattr(
        "app.core.conversation.resolve_by_order_name", fake_resolve_by_order_name
    )

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["orders"] = list(context.orders)
        return AgentReply(text="Your order tavas4242 is confirmed.")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hey where is tavas4242"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    # The token was extracted from the real message and looked up, ownership-checked, for this
    # sender -- and the resulting order reached the order-tracking agent's context.
    assert seen["raw_name"] == "tavas4242"
    assert seen["wa_id"] == "919999999999"
    assert seen["orders"] == [owned]


async def test_post_text_event_with_phone_orders_also_recovers_a_different_mentioned_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A customer with a phone-mapped order can still ask about a DIFFERENT order they own (e.g.
    placed under different contact info) -- the order-name scan runs regardless of whether the
    phone path already found something, and the recovered order is added alongside it."""
    from app.agents.base import AgentReply

    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    phone_order = _owned_order("gid://mapped", "tavas1000")
    other_order = _owned_order("gid://99", "tavas4242")

    async def fake_resolve_by_phone(*args, **kwargs):
        return [phone_order]

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    async def fake_resolve_by_order_name(shopify, wa_id, raw_name):
        seen["raw_name"] = raw_name
        return other_order

    monkeypatch.setattr(
        "app.core.conversation.resolve_by_order_name", fake_resolve_by_order_name
    )

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["orders"] = list(context.orders)
        return AgentReply(text="ok")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover2",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "what about tavas4242 too"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["raw_name"] == "tavas4242"
    assert seen["orders"] == [phone_order, other_order]


async def test_post_text_event_does_not_duplicate_an_order_already_found_by_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the mentioned order number is the SAME one the phone path already found, it must not
    be appended a second time."""
    from app.agents.base import AgentReply

    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    phone_order = _owned_order("gid://mapped", "tavas4242")

    async def fake_resolve_by_phone(*args, **kwargs):
        return [phone_order]

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    async def fake_resolve_by_order_name(shopify, wa_id, raw_name):
        return _owned_order("gid://mapped", "tavas4242")

    monkeypatch.setattr(
        "app.core.conversation.resolve_by_order_name", fake_resolve_by_order_name
    )

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["orders"] = list(context.orders)
        return AgentReply(text="ok")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover3",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "where is tavas4242"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["orders"] == [phone_order]


async def test_post_text_event_wrong_digit_order_number_asks_customer_to_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A number-shaped token of the wrong digit count never reaches Shopify -- the agent gets a
    format hint instead, so it can ask the customer to double-check their order ID."""
    seen: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    async def fake_resolve_by_phone(*args, **kwargs):
        return []

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("Shopify was queried for a token already known to be malformed")

    monkeypatch.setattr("app.core.conversation.resolve_by_order_name", must_not_be_called)

    provider = FakeProvider(responses=[json.dumps({"intent": "order_tracking"})])
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    async def fake_order_tracking_run(context, *args, **kwargs):
        seen["hint"] = context.order_number_format_hint
        from app.agents.base import AgentReply

        return AgentReply(text="Could you double check your order ID?")

    monkeypatch.setattr("app.agents.order_tracking.run", fake_order_tracking_run)

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.recover4",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "my order id is 965"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    assert seen["hint"] is not None


async def _seed_sent_outbound(wamid: str, phone: str = "+911111111111") -> None:
    """Queue an outbound row and mark it sent with a known wamid on the live container's
    IngestStore, so a status webhook for that wamid has a real outbound_messages row to hit."""
    from app.store.base import OutboundDraft

    ingest = get_container().ingest
    outbound_id = await ingest.enqueue_outbound(
        OutboundDraft(
            dedupe_key=f"order_created:{wamid}",
            kind="order_confirmation",
            phone_e164=phone,
            payload_json='{"template": "cod_confirmation"}',
        )
    )
    assert outbound_id is not None
    await ingest.mark_outbound_sent(outbound_id, wamid)


def _status_envelope(wamid: str, status: str) -> bytes:
    return json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "statuses": [{"id": wamid, "status": status, "timestamp": "1"}],
        }}]}],
    }).encode()


async def test_webhook_status_event_updates_outbound_delivery_status() -> None:
    await _seed_sent_outbound("wamid.SEEDED", phone="+919664290413")

    body = _status_envelope("wamid.SEEDED", "delivered")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    entries = await get_container().ingest.find_outbound_by_phone("+919664290413")
    assert entries[-1].delivery_status == "delivered"


async def test_webhook_status_event_updates_ai_reply_delivery_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real inline AI reply is sent (wamid captured), then a status webhook for that same wamid
    # must land on the messages table (not outbound_messages).
    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult

        return SendResult(ok=True, status_code=200, wamid="wamid.REPLY1", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)
    from app.admin.controls import AdminControls, save_controls

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.reply.turn1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "where is my order"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200

    status_body = _status_envelope("wamid.REPLY1", "delivered")
    status_resp = await post(status_body, {"X-Hub-Signature-256": sign(status_body)})
    assert status_resp.status_code == 200
    # The wamid was really attached to the assistant's persisted row: routing it succeeds.
    assert await c.conversations.apply_message_delivery_status("wamid.REPLY1", "read") is True


async def test_webhook_status_processing_exception_still_acks_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exploding_apply(c, status):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.channels.whatsapp.apply_delivery_status", exploding_apply)

    body = _status_envelope("wamid.SEEDED", "delivered")
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200


async def test_post_text_event_threads_history_into_router_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router's classify_intent call must receive the loaded conversation history, not
    just the bare current message -- otherwise a short reply like a bare order number right
    after the bot asked for one has no way to be classified correctly."""
    from app.channels.whatsapp_sender import SendResult

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    async def fake_resolve_by_phone(*args, **kwargs):
        return []

    monkeypatch.setattr("app.core.conversation.resolve_by_phone", fake_resolve_by_phone)

    class _RecordingProvider:
        def __init__(self) -> None:
            self.calls: list[list[object]] = []

        async def complete(self, model, messages, api_key, timeout, *, extra_params=None):
            self.calls.append(messages)
            if len(self.calls) == 1:
                # The router call.
                return CompletionResult(text=json.dumps({"intent": "order_tracking"}), model=model)
            return CompletionResult(text=json.dumps({"reply": "ok", "handoff": False}), model=model)

    provider = _RecordingProvider()
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    # Conversations are keyed on the normalized E.164 phone (what the real turn loads history
    # by), not the raw wa_id "919999999999" -- seed the history under that same key.
    conversation_id = await c.conversations.get_or_create("+919999999999")
    await c.conversations.append_message(conversation_id, "user", "can u tell me my order detail")
    await c.conversations.append_message(
        conversation_id, "assistant", "Could you please share your order number?"
    )

    body = json.dumps(
        envelope(
            {
                "from": "919999999999",
                "id": "wamid.routerhistory1",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "9652"},
            }
        )
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    router_messages = provider.calls[0]
    contents = [m.content for m in router_messages]
    assert "can u tell me my order detail" in contents
    assert "Could you please share your order number?" in contents
