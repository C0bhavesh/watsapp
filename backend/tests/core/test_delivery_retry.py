"""Delivery-failure auto-retry: resend logic + owner-alert-on-exhaustion.

Covers both the outbound-template path (retry_failed_outbound) and the AI-reply path
(retry_failed_message): a fresh 'failed' delivery status triggers a single resend (gated by the
same send_mode/allowlist kill switch as every other outbound send), the retry count is capped at
MAX_RETRIES, and exhaustion (or a send that can never get a future wamid) pages the owner.
"""

import json

import pytest

from app.admin.controls import AdminControls
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_sender import SendResult, WhatsAppSendError
from app.core.delivery_retry import (
    MAX_RETRIES,
    retry_failed_message,
    retry_failed_outbound,
)
from app.deps import get_container, reset_container
from app.store.base import MappingUpsert, OutboundDraft

PHONE = "+919664290413"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", "sec")
    await c.config.set_secret("whatsapp:verify_token", "ver")
    await c.config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    yield
    reset_container()


class FakeTemplateSender:
    """Records send_template calls; returns a scripted result (or raises)."""

    def __init__(self, result: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {
                "to": to,
                "template": template_name,
                "language": language,
                "body_params": body_params,
                "button_payloads": list(button_payloads),
                "header_image_url": header_image_url,
            }
        )
        if isinstance(self._result, Exception):
            raise self._result
        assert isinstance(self._result, SendResult)
        return self._result


class FakeTextSender:
    """Records send_text calls (used for both the AI-reply resend and the owner alert)."""

    def __init__(self, result: object = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(self, http, cfg, to, body, timeout=20.0) -> SendResult:
        self.calls.append({"to": to, "body": body})
        if isinstance(self._result, Exception):
            raise self._result
        if isinstance(self._result, SendResult):
            return self._result
        # Default: a successful alert send (owner alerts don't care about the wamid).
        return SendResult(ok=True, status_code=200, wamid=None, error=None)


def _install_template(monkeypatch: pytest.MonkeyPatch, sender: FakeTemplateSender) -> None:
    monkeypatch.setattr("app.core.delivery_retry.send_template", sender)


def _install_text(monkeypatch: pytest.MonkeyPatch, sender: FakeTextSender) -> None:
    monkeypatch.setattr("app.core.delivery_retry.send_text", sender)


async def _wa_cfg() -> WhatsAppConfig:
    cfg = await load_whatsapp_config(get_container().config)
    assert cfg is not None
    return cfg


async def _seed_outbound_sent(
    gid: str, wamid: str, phone: str = PHONE, payload: dict[str, object] | None = None,
) -> int:
    """Queue an outbound row, mark it sent with `wamid`. Returns the row id."""
    c = get_container()
    payload = payload or {
        "template": "cod_confirmation", "language": "en",
        "body_params": {
            "customer_name": "Suman", "order_id": "tavas3733",
            "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
            "product_amount": "949",
        },
        "image_url": "https://cdn.shopify.com/s/files/1/x.jpg",
        "buttons": [f"order:confirm:{gid}", f"order:cancel:{gid}"],
    }
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavas3733", order_number_int=3733, phone_e164=phone,
        customer_name="Suman", email=None, language="hi",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json=json.dumps(payload),
    )
    result = await c.ingest.ingest_order_created(f"wh-{gid}", "orders/create", mapping, draft)
    assert result.outbound_id is not None
    await c.ingest.mark_outbound_sent(result.outbound_id, wamid)
    return result.outbound_id


async def _seed_message_sent(wamid: str, content: str, phone: str = PHONE) -> tuple[int, int]:
    """Create a conversation + an assistant message marked sent with `wamid`.

    Returns (conversation_id, message_id).
    """
    c = get_container()
    conversation_id = await c.conversations.get_or_create(phone)
    message_id = await c.conversations.append_message(conversation_id, "assistant", content)
    await c.conversations.set_message_wamid(message_id, wamid)
    return conversation_id, message_id


# --- outbound-template path -------------------------------------------------


async def test_retry_failed_outbound_resends_with_original_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    gid = "gid://shopify/Order/1"
    await _seed_outbound_sent(gid, "wamid.ORIGINAL")
    assert await c.ingest.apply_outbound_delivery_status("wamid.ORIGINAL", "failed") == "applied"
    controls = AdminControls(send_mode="live", owner_alert_number="+919999999999")

    template = FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.RESENT", error=None)
    )
    text = FakeTextSender()
    _install_template(monkeypatch, template)
    _install_text(monkeypatch, text)

    await retry_failed_outbound(c, await _wa_cfg(), controls, "wamid.ORIGINAL")

    # Resent with byte-for-byte the original payload content.
    assert len(template.calls) == 1
    call = template.calls[0]
    assert call["to"] == PHONE
    assert call["template"] == "cod_confirmation"
    assert call["language"] == "en"
    assert call["body_params"] == {
        "customer_name": "Suman", "order_id": "tavas3733",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
    }
    assert call["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    assert call["header_image_url"] == "https://cdn.shopify.com/s/files/1/x.jpg"
    # Wamid rolled over to the resend; delivery_status reset to None (fresh send awaiting its own
    # confirmation). No owner alert on a successful resend.
    assert await c.ingest.get_outbound_retry_info("wamid.ORIGINAL") is None
    assert await c.ingest.get_outbound_retry_info("wamid.RESENT") is not None
    (entry,) = await c.ingest.find_outbound_by_phone(PHONE)
    assert entry.delivery_status is None
    assert text.calls == []


async def test_retry_failed_outbound_stops_and_alerts_at_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    gid = "gid://shopify/Order/2"
    row_id = await _seed_outbound_sent(gid, "wamid.ORIGINAL")
    for _ in range(MAX_RETRIES):
        await c.ingest.record_outbound_retry(row_id, None)  # retry_count -> 3, wamid unchanged
    controls = AdminControls(send_mode="live", owner_alert_number="+919999999999")

    template = FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.RESENT", error=None)
    )
    text = FakeTextSender()
    _install_template(monkeypatch, template)
    _install_text(monkeypatch, text)

    await retry_failed_outbound(c, await _wa_cfg(), controls, "wamid.ORIGINAL")

    assert template.calls == []  # no further resend once the cap is reached
    assert len(text.calls) == 1
    assert text.calls[0]["to"] == "+919999999999"
    assert PHONE in str(text.calls[0]["body"])


async def test_retry_failed_outbound_respects_send_mode_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    gid = "gid://shopify/Order/3"
    await _seed_outbound_sent(gid, "wamid.ORIGINAL")
    controls = AdminControls(send_mode="off", owner_alert_number="+919999999999")

    template = FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.RESENT", error=None)
    )
    text = FakeTextSender()
    _install_template(monkeypatch, template)
    _install_text(monkeypatch, text)

    await retry_failed_outbound(c, await _wa_cfg(), controls, "wamid.ORIGINAL")

    # Suppressed by send_decision -> no Meta template call at all.
    assert template.calls == []
    # A suppressed resend still burns a retry attempt (it can never earn a future wamid to hang the
    # next retry off), and the owner is paged since that resend will never land.
    info = await c.ingest.get_outbound_retry_info("wamid.ORIGINAL")
    assert info is not None
    assert info.retry_count == 1
    assert len(text.calls) == 1
    assert text.calls[0]["to"] == "+919999999999"


async def test_retry_failed_outbound_synchronous_send_failure_alerts_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    gid = "gid://shopify/Order/4"
    await _seed_outbound_sent(gid, "wamid.ORIGINAL")
    controls = AdminControls(send_mode="live", owner_alert_number="+919999999999")

    template = FakeTemplateSender(WhatsAppSendError("network down"))
    text = FakeTextSender()
    _install_template(monkeypatch, template)
    _install_text(monkeypatch, text)

    await retry_failed_outbound(c, await _wa_cfg(), controls, "wamid.ORIGINAL")

    # A transport error means this attempt earned no wamid -> alert immediately, do not wait for the
    # count to reach MAX_RETRIES.
    assert len(template.calls) == 1  # it was attempted, and raised
    info = await c.ingest.get_outbound_retry_info("wamid.ORIGINAL")
    assert info is not None
    assert info.retry_count == 1
    assert len(text.calls) == 1
    assert text.calls[0]["to"] == "+919999999999"


async def test_retry_failed_outbound_bad_payload_alerts_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    gid = "gid://shopify/Order/5"
    # Legacy/corrupt shape: no top-level body_params -> parse_payload returns None.
    await _seed_outbound_sent(gid, "wamid.ORIGINAL", payload={
        "template": "order_confirmation_cod", "language": "hi",
        "customer_name": "Suman", "order_name": "tavas3733", "amount": "949",
    })
    controls = AdminControls(send_mode="live", owner_alert_number="+919999999999")

    template = FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="wamid.RESENT", error=None)
    )
    text = FakeTextSender()
    _install_template(monkeypatch, template)
    _install_text(monkeypatch, text)

    await retry_failed_outbound(c, await _wa_cfg(), controls, "wamid.ORIGINAL")

    assert template.calls == []  # unrenderable payload -> never reaches the sender
    info = await c.ingest.get_outbound_retry_info("wamid.ORIGINAL")
    assert info is not None
    assert info.retry_count == 1
    assert len(text.calls) == 1
    assert text.calls[0]["to"] == "+919999999999"


# --- AI-reply (messages) path -----------------------------------------------


async def test_retry_failed_message_resends_ai_reply_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    content = "Your order is on the way."
    await _seed_message_sent("wamid.MSGORIG", content)
    assert await c.conversations.apply_message_delivery_status(
        "wamid.MSGORIG", "failed"
    ) == "applied"
    controls = AdminControls(send_mode="live", owner_alert_number="+919999999999")

    text = FakeTextSender(
        SendResult(ok=True, status_code=200, wamid="wamid.MSGRESENT", error=None)
    )
    _install_text(monkeypatch, text)

    await retry_failed_message(c, await _wa_cfg(), controls, "wamid.MSGORIG")

    # Resent the same content to the same phone (recipient resolved via the conversation user_id).
    assert len(text.calls) == 1
    assert text.calls[0]["to"] == PHONE
    assert text.calls[0]["body"] == content
    # Wamid rolled over; delivery_status reset to None. No owner alert on a successful resend.
    assert await c.conversations.get_message_retry_info("wamid.MSGORIG") is None
    assert await c.conversations.get_message_retry_info("wamid.MSGRESENT") is not None
    messages = await c.conversations.find_messages_by_user_id(PHONE)
    assert messages[-1].delivery_status is None


async def test_retry_failed_message_stops_and_alerts_at_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _, message_id = await _seed_message_sent("wamid.MSGORIG", "Your order is on the way.")
    for _ in range(MAX_RETRIES):
        await c.conversations.record_message_retry(message_id, None)  # retry_count -> 3
    controls = AdminControls(send_mode="live", owner_alert_number="+919999999999")

    text = FakeTextSender()
    _install_text(monkeypatch, text)

    await retry_failed_message(c, await _wa_cfg(), controls, "wamid.MSGORIG")

    # Only the owner alert is sent -- no resend to the customer once the cap is reached.
    assert len(text.calls) == 1
    assert text.calls[0]["to"] == "+919999999999"
    assert PHONE in str(text.calls[0]["body"])


# --- owner-alert degradation ------------------------------------------------


async def test_owner_alert_degrades_silently_when_unset_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Outbound at max retries with an unset owner number: must not raise, must not send to an empty
    # number.
    c = get_container()
    controls = AdminControls(send_mode="live", owner_alert_number="")
    out_id = await _seed_outbound_sent("gid://shopify/Order/6", "wamid.OUT")
    for _ in range(MAX_RETRIES):
        await c.ingest.record_outbound_retry(out_id, None)

    template = FakeTemplateSender(
        SendResult(ok=True, status_code=200, wamid="w", error=None)
    )
    text = FakeTextSender()
    _install_template(monkeypatch, template)
    _install_text(monkeypatch, text)

    await retry_failed_outbound(c, await _wa_cfg(), controls, "wamid.OUT")

    assert template.calls == []
    assert text.calls == []  # no send attempted to an unset owner number


async def test_owner_alert_degrades_silently_when_unset_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The messages-path sibling of the above: max retries + unset owner number must not raise or
    # send to an empty number.
    c = get_container()
    controls = AdminControls(send_mode="live", owner_alert_number="")
    _, msg_id = await _seed_message_sent("wamid.MSG", "hello")
    for _ in range(MAX_RETRIES):
        await c.conversations.record_message_retry(msg_id, None)

    text = FakeTextSender()
    _install_text(monkeypatch, text)

    await retry_failed_message(c, await _wa_cfg(), controls, "wamid.MSG")

    assert text.calls == []  # no send attempted to an unset owner number
